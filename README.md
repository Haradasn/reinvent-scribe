# reinvent-scribe

re:Invent などの英語セッションを **ローカルで録音+リアルタイム文字起こし** し、
準リアルタイムの日本語要約・文字起こしチャット・出張日報(Slack 貼り付け用)まで
面倒を見るローカル Web アプリ。

- 文字起こしはローカル (faster-whisper) — **会場の Wi-Fi が死んでいても録音と字幕は動く**
- 停止後にバックグラウンドで高精度モデルによる再文字起こし (`transcript_hq.md`)
- 要約/チャット/日報は OpenAI 互換の LLM API(安いモデルを自由に選択、未設定でも劣化動作)

## セットアップ

### Mac

```bash
git clone <this-repo> && cd reinvent-scribe
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m reinvent_scribe   # ブラウザが自動で開く
```

### Windows

```powershell
git clone <this-repo>; cd reinvent-scribe
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e .
python -m reinvent_scribe
```

> **重要: 出発前に必ず一度起動して録音→停止まで試すこと。**
> 初回はモデル(small.en と large-v3-turbo、計 数GB)のダウンロードが走るため、
> オフラインの現地では初回起動できません。マイク権限の許可もこのとき済ませます。

## 環境変数(要約・チャット・日報を使う場合)

OpenAI 互換の Chat Completions API ならどこでも使えます。安いモデル推奨
(gpt-4o-mini / Gemini Flash 級なら 1 セッション 1 時間の要約で数円以下)。

```bash
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_API_KEY="sk-..."
export LLM_MODEL="gpt-4o-mini"
export SUMMARY_INTERVAL_SEC=180   # 準リアルタイム要約の間隔(任意)
export DATA_DIR=data              # 保存先(任意)
export WHISPER_MODEL=small.en     # リアルタイム用モデル(任意)
```

未設定でも録音・字幕・Markdown 保存は全て動きます(要約ペインは「オフライン」表示)。

## 使い方

1. **セッション前**: `sessions.csv` を用意しておくと 1 クリック開始できる(下記)
2. **セッション中**: ●ボタン → ID/タイトル入力 → 録音開始。英語字幕が流れ、
   オンラインなら数分ごとに日本語要約が右ペインに積まれる。
   チャット欄で「さっきの新機能って何?」のように質問も可能
3. **停止後**: 高精度文字起こしが自動で走り `transcript_hq.md` ができる
4. **夜**: `transcript_hq.md` を Claude に渡して記事化(`prompts/nightly_article.md` 参照)
5. **1日の終わり**: 「日報生成」ボタン → コピー → Slack に貼って上司へ共有

## sessions.csv(1クリック開始用)

リポジトリ直下に置く。形式: `日付,開始時刻,セッションID,タイトル`

```csv
2026-12-01,08:30,KEY001,Opening Keynote
2026-12-01,13:00,SVS201,Serverless Deep Dive
```

現在時刻 ±30 分のセッションが起動モーダルに自動提案されます。

## glossary.txt(AWS 用語の認識精度向上)

`glossary.txt` の単語が Whisper の initial_prompt に注入されます。
自分が聞くセッションの固有名詞に合わせて自由に編集してください。
**約 170 語(≒224 トークン)を超えた分は使われません**(起動ログに警告が出ます)。

## データの場所

```
data/2026-12-01/1201_0830_KEY001/
  audio.wav          # 録音(クラッシュしても途中まで有効)
  transcript.md      # リアルタイム文字起こし
  transcript_hq.md   # 高精度版(夜に Claude へ渡すのはこちら)
  meta.json          # ID/タイトル/メモ/開始終了時刻
  chat.md            # チャット履歴
data/2026-12-01/daily_summary.md   # 出張日報
```

## テスト

```bash
pip install -e ".[dev]"
pytest
```
