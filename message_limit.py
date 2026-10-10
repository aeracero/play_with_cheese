"""
message_limit.py - 特定ユーザーの発言を制限する Cog (discord.py 2.x)

機能:
  - 1日のメッセージ数制限（対象ユーザーIDは埋め込み）
  - /roulette  : 今日の上限回数をランダムで決定
  - 文字数制限  : 超過したメッセージを自動削除（/charlimit で変更可）
  - 操作できるのは AUTHORIZED_USER_IDS と /limitadmin add で追加した人のみ
  - 制限対象は TARGET_USER_IDS（埋め込み）と /limittarget add で追加した人
  - 準アドミン: /roulette だけ使える人。範囲指定の可否と固定範囲はアドミンが /subadmin config で設定
  - 短歌モード / 詩人モード（/versemode）: 回数・文字数制限なしの代わりに、
    1メッセージ = 1句（5・7・5・7・7）/ 1行（七五調・脚韻）で投稿しなければ削除。
    厳しさ（非常に厳しい / 厳しい / 普通 / 優しめ）をユーザーごとに設定できる。
    投稿のたびにそのチャンネルの過去の投稿を読み直し、何句目か・ルールを守れているかを確認する

必要な権限: Manage Messages / View Channels / Send Messages / Read Message History
必要な intent: Message Content Intent（文字数制限に必要。Developer Portal でも有効化）
"""

import asyncio
import os
import random
import time
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import verse

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
HISTORY_SCAN_LIMIT = 300  # 短歌/詩人モードで遡って読む最大メッセージ数
ANNOUNCE_COMPLETE = True  # 一首（一連）が完成したら全文を投稿する
AUTO_VERSE_DEFAULT = 100  # /auto_verse で遡る既定のメッセージ数
AUTO_VERSE_COOLDOWN = 30  # /auto_verse のチャンネルごとのクールダウン（秒）
MODE_LABEL = {"normal": "通常", "tanka": "短歌モード", "poet": "詩人モード"}
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
    versemode = app_commands.Group(
        name="versemode", description="短歌モード・詩人モードを管理します"
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
            CREATE TABLE IF NOT EXISTS modes (
                user_id INTEGER PRIMARY KEY, mode TEXT, since_id INTEGER);
            CREATE TABLE IF NOT EXISTS verse_anchors (
                user_id INTEGER, channel_id INTEGER, anchor_id INTEGER,
                PRIMARY KEY (user_id, channel_id));
            """
        )
        # 旧バージョンのDBに厳しさの列を追加
        try:
            self.db.execute("ALTER TABLE modes ADD COLUMN strictness TEXT")
        except sqlite3.OperationalError:
            pass
        self.db.commit()
        # 制限対象（埋め込み + コマンド追加分）。メッセージごとのDB参照を避けるためキャッシュ
        self._db_targets: set[int] = {
            r[0] for r in self.db.execute("SELECT user_id FROM targets")
        }
        self._notified: set[tuple[int, str]] = set()
        self._verse_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._auto_verse_last: dict[int, float] = {}

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

    def _get_mode(self, uid: int) -> tuple[str, int, str]:
        """(モード, 数え始めのメッセージID, 厳しさ)"""
        row = self.db.execute(
            "SELECT mode, since_id, strictness FROM modes WHERE user_id=?", (uid,)
        ).fetchone()
        if not row:
            return ("normal", 0, verse.DEFAULT_STRICTNESS)
        level = row[2] if row[2] in verse.STRICTNESS else verse.DEFAULT_STRICTNESS
        return (row[0], row[1], level)

    def _save_mode(self, uid: int, mode: str, level: str) -> None:
        # 切り替えた瞬間より後の投稿だけを数える（途中の歌はリセット）
        since_id = discord.utils.time_snowflake(discord.utils.utcnow())
        self.db.execute(
            "INSERT OR REPLACE INTO modes (user_id, mode, since_id, strictness) VALUES (?, ?, ?, ?)",
            (uid, mode, since_id, level),
        )
        self.db.execute("DELETE FROM verse_anchors WHERE user_id=?", (uid,))
        self.db.commit()

    def _get_anchor(self, uid: int, channel_id: int) -> int:
        row = self.db.execute(
            "SELECT anchor_id FROM verse_anchors WHERE user_id=? AND channel_id=?",
            (uid, channel_id),
        ).fetchone()
        return row[0] if row else 0

    def _set_anchor(self, uid: int, channel_id: int, anchor_id: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO verse_anchors (user_id, channel_id, anchor_id) VALUES (?, ?, ?)",
            (uid, channel_id, anchor_id),
        )
        self.db.commit()

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

        mode, since_id, level = self._get_mode(uid)
        if mode != "normal":
            await self._handle_verse(message, mode, since_id, level)
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

    # ---------- verse mode ----------
    async def _verse_state(self, message: discord.Message, mode: str, since_id: int,
                           rules: "verse.Rules", edited: bool = False):
        """前回完成した一首（一連）以降のこの人の投稿を読み直し、今どこまで詠んだかを返す。
        ルール違反の句や、制限時間切れの未完成の歌はここで削除する。"""
        uid, ch = message.author.id, message.channel
        cycle = verse.cycle_length(mode)
        # 編集の再判定は、完成済みの歌の中の句かもしれないのでモード開始から読み直す
        anchor = since_id if edited else max(self._get_anchor(uid, ch.id), since_id)
        try:
            history = [
                m async for m in ch.history(
                    limit=HISTORY_SCAN_LIMIT, after=discord.Object(id=anchor),
                    before=message, oldest_first=True,
                )
                if m.author.id == uid
            ]
        except discord.HTTPException:
            history = []

        partial: list[tuple[discord.Message, str]] = []  # (メッセージ, 読み)
        rhyme = None
        expired = False

        async def drop(msgs):
            for m in msgs:
                try:
                    await m.delete()
                except (discord.Forbidden, discord.NotFound):
                    pass

        for m in history + [message]:
            # 制限時間: 前の句から時間が空きすぎたら未完成の歌を破棄
            if rules.time_limit_min and partial:
                gap = (m.created_at - partial[-1][0].created_at).total_seconds()
                if gap > rules.time_limit_min * 60:
                    await drop([pm for pm, _ in partial])
                    partial, rhyme, expired = [], None, True
            if m is message:
                break
            r = verse.check(mode, m.content, len(partial), rules, rhyme,
                            [k for _, k in partial], bool(m.attachments or m.stickers))
            if not r.ok:
                await drop([m])  # 取りこぼし（bot停止中の投稿など）
                continue
            partial.append((m, r.reading))
            rhyme = r.rhyme if r.rhyme else rhyme
            if len(partial) == cycle:
                if not edited:
                    self._set_anchor(uid, ch.id, m.id)
                partial, rhyme = [], None
        return partial, rhyme, expired

    async def _handle_verse(self, message: discord.Message, mode: str, since_id: int,
                            level: str, edited: bool = False):
        uid, ch = message.author.id, message.channel
        rules = verse.STRICTNESS[level]
        lock = self._verse_locks.setdefault((uid, ch.id), asyncio.Lock())
        async with lock:
            partial, rhyme, expired = await self._verse_state(
                message, mode, since_id, rules, edited)
            pos = len(partial)
            r = verse.check(mode, message.content, pos, rules, rhyme,
                            [k for _, k in partial],
                            bool(message.attachments or message.stickers))
            notes = []
            if expired:
                notes.append(f"前の句から{rules.time_limit_min}分以上空いたため、"
                             "未完成の歌は消えました。最初から詠み直しです")
            if r.ok:
                if pos + 1 == verse.cycle_length(mode) and not edited:
                    self._set_anchor(uid, ch.id, message.id)
                    if ANNOUNCE_COMPLETE:
                        title = "🎋 一首できました" if mode == "tanka" else "📜 一連できました"
                        lines = [pm.content for pm, _ in partial] + [message.content]
                        try:
                            await ch.send(
                                f"{title}（{message.author.display_name}）\n" + "\n".join(lines),
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except discord.HTTPException:
                            pass
                if notes:
                    try:
                        await ch.send(f"{message.author.mention} " + notes[0], delete_after=10)
                    except discord.HTTPException:
                        pass
                return

            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                return
            head = f"【{MODE_LABEL[mode]}・{rules.label}】"
            if expired:
                pos, rhyme = 0, None
            try:
                await ch.send(
                    f"{message.author.mention} {head}{verse.expected_hint(mode, pos, rhyme)}\n"
                    + "\n".join(f"・{e}" for e in notes + r.errors),
                    delete_after=12,
                )
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or after.guild is None or before.content == after.content:
            return
        if after.author.id not in self._target_ids():
            return
        mode, since_id, level = self._get_mode(after.author.id)
        if mode == "normal" or after.id <= since_id:
            return
        rules = verse.STRICTNESS[level]
        if rules.edit_policy == "recheck":
            # 編集後の内容を、その句の位置のルールでもう一度判定（違反なら削除）
            await self._handle_verse(after, mode, since_id, level, edited=True)
            return
        try:
            await after.delete()
            await after.channel.send(
                f"{after.author.mention} 【{MODE_LABEL[mode]}・{rules.label}】編集は禁止です。"
                "編集した句は削除しました（その句から詠み直してください）。",
                delete_after=10,
            )
        except discord.HTTPException:
            pass

    # ---------- /auto_verse ----------
    @app_commands.command(name="auto_verse", description="このチャンネルの最近の発言を集めて短歌にします")
    @app_commands.describe(
        count=f"遡るメッセージ数（10〜500、省略時は{AUTO_VERSE_DEFAULT}）",
        user="この人の発言だけで詠む",
    )
    async def auto_verse(
        self,
        interaction: discord.Interaction,
        count: Optional[app_commands.Range[int, 10, 500]] = None,
        user: Optional[discord.User] = None,
    ):
        ch = interaction.channel
        if ch is None or not hasattr(ch, "history"):
            await interaction.response.send_message("このチャンネルでは使えません。", ephemeral=True)
            return
        now = time.monotonic()
        wait = AUTO_VERSE_COOLDOWN - (now - self._auto_verse_last.get(ch.id, -1e9))
        if wait > 0 and not self._is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"歌を詠むには少し間をおいてください（あと{int(wait) + 1}秒）。", ephemeral=True
            )
            return
        self._auto_verse_last[ch.id] = now
        await interaction.response.defer(thinking=True)

        limit = count or AUTO_VERSE_DEFAULT
        try:
            msgs = [
                m async for m in ch.history(limit=limit)
                if not m.author.bot and m.content.strip()
                and not m.content.startswith(("/", "!"))
                and (user is None or m.author.id == user.id)
            ]
        except discord.Forbidden:
            await interaction.followup.send("このチャンネルの履歴を読む権限がありません。")
            return
        msgs.reverse()  # 古い順
        result = await asyncio.to_thread(verse.compose_tanka, [m.content for m in msgs])
        if result is None:
            who = f"{user.display_name} の" if user else ""
            await interaction.followup.send(
                f"直近{limit}件の{who}発言からは、5音・7音の句が足りず短歌になりませんでした。"
                "もう少し会話が進んでからお試しください。"
            )
            return

        lines = []
        for text, idx in result["lines"]:
            m = msgs[idx]
            lines.append(f"{text}　— [{m.author.display_name}]({m.jump_url})")
        if result["kind"] == "accidental":
            m = msgs[result["lines"][0][1]]
            title = "🎯 偶然短歌を見つけました"
            desc = "\n".join(t for t, _ in result["lines"])
            footer = f"{m.author.display_name} の発言がそのまま 5・7・5・7・7 でした"
            embed = discord.Embed(title=title, description=desc, color=0xE67E22, url=m.jump_url)
        else:
            authors = []
            for _, idx in result["lines"]:
                n = msgs[idx].author.display_name
                if n not in authors:
                    authors.append(n)
            embed = discord.Embed(
                title="🎋 みんなの言葉で一首",
                description="\n".join(lines),
                color=0x27AE60,
            )
            footer = f"詠み人: {'・'.join(authors)}（直近{limit}件の発言より）"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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
        lines = []
        for uid in sorted(self._target_ids()):
            mode, _, level = self._get_mode(uid)
            if mode == "normal":
                lines.append(
                    f"<@{uid}>: 今日 {self._get_count(uid, date)} / {self._get_limit(uid, date)} 件"
                )
            else:
                lines.append(f"<@{uid}>: {MODE_LABEL[mode]}・{verse.STRICTNESS[level].label}"
                             "（回数制限なし）")
        lines.append(f"文字数制限: {'なし' if max_chars == 0 else f'{max_chars}文字'}")
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- verse mode management ----------
    _STRICT_CHOICES = [
        app_commands.Choice(name="非常に厳しい", value="very_strict"),
        app_commands.Choice(name="厳しい", value="strict"),
        app_commands.Choice(name="普通", value="normal"),
        app_commands.Choice(name="優しめ", value="gentle"),
    ]

    async def _announce(self, interaction: discord.Interaction, user: discord.User,
                        mode: str, level: str, changed_level_only: bool = False):
        """モード指定時の公開アナウンス"""
        rules = verse.STRICTNESS[level]
        name = getattr(user, "display_name", user.name)
        if mode == "normal":
            embed = discord.Embed(
                title=f"🕊️ {name} は本日から普通の人に戻ります",
                description="通常の回数・文字数制限に戻りました。",
                color=0x95A5A6,
            )
        else:
            if changed_level_only:
                title = f"⚖️ {name} の掟が「{rules.label}」に変わりました"
            elif mode == "poet":
                title = f"📜 {name} は本日から詩人になります！"
            else:
                title = f"🎋 {name} は本日から歌人になります！"
            embed = discord.Embed(
                title=title,
                description="発言回数は無制限。その代わり、次の掟を守ること。",
                color=0x8E44AD if mode == "poet" else 0x27AE60,
            )
            embed.add_field(
                name=f"厳しさ: {rules.label}",
                value="\n".join(f"・{x}" for x in verse.describe_rules(mode, rules)),
                inline=False,
            )
        await interaction.response.send_message(
            content=user.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )

    @versemode.command(name="set", description="制限対象のユーザーのモードを切り替えます")
    @app_commands.describe(user="対象ユーザー", mode="モード", strictness="ルールの厳しさ（省略時は今の設定、初回は「厳しい」）")
    @app_commands.choices(mode=[
        app_commands.Choice(name="通常（回数制限）", value="normal"),
        app_commands.Choice(name="短歌モード（5・7・5・7・7）", value="tanka"),
        app_commands.Choice(name="詩人モード（七五調・脚韻）", value="poet"),
    ], strictness=_STRICT_CHOICES)
    async def versemode_set(
        self, interaction: discord.Interaction, user: discord.User,
        mode: app_commands.Choice[str],
        strictness: Optional[app_commands.Choice[str]] = None,
    ):
        if not await self._guard(interaction):
            return
        if user.id not in self._target_ids():
            await interaction.response.send_message(
                "このユーザーは制限対象ではありません（/limittarget add で追加できます）。",
                ephemeral=True,
            )
            return
        level = strictness.value if strictness else self._get_mode(user.id)[2]
        self._save_mode(user.id, mode.value, level)
        await self._announce(interaction, user, mode.value, level)

    @versemode.command(name="strictness", description="ルールの厳しさだけを変更します（詠みかけの歌はリセット）")
    @app_commands.describe(user="対象ユーザー", level="厳しさ")
    @app_commands.choices(level=_STRICT_CHOICES)
    async def versemode_strictness(
        self, interaction: discord.Interaction, user: discord.User,
        level: app_commands.Choice[str],
    ):
        if not await self._guard(interaction):
            return
        if user.id not in self._target_ids():
            await interaction.response.send_message(
                "このユーザーは制限対象ではありません。", ephemeral=True
            )
            return
        mode = self._get_mode(user.id)[0]
        self._save_mode(user.id, mode, level.value)
        if mode == "normal":
            await interaction.response.send_message(
                f"{user.mention} の厳しさを「{level.name}」にしました"
                "（短歌・詩人モードにしたときに適用されます）。",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._announce(interaction, user, mode, level.value, changed_level_only=True)

    @versemode.command(name="list", description="各ユーザーの現在のモードと厳しさを表示します")
    async def versemode_list(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        rows = []
        for u in sorted(self._target_ids()):
            mode, _, level = self._get_mode(u)
            rows.append(f"- <@{u}>: {MODE_LABEL[mode]}（厳しさ: {verse.STRICTNESS[level].label}）")
        await interaction.response.send_message(
            "\n".join(rows) or "制限対象のユーザーはいません。",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
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
