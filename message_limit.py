"""
message_limit.py - 特定ユーザーの発言を制限する Cog (discord.py 2.x)

機能:
  - 1日のメッセージ数制限（対象ユーザーIDは埋め込み）
  - /roulette  : 今日の上限回数をランダムで決定
  - 文字数制限  : 超過したメッセージを自動削除（/charlimit で変更可）
  - 操作できるのは AUTHORIZED_USER_IDS と /limitadmin add で追加した人のみ
  - 制限対象は TARGET_USER_IDS（埋め込み）と /limittarget add で追加した人
  - 準アドミン: /roulette だけ使える人。範囲指定の可否と固定範囲はアドミンが /subadmin config で設定

必要な権限: Manage Messages / View Channels / Send Messages / Read Message History
必要な intent: Message Content Intent（文字数制限に必要。Developer Portal でも有効化）
"""

import os
import random
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

# ===== 設定（ここを書き換えてください） =====
TARGET_USER_IDS = {1418847343003832351}   # 制限対象のユーザーID（複数可）
AUTHORIZED_USER_IDS = {                    # コマンドを使える人（ここに追加していけます）
    1397897673612329022,
    916106297190019102,
}
DAILY_LIMIT = 10        # ルーレットを回していない日の、1日の上限メッセージ数
ROULETTE_MIN = 1        # /roulette のデフォルト最小値
ROULETTE_MAX = 30       # /roulette のデフォルト最大値
MAX_CHARS = 200         # 1メッセージの最大文字数（0で無制限）
TZ = ZoneInfo("Asia/Tokyo")  # 日付の切り替え基準（0:00 JST）
DB_PATH = os.getenv("DB_PATH", "message_limit.db")  # Railwayでは Volume 配下を指定
# ============================================


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


class MessageLimit(commands.Cog):
    limitadmin = app_commands.Group(
        name="limitadmin", description="コマンドを使える人を管理します"
    )
    subadmin = app_commands.Group(
        name="subadmin",
        description="準アドミン（/rouletteだけ使える人）を管理します",
    )
    limittarget = app_commands.Group(
        name="limittarget", description="制限対象のユーザーを管理します"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect(DB_PATH)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS counts (
                user_id INTEGER, date TEXT, count INTEGER,
                PRIMARY KEY (user_id, date));
            CREATE TABLE IF NOT EXISTS daily_limits (
                user_id INTEGER, date TEXT, limit_value INTEGER,
                PRIMARY KEY (user_id, date));
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS targets (
                user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS sub_admins (
                user_id INTEGER PRIMARY KEY);
            """
        )
        self.db.commit()
        # 制限対象（埋め込み + コマンド追加分）。メッセージごとのDB参照を避けるためキャッシュ
        self._db_targets: set[int] = {
            r[0] for r in self.db.execute("SELECT user_id FROM targets")
        }
        self._notified: set[tuple[int, str]] = set()

    def cog_unload(self):
        self.db.close()

    # ---------- DB helpers ----------
    def _get_count(self, user_id: int, date: str) -> int:
        row = self.db.execute(
            "SELECT count FROM counts WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()
        return row[0] if row else 0

    def _increment(self, user_id: int, date: str) -> None:
        self.db.execute(
            """INSERT INTO counts (user_id, date, count) VALUES (?, ?, 1)
               ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1""",
            (user_id, date),
        )
        self.db.commit()

    def _get_limit(self, user_id: int, date: str) -> int:
        row = self.db.execute(
            "SELECT limit_value FROM daily_limits WHERE user_id=? AND date=?",
            (user_id, date),
        ).fetchone()
        return row[0] if row else DAILY_LIMIT

    def _set_limit(self, user_id: int, date: str, value: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO daily_limits (user_id, date, limit_value) VALUES (?, ?, ?)",
            (user_id, date, value),
        )
        self.db.commit()

    def _get_max_chars(self) -> int:
        row = self.db.execute("SELECT value FROM settings WHERE key='max_chars'").fetchone()
        return row[0] if row else MAX_CHARS

    def _get_setting(self, key: str, default: int) -> int:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_setting(self, key: str, value: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def _sub_admin_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT user_id FROM sub_admins")}

    def _sub_custom_allowed(self) -> bool:
        return bool(self._get_setting("sub_custom_allowed", 0))

    def _sub_range(self) -> tuple[int, int]:
        return (
            self._get_setting("sub_min", ROULETTE_MIN),
            self._get_setting("sub_max", ROULETTE_MAX),
        )

    def _target_ids(self) -> set[int]:
        return TARGET_USER_IDS | self._db_targets

    def _db_admin_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT user_id FROM admins")}

    # ---------- permission ----------
    def _is_admin(self, uid: int) -> bool:
        return uid in AUTHORIZED_USER_IDS or uid in self._db_admin_ids()

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """アドミン専用コマンド用のチェック"""
        if self._is_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            "このコマンドを使う権限がありません。", ephemeral=True
        )
        return False

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        uid = message.author.id
        if uid not in self._target_ids():
            return

        # 1) 文字数制限（超過分は回数にカウントしない）
        max_chars = self._get_max_chars()
        if max_chars > 0 and len(message.content) > max_chars:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                return
            try:
                await message.channel.send(
                    f"{message.author.mention} 1メッセージは{max_chars}文字までです"
                    f"（{len(message.content)}文字でした）。",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            return

        # 2) 1日の回数制限
        date = today()
        limit = self._get_limit(uid, date)
        if self._get_count(uid, date) < limit:
            self._increment(uid, date)
            return

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            return

        if (uid, date) not in self._notified:
            self._notified = {k for k in self._notified if k[1] == date}
            self._notified.add((uid, date))
            try:
                await message.channel.send(
                    f"{message.author.mention} 本日のメッセージ上限（{limit}件）に達しました。"
                    "0:00（JST）にリセットされます。",
                    delete_after=10,
                )
            except discord.HTTPException:
                pass

    # ---------- commands ----------
    @app_commands.command(name="roulette", description="今日の上限回数をランダムで決めます")
    @app_commands.describe(
        user="対象ユーザー（省略すると制限対象の全員）",
        minimum="最小回数（省略時はデフォルトの範囲）",
        maximum="最大回数（省略時はデフォルトの範囲）",
    )
    @app_commands.rename(minimum="min", maximum="max")
    async def roulette(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        minimum: Optional[app_commands.Range[int, 0, 1000]] = None,
        maximum: Optional[app_commands.Range[int, 0, 1000]] = None,
    ):
        uid = interaction.user.id
        is_admin = self._is_admin(uid)
        if not is_admin and uid not in self._sub_admin_ids():
            await interaction.response.send_message(
                "このコマンドを使う権限がありません。", ephemeral=True
            )
            return

        note = ""
        if is_admin:
            lo = minimum if minimum is not None else ROULETTE_MIN
            hi = maximum if maximum is not None else ROULETTE_MAX
        else:
            sub_lo, sub_hi = self._sub_range()
            if self._sub_custom_allowed():
                lo = minimum if minimum is not None else sub_lo
                hi = maximum if maximum is not None else sub_hi
            else:
                lo, hi = sub_lo, sub_hi
                if minimum is not None or maximum is not None:
                    note = "\n（範囲指定は許可されていないため、固定範囲を使用しました）"

        if lo > hi:
            await interaction.response.send_message(
                "min は max 以下にしてください。", ephemeral=True
            )
            return

        if user is not None:
            if user.id not in self._target_ids():
                await interaction.response.send_message(
                    "このユーザーは制限対象ではありません（/limittarget add で追加できます）。",
                    ephemeral=True,
                )
                return
            targets = [user.id]
        else:
            targets = sorted(self._target_ids())
            if not targets:
                await interaction.response.send_message(
                    "制限対象のユーザーがいません（/limittarget add で追加してください）。",
                    ephemeral=True,
                )
                return

        date = today()
        lines = []
        for tid in targets:
            n = random.randint(lo, hi)
            self._set_limit(tid, date, n)
            lines.append(f"<@{tid}> の今日の上限は **{n}件** に決まりました！")
        await interaction.response.send_message(
            f"🎰 ルーレット結果（{lo}〜{hi}）\n" + "\n".join(lines) + note,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="charlimit", description="1メッセージの最大文字数を設定します")
    @app_commands.describe(chars="最大文字数（0で無制限）")
    async def charlimit(
        self, interaction: discord.Interaction, chars: app_commands.Range[int, 0, 4000]
    ):
        if not await self._guard(interaction):
            return
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('max_chars', ?)", (chars,)
        )
        self.db.commit()
        msg = "文字数制限を解除しました。" if chars == 0 else f"最大文字数を {chars} 文字に設定しました。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="limitstatus", description="今日の使用状況を表示します")
    async def limitstatus(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        date = today()
        max_chars = self._get_max_chars()
        lines = [
            f"<@{uid}>: 今日 {self._get_count(uid, date)} / {self._get_limit(uid, date)} 件"
            for uid in sorted(self._target_ids())
        ]
        lines.append(f"文字数制限: {'なし' if max_chars == 0 else f'{max_chars}文字'}")
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- sub-admin management ----------
    @subadmin.command(name="add", description="準アドミン（/rouletteだけ使える人）を追加します")
    @app_commands.describe(user="追加するユーザー")
    async def subadmin_add(self, interaction: discord.Interaction, user: discord.User):
        if not await self._guard(interaction):
            return
        self.db.execute("INSERT OR IGNORE INTO sub_admins (user_id) VALUES (?)", (user.id,))
        self.db.commit()
        await interaction.response.send_message(
            f"{user.mention} を準アドミンに追加しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @subadmin.command(name="remove", description="準アドミンを外します")
    @app_commands.describe(user="外すユーザー")
    async def subadmin_remove(self, interaction: discord.Interaction, user: discord.User):
        if not await self._guard(interaction):
            return
        self.db.execute("DELETE FROM sub_admins WHERE user_id=?", (user.id,))
        self.db.commit()
        await interaction.response.send_message(
            f"{user.mention} を準アドミンから外しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @subadmin.command(name="list", description="準アドミンの一覧と現在の設定を表示します")
    async def subadmin_list(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        lo, hi = self._sub_range()
        members = "\n".join(f"- <@{u}>" for u in sorted(self._sub_admin_ids())) or "- （なし）"
        mode = "許可（自由に指定可）" if self._sub_custom_allowed() else "禁止（固定範囲のみ）"
        await interaction.response.send_message(
            f"準アドミン:\n{members}\n\n範囲指定: {mode}\n準アドミンの範囲: {lo}〜{hi}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @subadmin.command(name="config", description="準アドミンの/rouletteの範囲設定を変更します")
    @app_commands.describe(
        allow_custom="準アドミンが範囲（min/max）を指定できるか",
        minimum="準アドミンの範囲の最小値（禁止時は固定、許可時は省略時のデフォルト）",
        maximum="準アドミンの範囲の最大値（禁止時は固定、許可時は省略時のデフォルト）",
    )
    @app_commands.rename(minimum="min", maximum="max")
    async def subadmin_config(
        self,
        interaction: discord.Interaction,
        allow_custom: Optional[bool] = None,
        minimum: Optional[app_commands.Range[int, 0, 1000]] = None,
        maximum: Optional[app_commands.Range[int, 0, 1000]] = None,
    ):
        if not await self._guard(interaction):
            return
        cur_lo, cur_hi = self._sub_range()
        lo = minimum if minimum is not None else cur_lo
        hi = maximum if maximum is not None else cur_hi
        if lo > hi:
            await interaction.response.send_message(
                "min は max 以下にしてください。", ephemeral=True
            )
            return
        if allow_custom is not None:
            self._set_setting("sub_custom_allowed", int(allow_custom))
        if minimum is not None or maximum is not None:
            self._set_setting("sub_min", lo)
            self._set_setting("sub_max", hi)
        mode = "許可（自由に指定可）" if self._sub_custom_allowed() else "禁止（固定範囲のみ）"
        await interaction.response.send_message(
            f"準アドミンの設定\n範囲指定: {mode}\n範囲: {lo}〜{hi}", ephemeral=True
        )

    # ---------- target management ----------
    @limittarget.command(name="add", description="制限対象のユーザーを追加します")
    @app_commands.describe(user="追加するユーザー")
    async def target_add(self, interaction: discord.Interaction, user: discord.User):
        if not await self._guard(interaction):
            return
        if user.bot:
            await interaction.response.send_message(
                "botは制限対象にできません。", ephemeral=True
            )
            return
        self.db.execute("INSERT OR IGNORE INTO targets (user_id) VALUES (?)", (user.id,))
        self.db.commit()
        self._db_targets.add(user.id)
        await interaction.response.send_message(
            f"{user.mention} を制限対象に追加しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @limittarget.command(name="remove", description="追加した制限対象を外します")
    @app_commands.describe(user="外すユーザー")
    async def target_remove(self, interaction: discord.Interaction, user: discord.User):
        if not await self._guard(interaction):
            return
        if user.id in TARGET_USER_IDS:
            await interaction.response.send_message(
                "コードに埋め込まれたユーザーはコマンドから外せません"
                "（TARGET_USER_IDS を編集してください）。",
                ephemeral=True,
            )
            return
        self.db.execute("DELETE FROM targets WHERE user_id=?", (user.id,))
        self.db.commit()
        self._db_targets.discard(user.id)
        await interaction.response.send_message(
            f"{user.mention} を制限対象から外しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @limittarget.command(name="list", description="制限対象の一覧を表示します")
    async def target_list(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        fixed = [f"- <@{u}>（埋め込み）" for u in sorted(TARGET_USER_IDS)]
        added = [f"- <@{u}>" for u in sorted(self._db_targets - TARGET_USER_IDS)]
        text = "\n".join(fixed + added) or "制限対象のユーザーはいません。"
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    # ---------- admin management ----------
    @limitadmin.command(name="add", description="コマンドを使える人を追加します")
    @app_commands.describe(user="追加するユーザー")
    async def admin_add(self, interaction: discord.Interaction, user: discord.User):
        if not await self._guard(interaction):
            return
        self.db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user.id,))
        self.db.commit()
        await interaction.response.send_message(
            f"{user.mention} をコマンド使用者に追加しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @limitadmin.command(name="remove", description="追加した使用者を外します")
    @app_commands.describe(user="外すユーザー")
    async def admin_remove(self, interaction: discord.Interaction, user: discord.User):
        if not await self._guard(interaction):
            return
        if user.id in AUTHORIZED_USER_IDS:
            await interaction.response.send_message(
                "コードに埋め込まれたユーザーはコマンドから外せません"
                "（AUTHORIZED_USER_IDS を編集してください）。",
                ephemeral=True,
            )
            return
        self.db.execute("DELETE FROM admins WHERE user_id=?", (user.id,))
        self.db.commit()
        await interaction.response.send_message(
            f"{user.mention} を外しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @limitadmin.command(name="list", description="コマンドを使える人の一覧を表示します")
    async def admin_list(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        fixed = "\n".join(f"- <@{u}>（埋め込み）" for u in sorted(AUTHORIZED_USER_IDS))
        added = "\n".join(f"- <@{u}>" for u in sorted(self._db_admin_ids()))
        text = fixed + ("\n" + added if added else "")
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MessageLimit(bot))
