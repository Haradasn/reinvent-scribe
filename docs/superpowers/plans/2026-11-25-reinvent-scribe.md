# reinvent-scribe 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** re:Invent セッションをローカルで録音・リアルタイム英語文字起こしし、準リアルタイム日本語要約・文字起こしチャット・出張日報を生成するローカル Web アプリ。

**Architecture:** Python バックエンド(FastAPI + WebSocket)がマイク音声を faster-whisper でストリーム文字起こしし WAV を逐次保存。要約/チャット/日報は OpenAI 互換 LLM API(差し替え可能、オフライン時は劣化動作)。フロントは同梱の単一ページ(素の HTML/JS、アニメーション重視)。

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, sounddevice, faster-whisper, httpx, pytest。フロントはビルドツールなしの HTML/CSS/JS。

**Spec:** `docs/superpowers/specs/2026-11-25-reinvent-scribe-design.md`

## Global Constraints

- オフラインで録音・字幕・Markdown 出力が完結すること。LLM 機能の失敗が録音・文字起こしに影響してはならない
- 依存はクロスプラットフォームの PyPI パッケージのみ。OS 依存コードを書かない(Mac/Windows 両対応)
- Whisper モデル既定 `small.en`(int8)、実時間比 1x 未満なら `base.en` に自動フォールバック
- データ配置: `./data/YYYY-MM-DD/<MMDD>_<HHMM>_<セッションID>/` に `audio.wav` / `transcript.md` / `meta.json` / `chat.md`
- LLM 設定は環境変数 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`(OpenAI 互換 Chat Completions)
- `glossary.txt` を `initial_prompt` に注入。約224トークン超過時は先頭から使い警告ログ
- WAV は逐次書き込み(クラッシュしても途中まで有効なファイルが残る)
- 準リアルタイム要約の間隔は既定 180 秒、環境変数 `SUMMARY_INTERVAL_SEC` で変更可

## Review Focus

- マイクデバイスが取れない/選べない環境での起動 → 明確なエラーメッセージで起動は継続(Task 7 にテスト)
- 録音中の LLM API 失敗・タイムアウト → 静かにスキップし字幕継続(Task 5/6 にテスト)
- セッションIDにファイル名不正文字(`/`, `:` 等) → サニタイズして保存成功(Task 2 にテスト)
- セッション0件の日の日報生成 → 空日報を正常生成(Task 6 にテスト)
- glossary.txt が無い/空/超過 → それぞれ起動継続・警告(Task 4 にテスト)

---

### Task 0: Git リポジトリ初期化

- [ ] `reinvent-scribe/` で `git init`、Python 用 `.gitignore`(venv, __pycache__, data/, モデルキャッシュ)を作成し initial commit(既存の docs/ も含める)

### Task 1: プロジェクト雛形と設定

**Files:**
- Create: `reinvent-scribe/pyproject.toml`, `reinvent_scribe/__init__.py`, `reinvent_scribe/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` dataclass — `data_dir: Path`, `whisper_model: str` (既定 "small.en"), `llm_base_url: str|None`, `llm_api_key: str|None`, `llm_model: str|None`, `summary_interval_sec: int` (既定 180)。`Settings.from_env() -> Settings`

- [ ] **Step 1: 失敗するテストを書く** — `test_from_env_defaults`(環境変数なし→既定値、`llm_base_url is None`)、`test_from_env_overrides`(monkeypatch で全変数設定→反映)
- [ ] **Step 2: `pytest tests/test_config.py -v` で FAIL を確認**
- [ ] **Step 3: `config.py` に `Settings` と `from_env()` を実装。pyproject.toml に依存(fastapi, uvicorn, sounddevice, faster-whisper, httpx, numpy)と pytest 設定**
- [ ] **Step 4: テスト PASS を確認**
- [ ] **Step 5: Commit** `feat: project scaffold and settings`

### Task 2: セッションストア

**Files:**
- Create: `reinvent_scribe/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `SessionStore(data_dir: Path)` — `create(session_id: str, title: str, memo: str = "") -> Session`; `list_for_date(date: str) -> list[Session]`; `get(dirname: str) -> Session`
- `Session` — `.dir: Path`, `.meta: dict`(id, title, memo, started_at, ended_at), `.append_segment(ts_sec: float, text: str)`(transcript.md に `[HH:MM:SS] text` 追記、ヘッダは作成時に書く), `.update_memo(memo: str)`, `.finalize()`(ended_at 記録), `.transcript_text() -> str`, `.append_chat(role: str, text: str)`

- [ ] **Step 1: 失敗するテストを書く** — `test_create_builds_dir_and_meta`(`1201_0830_KEY001` 形式のディレクトリ名、meta.json 内容)、`test_sanitizes_session_id`(`"KEY/0:01"` → 不正文字が `_` に)、`test_append_segment_and_transcript_text`、`test_update_memo_mutates_meta`、`test_list_for_date`
- [ ] **Step 2: FAIL 確認**
- [ ] **Step 3: `store.py` 実装。ディレクトリ名の日時はセッション開始時刻(ローカル時刻)から生成**
- [ ] **Step 4: PASS 確認**
- [ ] **Step 5: Commit** `feat: session store with transcript/meta/chat persistence`

### Task 3: 録音(逐次 WAV 書き込み)とマイク入力

**Files:**
- Create: `reinvent_scribe/audio.py`
- Test: `tests/test_audio.py`

**Interfaces:**
- Produces: `WavWriter(path: Path, samplerate: int = 16000)` — `.write(frames: np.ndarray)`(int16 mono、呼び出しごとにヘッダ更新しクラッシュ耐性確保), `.close()`; `MicStream(on_frames: Callable[[np.ndarray], None], samplerate: int = 16000)` — `.start()`, `.stop()`(sounddevice.InputStream の薄いラッパ); `list_devices() -> list[str]`
- Consumes: なし

- [ ] **Step 1: 失敗するテストを書く** — `test_wav_written_incrementally`(数回 write → close せずにファイルを別途開いても有効な WAV として読める)、`test_close_finalizes`(wave モジュールで読めてフレーム数一致)。MicStream は実マイク不要のため on_frames への配線のみモックでテスト
- [ ] **Step 2: FAIL 確認**
- [ ] **Step 3: `audio.py` 実装(WavWriter は wave モジュール + 追記のたびに seek してサイズフィールド更新、または raw 追記+ヘッダ書き直し。手段は任意、テストが通ればよい)**
- [ ] **Step 4: PASS 確認**
- [ ] **Step 5: Commit** `feat: crash-safe incremental WAV writer and mic stream`

### Task 4: 文字起こしエンジン

**Files:**
- Create: `reinvent_scribe/transcribe.py`, `glossary.txt`(汎用 AWS 用語の初期リスト: サービス名 50〜80語程度)
- Test: `tests/test_transcribe.py`

**Interfaces:**
- Produces: `load_glossary(path: Path) -> tuple[str, bool]`(プロンプト文字列, 切り詰めたか); `transcribe_file(wav_path: Path, model_name: str, initial_prompt: str) -> list[tuple[float, str]]`(保存 WAV の一括高精度文字起こし。2パス目用、既定モデル "large-v3-turbo"); `Transcriber(model_name: str, initial_prompt: str, on_segment: Callable[[float, str], None])` — `.feed(pcm: np.ndarray)`(16kHz float32 を内部バッファし、VAD 区切りで transcribe して on_segment(開始秒, テキスト) を呼ぶ), `.flush()`, `.stop()`; `pick_model(preferred: str = "small.en") -> str`(短い無音でベンチし実時間比 1x 未満なら "base.en")
- Consumes: Task 1 `Settings.whisper_model`

- [ ] **Step 1: 失敗するテストを書く** — `test_load_glossary_missing_returns_empty`、`test_load_glossary_truncates_long`(長文→切り詰めフラグ True)、`test_transcriber_emits_segments`(WhisperModel をモックし、feed→flush で on_segment が呼ばれる)、`test_transcribe_file_returns_segments`(モデルをモックし WAV パス→セグメントリスト)
- [ ] **Step 2: FAIL 確認**
- [ ] **Step 3: `transcribe.py` 実装。WhisperModel はコンストラクタ注入可能にしてテストでモック。トークン概算は 1 語 ≈ 1.3 トークンで良い。glossary.txt 同梱**
- [ ] **Step 4: PASS 確認**
- [ ] **Step 5: Commit** `feat: streaming transcriber with glossary prompt and model fallback`

### Task 5: LLM クライアントとチャット

**Files:**
- Create: `reinvent_scribe/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `LLMClient(settings: Settings)` — `.available() -> bool`(base_url と key が設定済みか), `.chat(messages: list[dict], max_tokens: int = 1000) -> str | None`(OpenAI 互換 `/chat/completions` を httpx で呼ぶ。例外・非200・タイムアウト(15s)は None); `transcript_chat(llm, transcript: str, history: list[dict], question: str) -> str | None`(transcript が長い場合は先頭 2000 字+直近 6000 字を文脈に)
- Consumes: Task 1 `Settings`

- [ ] **Step 1: 失敗するテストを書く** — `test_available_false_without_config`、`test_chat_returns_content`(httpx をモック)、`test_chat_returns_none_on_error`(500/タイムアウト→None、例外を外に漏らさない)、`test_transcript_chat_truncates_long_transcript`
- [ ] **Step 2: FAIL 確認**
- [ ] **Step 3: `llm.py` 実装**
- [ ] **Step 4: PASS 確認**
- [ ] **Step 5: Commit** `feat: provider-agnostic LLM client and transcript chat`

### Task 6: 準リアルタイム要約と出張日報

**Files:**
- Create: `reinvent_scribe/summarize.py`
- Test: `tests/test_summarize.py`

**Interfaces:**
- Produces: `RollingSummarizer(llm: LLMClient, interval_sec: int)` — `.add(text: str)`, `.due(now: float) -> bool`, `.summarize() -> str | None`(未要約分+これまでの要約を渡し、累積日本語要約を返す。llm 不可/失敗なら None で静かにスキップ); `build_daily_report(sessions: list[Session], llm: LLMClient) -> str`(日報 Markdown: 行動概要=セッション一覧表 → 各セッション要点 2〜4 行(LLM、ブログ記事調で読める文体) → 所感(メモ集約)。llm 不可ならメタ情報+メモのみ。セッション 0 件でも「本日の出席セッションはありません」を含む有効な Markdown)
- Consumes: Task 2 `Session`, Task 5 `LLMClient`

- [ ] **Step 1: 失敗するテストを書く** — `test_due_respects_interval`、`test_summarize_skips_when_unavailable`(None、例外なし)、`test_daily_report_offline_fallback`(llm 不可→メタ+メモのみ)、`test_daily_report_empty_day`
- [ ] **Step 2: FAIL 確認**
- [ ] **Step 3: `summarize.py` 実装。プロンプトは日本語で「上司への出張日報」「ブログ記事のように内容と重要な点がわかる」ことを明記**
- [ ] **Step 4: PASS 確認**
- [ ] **Step 5: Commit** `feat: rolling JP summary and daily trip report`

### Task 7: FastAPI サーバと録音オーケストレーション

**Files:**
- Create: `reinvent_scribe/server.py`, `reinvent_scribe/__main__.py`(`python -m reinvent_scribe` で uvicorn 起動+`webbrowser.open` でブラウザ自動オープン、`--no-browser` で抑止)
- Test: `tests/test_server.py`(FastAPI TestClient、audio/transcriber はフェイク注入)

**Interfaces:**
- Consumes: Task 2〜6 の全インターフェース
- Produces: HTTP API —
  - `POST /api/sessions` {session_id, title, memo} → 録音+文字起こし開始、Session 情報を返す(多重開始は 409)
  - `POST /api/sessions/stop` → flush・finalize・WAV close。その後バックグラウンドで `transcribe_file` による高精度再文字起こしを実行し `transcript_hq.md` を保存(進行状況は `GET /api/status` の `hq_pending: list[str]` で確認可。失敗しても録音済みデータに影響なし)
  - `GET /api/schedule?now=` → `sessions.csv`(日付,開始時刻,ID,タイトル。リポジトリ直下、無ければ空)から現在時刻±30分で該当するセッション候補を返す {suggestions: [{session_id, title}]}
  - `PATCH /api/sessions/{dirname}/memo` {memo}
  - `GET /api/sessions?date=YYYY-MM-DD` → 一覧
  - `POST /api/sessions/{dirname}/chat` {question} → {answer}(chat.md に追記。LLM 不可は 503 + 日本語メッセージ)
  - `POST /api/daily-report?date=` → {markdown}(`daily_summary.md` 保存)
  - `GET /api/status` → {recording: bool, mic_ok: bool, llm_ok: bool, model: str}
  - `WS /ws` → `{type: "segment", ts, text}` と `{type: "summary", text}` を push
  - `GET /` → static/index.html
- 録音ループ: MicStream → WavWriter.write + Transcriber.feed。on_segment → Session.append_segment + WS 配信 + RollingSummarizer.add。要約は録音スレッドと分離(asyncio タスク)し、失敗しても録音継続
- マイク初期化失敗時: サーバは起動し `GET /api/status` の mic_ok=false、`POST /api/sessions` は 503 + 「マイクが見つかりません」

- [ ] **Step 1: 失敗するテストを書く** — `test_start_stop_session_writes_files`、`test_stop_triggers_hq_transcription`(transcribe_file をフェイクし transcript_hq.md 生成)、`test_schedule_suggests_current_session`(CSV フィクスチャ+now 指定)、`test_double_start_409`、`test_memo_patch`、`test_chat_without_llm_503`、`test_daily_report_endpoint`、`test_status_reports_mic_failure`(フェイク MicStream が raise)
- [ ] **Step 2: FAIL 確認**
- [ ] **Step 3: `server.py` 実装。依存(MicStream/Transcriber/LLMClient)はアプリ state に注入可能なファクトリにしてテストで差し替え**
- [ ] **Step 4: PASS 確認**
- [ ] **Step 5: Commit** `feat: FastAPI server wiring recording, captions, summary, chat`

### Task 8: フロントエンド UI

**Files:**
- Create: `reinvent_scribe/static/index.html`, `static/app.js`, `static/style.css`

**Interfaces:**
- Consumes: Task 7 の HTTP/WS API(URL・ペイロードは Task 7 の定義どおり)

仕様: 単一ページ。ダーク基調のグラスモーフィズム+グラデーション、録音中はマイクボタンのパルス+簡易波形(CSS/Canvas)、字幕セグメントはフェード/スライドインで追記、右サイドに日本語要約ペイン(カード積み上げ、スライドイン)。下部にチャットフォーム(送信中はタイピングインジケータ)。セッション開始モーダル(ID/タイトル/メモ。起動時に `GET /api/schedule` の候補をプリフィルし、候補があれば「今のセッションを開始」1クリックボタンを表示)、メモはいつでも編集可、本日のセッション一覧と「日報生成」ボタン(結果はモーダル+コピーしボタン)。外部 CDN に依存しない(フォント・ライブラリは同梱か system)。LLM 未設定時は要約ペインに「オフライン(字幕のみ)」バッジ。

- [ ] **Step 1: 実装(index.html / app.js / style.css)**
- [ ] **Step 2: 手動確認** — `python -m reinvent_scribe` を起動しブラウザで: 開始→字幕が流れる→停止→ファイル生成、を確認(マイクがない CI 環境ではスキップ可)
- [ ] **Step 3: Commit** `feat: animated single-page UI`

### Task 9: README とプロンプト例・仕上げ

**Files:**
- Create: `README.md`, `prompts/nightly_article.md`(夜に Claude へ渡すブログ記事調生成プロンプト例)
- Test: 全テストスイート

- [ ] **Step 1: README 作成** — Mac/Windows それぞれの clone→venv→pip install→起動手順、環境変数(LLM 3種+SUMMARY_INTERVAL_SEC)、**出発前に一度起動してモデルをダウンロードしておく**こと、glossary.txt の編集方法、`sessions.csv` の書き方(1クリック開始用)、2パス文字起こしの説明(夜は transcript_hq.md を Claude に渡す)、データの場所、日報運用(生成→コピー→Slack)
- [ ] **Step 2: `pytest -v` 全体 PASS を確認**
- [ ] **Step 3: 実機スモークテスト** — 実マイクで 1 分録音し、WAV・transcript.md が正しく残ることを確認
- [ ] **Step 4: Commit** `docs: README, nightly article prompt, final polish`
