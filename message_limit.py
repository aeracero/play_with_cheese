"""
message_limit.py - 特定ユーザーの発言を制限する Cog (discord.py 2.x)

機能:
  - 1日のメッセージ数制限（対象ユーザーIDは埋め込み）
  - /roulette  : 今日の上限回数をランダムで決定
  - 文字数制限  : 超過したメッセージを自動削除（/charlimit で変更可）
  - 操作できるのは AUTHORIZED_USER_IDS と /limitadmin add で追加した人のみ

必要な権限: Manage Messages / View Channels / Send Messages / Read Message History
必要な intent: Message Content Intent（文字数制限に必要。Developer Portal でも有効化）
"""

import os
import random
import sqlite3
from datetime import datetime
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
            """
        )
        self.db.commit()
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

    def _db_admin_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT user_id FROM admins")}

    # ---------- permission ----------
    async def _guard(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        if uid in AUTHORIZED_USER_IDS or uid in self._db_admin_ids():
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
        if uid not in TARGET_USER_IDS:
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
        minimum=f"最小回数（デフォルト {ROULETTE_MIN}）",
        maximum=f"最大回数（デフォルト {ROULETTE_MAX}）",
    )
    @app_commands.rename(minimum="min", maximum="max")
    async def roulette(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        minimum: app_commands.Range[int, 0, 1000] = ROULETTE_MIN,
        maximum: app_commands.Range[int, 0, 1000] = ROULETTE_MAX,
    ):
        if not await self._guard(interaction):
            return
        if minimum > maximum:
            await interaction.response.send_message(
                "min は max 以下にしてください。", ephemeral=True
            )
            return

        if user is not None:
            if user.id not in TARGET_USER_IDS:
                await interaction.response.send_message(
                    "このユーザーは制限対象ではありません（TARGET_USER_IDS に追加してください）。",
                    ephemeral=True,
                )
                return
            targets = [user.id]
        else:
            targets = sorted(TARGET_USER_IDS)

        date = today()
        lines = []
        for uid in targets:
            n = random.randint(minimum, maximum)
            self._set_limit(uid, date, n)
            lines.append(f"<@{uid}> の今日の上限は **{n}件** に決まりました！")
        await interaction.response.send_message(
            "🎰 ルーレット結果（" + f"{minimum}〜{maximum}）\n" + "\n".join(lines),
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
            for uid in sorted(TARGET_USER_IDS)
        ]
        lines.append(f"文字数制限: {'なし' if max_chars == 0 else f'{max_chars}文字'}")
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
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
