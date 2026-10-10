# Discord Message Limit Bot

特定ユーザーの1日あたりのメッセージ数・文字数を制限する Discord bot（discord.py 2.x）。

## ファイル
- `bot.py` : エントリポイント
- `message_limit.py` : 本体の Cog（設定は冒頭の定数）
- `verse.py` : 短歌/詩人モードの判定（音数・ルビ・脚韻。ルールは冒頭の定数）
- `requirements.txt` : 依存ライブラリ

## 設定（message_limit.py の冒頭）
- `TARGET_USER_IDS` : 制限対象のユーザーID（`/limittarget add` でも追加可）
- `AUTHORIZED_USER_IDS` : コマンドを使える人のID
- `DAILY_LIMIT` / `ROULETTE_MIN` / `ROULETTE_MAX` / `MAX_CHARS`

## コマンド（AUTHORIZED_USER_IDS と /limitadmin add の人のみ。/roulette は準アドミンも可）
- `/roulette [user] [min] [max]` : 今日の上限回数をランダムで決定
- `/charlimit chars` : 最大文字数を設定（0で無制限）
- `/limitstatus` : 今日の使用状況
- `/limittarget add|remove|list` : 制限対象ユーザーの管理
- `/subadmin add|remove|list` : 準アドミン（/rouletteだけ使える人）の管理
- `/subadmin config [allow_custom] [min] [max]` : 準アドミンの範囲指定の可否と範囲を設定
- `/limitadmin add|remove|list` : 使用者の管理
- `/versemode set user mode [strictness]` : 通常 / 短歌モード / 詩人モード を切り替え（チャンネルにアナウンス）
- `/versemode strictness user level` : 厳しさだけ変更（非常に厳しい / 厳しい / 普通 / 優しめ）
- `/versemode list` : 各ユーザーのモードと厳しさ
- `/auto_verse [count] [user]` : チャンネルの最近の発言から短歌を作る（誰でも使用可、チャンネルごとに30秒のクールダウン）

## 短歌モード・詩人モード

回数・文字数制限がなくなる代わりに、形式を守らない投稿は即削除されます。

- 短歌モード: 1メッセージ = 1句。5→7→5→7→7音の順に投稿し、5句で一首完成
- 詩人モード: 1メッセージ = 1行の七五調（`七音 五音` と空白で区切る）。4行で1連、連の中は行末の母音をそろえる（脚韻）
- 投稿のたびに、そのチャンネルの前回完成した一首以降の投稿を読み直して「何句目か」「前の句がルールを守っているか」を再確認（不正な句が残っていれば削除）
- 漢字の読みは MeCab で自動判定。違う読みにしたいときは `漢字《かな》`、英字などは `｜AI《えーあい》`
- 小書き文字（ゃゅょ等）は数えない／っ・ん・ー は1音／句読点は数えない
- 1行のみ・添付/スタンプ/絵文字/メンション/URL 禁止・編集禁止（編集した句は削除）

## Railway へのデプロイ
1. GitHub に push して Railway で Deploy from GitHub repo
2. Variables: `DISCORD_TOKEN`, `DB_PATH=/data/message_limit.db`, （任意）`GUILD_ID`
3. Volume を作成し、マウントパスを `/data` にする（付けないと再デプロイでデータが消えます）
4. Start Command: `python bot.py`

## Discord Developer Portal
- Message Content Intent を ON
- 招待スコープ: `bot`, `applications.commands`
- bot 権限: メッセージの管理 / チャンネルを見る / メッセージを送信 / メッセージ履歴を読む

### 厳しさ

| | 非常に厳しい | 厳しい | 普通 | 優しめ |
|---|---|---|---|---|
| 字余り・字足らず | なし | なし | ±1音 | ±2音 |
| 句読点・記号 | 禁止 | 可（数えない） | 可 | 可 |
| 英数字（ルビ付き） | 禁止 | 可 | 可 | 可 |
| 絵文字 | 禁止 | 禁止 | 可（数えない） | 可 |
| 添付・スタンプ・メンション | 禁止 | 禁止 | 禁止 | 可 |
| 詩人: 七と五の間の空白 | 必須 | 必須 | 必須 | 不要（全体で12音） |
| 詩人: 脚韻 | 行末2音 | 行末1音 | 行末1音 | なし |
| 同じ句の使い回し | 禁止 | 可 | 可 | 可 |
| 制限時間 | 前の句から10分（超えると未完成の歌は消える） | なし | なし | なし |
| 編集 | 禁止（削除） | 禁止（削除） | 再判定 | 再判定 |

各レベルの中身は `verse.py` の `STRICTNESS` で変更できます。

## /auto_verse

- まず、1つの発言の中にそのまま 5・7・5・7・7 が隠れていないか探します（偶然短歌）
- なければ、発言から文節の切れ目で切り出せる5音・7音の句を集めて一首にします。できるだけ会話の順に、1つの発言から1句ずつ選びます
- 各句に発言者と元メッセージへのリンクが付きます。bot の発言と `/` `!` で始まるコマンドは使いません
