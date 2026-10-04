# Discord Message Limit Bot

特定ユーザーの1日あたりのメッセージ数・文字数を制限する Discord bot（discord.py 2.x）。

## ファイル
- `bot.py` : エントリポイント
- `message_limit.py` : 本体の Cog（設定は冒頭の定数）
- `requirements.txt` : 依存ライブラリ

## 設定（message_limit.py の冒頭）
- `TARGET_USER_IDS` : 制限対象のユーザーID（`/limittarget add` でも追加可）
- `AUTHORIZED_USER_IDS` : コマンドを使える人のID
- `DAILY_LIMIT` / `ROULETTE_MIN` / `ROULETTE_MAX` / `MAX_CHARS`

## コマンド（AUTHORIZED_USER_IDS と /limitadmin add の人のみ）
- `/roulette [user] [min] [max]` : 今日の上限回数をランダムで決定
- `/charlimit chars` : 最大文字数を設定（0で無制限）
- `/limitstatus` : 今日の使用状況
- `/limittarget add|remove|list` : 制限対象ユーザーの管理
- `/limitadmin add|remove|list` : 使用者の管理

## Railway へのデプロイ
1. GitHub に push して Railway で Deploy from GitHub repo
2. Variables: `DISCORD_TOKEN`, `DB_PATH=/data/message_limit.db`, （任意）`GUILD_ID`
3. Volume を作成し、マウントパスを `/data` にする（付けないと再デプロイでデータが消えます）
4. Start Command: `python bot.py`

## Discord Developer Portal
- Message Content Intent を ON
- 招待スコープ: `bot`, `applications.commands`
- bot 権限: メッセージの管理 / チャンネルを見る / メッセージを送信 / メッセージ履歴を読む
