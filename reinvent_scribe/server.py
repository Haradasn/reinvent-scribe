import asyncio
import logging
import queue
import re
import shutil
import smtplib
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio import LevelMeter, MicStream, WavWriter
from .config import Settings
from .mail import build_message, send_gmail
from .photos import MAX_BYTES, PhotoStore, read_photo, span, suggest
from .schedule import load_schedule, save_schedule
from .llm import LLMClient, transcript_chat
from .store import SessionStore
from .summarize import RollingSummarizer, build_daily_report, build_slack_post, summarize_whole
from .transcribe import Transcriber, load_glossary, transcribe_file

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SCHEDULE_CSV = Path("sessions.csv")
GLOSSARY = Path("glossary.txt")
RECONNECT_SEC = 10
END_UTTERANCE = object()  # queue marker: finalize captions on pause


class StartBody(BaseModel):
    session_id: str = ""
    title: str = ""
    schedule_key: str | None = None
    memo: str = ""


class MemoBody(BaseModel):
    memo: str


class ReportBody(BaseModel):
    mention: str = ""


class MailBody(BaseModel):
    to: str
    subject: str
    body: str
    attach_transcript: bool = False
    photos: list[dict] = []  # {date, dirname, id}


class CommitBody(BaseModel):
    items: list[dict]  # {id, date, dirname}


class ShiftBody(BaseModel):
    seconds: float


class MoveBody(BaseModel):
    date: str
    dirname: str




class ScheduleItem(BaseModel):
    month: int
    day: int
    start: str
    end: str
    title: str
    venue: str = ""
    kind: str = "reserved"


class ScheduleBody(BaseModel):
    items: list[ScheduleItem]
    force: bool = False


class ChatBody(BaseModel):
    question: str


class Recording:
    def __init__(self, session, mic, engine, wav, summarizer,
                 frame_queue, worker):
        self.session = session
        self.mic = mic
        self.engine = engine  # {"transcriber": ..., "streamer": ...}
        self.wav = wav
        self.summarizer = summarizer
        self.frame_queue = frame_queue
        self.worker = worker
        self.paused = False
        self.jp_queue = None
        self.jp_worker = None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_date(date: str) -> None:
    if not _DATE_RE.match(date):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="invalid date")


def create_app(settings: Settings | None = None, mic_factory=MicStream,
               transcriber_factory=Transcriber,
               transcribe_file_fn=transcribe_file, llm: LLMClient | None = None,
               model_name: str | None = None, streamer_factory="auto",
               event_now=None, mail_sender=send_gmail) -> FastAPI:
    settings = settings or Settings.from_env()
    if event_now is None:
        tz = ZoneInfo(settings.event_tz)

        def event_now():
            return datetime.now(tz).replace(tzinfo=None)
    if streamer_factory == "auto":
        if settings.assemblyai_api_key:
            from .streaming import AssemblyAIStreamer
            key = settings.assemblyai_api_key

            def streamer_factory(on_segment, on_partial):
                return AssemblyAIStreamer(key, on_segment=on_segment,
                                          on_partial=on_partial)
        else:
            streamer_factory = None
    # reports read whole sessions, so they get a longer timeout than captions
    report_llm = llm or LLMClient(settings, timeout=90.0)
    llm = llm or LLMClient(settings)
    model = model_name or settings.whisper_model
    store = SessionStore(settings.data_dir)
    photos = PhotoStore(settings.data_dir, settings.event_tz)
    glossary_prompt, _ = load_glossary(GLOSSARY)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        loop_holder["loop"] = asyncio.get_running_loop()
        yield

    app = FastAPI(lifespan=lifespan)
    # this app can send mail: refuse requests that reach it via another name
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    state = {"recording": None, "mic_ok": True, "hq_pending": []}
    ws_clients: set[WebSocket] = set()
    loop_holder = {}
    lock = threading.Lock()

    def broadcast(payload: dict) -> None:
        loop = loop_holder.get("loop")
        if loop is None:
            return
        for ws in list(ws_clients):
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)

    @app.post("/api/sessions")
    def start_session(body: StartBody):
        with lock:
            if state["recording"] is not None:
                return JSONResponse({"error": "already recording"}, status_code=409)
            session_id, title, extra = body.session_id, body.title, {}
            if body.schedule_key:
                item = next((i for i in load_schedule(SCHEDULE_CSV)
                             if i["key"] == body.schedule_key), None)
                if item is None:
                    return JSONResponse({"error": "予定が見つかりません。画面を再読み込みしてください。"},
                                        status_code=404)
                session_id, title = item["id"], item["title"]
                extra = {"venue": item["venue"], "planned_start": item["start"],
                         "planned_end": item["end"]}
                if item["date"] == f"{event_now():%Y-%m-%d}":
                    extra.update(schedule_key=item["key"],
                                 schedule_date=item["date"])
                else:  # rehearsal: must not mark the real session as recorded
                    extra = {"venue": item["venue"], "test": True}
            if not session_id or not title:
                return JSONResponse({"error": "IDとタイトルを入力してください。"},
                                    status_code=422)
            session = store.create(session_id, title, body.memo, extra=extra)
            wav = WavWriter(session.dir / "audio.wav")
            summarizer = RollingSummarizer(llm, settings.summary_interval_sec)

            # Per-segment Japanese translation: a single worker keeps order
            # and its failures never touch captions or recording.
            jp_queue: queue.Queue = queue.Queue()
            recent: list[str] = []

            def _jp_worker() -> None:
                retry: list = []
                while True:
                    try:
                        item = jp_queue.get(timeout=3 if retry else None)
                    except queue.Empty:  # idle: revisit what failed earlier
                        item = retry.pop(0)
                    if item is None:
                        for left in retry:
                            _translate(*left[:2], last=True)
                        return
                    ts, text, tries = item
                    if not _translate(ts, text, last=tries >= 4) and tries < 4:
                        # an outage is not the caption's fault: keep it waiting
                        offline = getattr(llm, "last_error", None) == "network"
                        retry.append((ts, text, tries + (not offline)))

            def _previous(text) -> str:
                i = recent.index(text) if text in recent else 0
                return ("\n\n## 直前の発言(参考。訳さない)\n" + recent[i - 1][-300:]
                        if i else "")

            def _translate(ts, text, last) -> bool:
                try:
                    out = llm.chat([{"role": "user", "content":
                                     "次の発言を自然な日本語に翻訳してください。"
                                     "すでに日本語ならそのまま返すこと。"
                                     "訳文のみを返すこと。"
                                     "発言は文の途中で始まったり切れたりする。原文にない内容で"
                                     "補わず、切れた所までを訳すこと。固有名詞と技術用語以外は日本語にする。"
                                     + _previous(text) + "\n\n## 訳す発言\n" + text}],
                                   max_tokens=1000)
                    if out:
                        # one line: it may be saved after later captions
                        out = " ".join(out.split("\n")).replace("】【", "").strip()
                        session.append_segment(ts, "(JA) " + out)
                        broadcast({"type": "jp", "ts": ts, "text": out,
                                   "dirname": session.dir.name})
                        return True
                except Exception:
                    log.exception("translation failed")
                if last:
                    broadcast({"type": "jp_failed", "ts": ts,
                               "dirname": session.dir.name})
                return False

            jp_worker = None
            if llm.available():
                jp_worker = threading.Thread(target=_jp_worker, daemon=True,
                                             name="jp-translate")
                jp_worker.start()

            def on_segment(ts: float, text: str) -> None:
                session.append_segment(ts, text)
                summarizer.add(text)
                broadcast({"type": "segment", "ts": ts, "text": text,
                           "translating": jp_worker is not None})
                if jp_worker is not None:
                    recent.append(text)
                    jp_queue.put((ts, text, 0))
                if summarizer.due():
                    threading.Thread(target=_run_summary, daemon=True).start()

            def _run_summary() -> None:
                out = summarizer.summarize()
                if out:
                    try:
                        (session.dir / "summary.md").write_text(
                            out, encoding="utf-8")
                    except Exception:
                        log.exception("summary save failed")
                    broadcast({"type": "summary", "text": out,
                               "dirname": session.dir.name})

            engine = {"transcriber": None, "streamer": None, "pending": None}
            # positions in the recorded audio, in samples
            pos = {"fed": 0, "local_fed": 0, "local_from": 0.0}
            unconfirmed: deque = deque()  # (start, frames) not in captions yet
            stopping = threading.Event()

            def _local_transcriber():
                if engine["transcriber"] is None:
                    engine["transcriber"] = transcriber_factory(
                        model_name=model, initial_prompt=glossary_prompt,
                        on_segment=lambda ts, t: on_segment(ts + pos["local_from"], t))
                return engine["transcriber"]

            def _open_stream():
                base = {"sec": 0.0}
                streamer = streamer_factory(
                    on_segment=lambda ts, t: on_segment(ts + base["sec"], t),
                    on_partial=lambda t: broadcast({"type": "partial", "text": t}))
                streamer.start()
                return streamer, base

            def _use_stream(streamer, base) -> None:
                base["sec"] = pos["fed"] / 16000
                engine["base"] = base["sec"]
                engine["streamer"] = streamer
                unconfirmed.clear()

            def _feed_local(start: int, frames) -> None:
                # the transcriber keeps its own clock across takeovers
                if pos.get("local_next") != start:
                    if engine["transcriber"] is not None:
                        engine["transcriber"].flush()
                    _local_transcriber()
                    pos["local_from"] = (start - pos["local_fed"]) / 16000
                _local_transcriber().feed(frames.astype(np.float32) / 32768.0)
                pos["local_fed"] += len(frames)
                pos["local_next"] = start + len(frames)

            def _reconnect() -> None:
                while not stopping.wait(RECONNECT_SEC):
                    if rec_ref and rec_ref[0].paused:
                        continue
                    try:
                        fresh = _open_stream()
                    except Exception:
                        continue
                    with lock:
                        if not stopping.is_set():
                            engine["pending"] = fresh
                            return
                    fresh[0].stop()
                    return

            rec_ref: list = []

            def _start_reconnect() -> None:
                if not stopping.is_set():
                    threading.Thread(target=_reconnect, daemon=True,
                                     name="stt-reconnect").start()

            def _to_local() -> None:
                dead, engine["streamer"] = engine["streamer"], None
                done = engine["base"] + getattr(dead, "confirmed_sec", 0.0)
                threading.Thread(target=lambda: _quiet(dead.stop), daemon=True).start()
                broadcast({"type": "stt_fallback"})
                _start_reconnect()
                # what was sent but never came back as a caption
                for start, old in list(unconfirmed):
                    if (start + len(old)) / 16000 > done:
                        _feed_local(start, old)
                unconfirmed.clear()

            def _quiet(fn) -> None:
                try:
                    fn()
                except Exception:
                    log.info("closing a dead stream failed", exc_info=True)

            if streamer_factory is not None:
                try:
                    _use_stream(*_open_stream())
                    log.info("using streaming STT (AssemblyAI)")
                except Exception:
                    log.warning("streaming STT unavailable; using local whisper",
                                exc_info=True)
                    _start_reconnect()
            if engine["streamer"] is None:
                _local_transcriber()

            # The audio callback must stay fast: it only writes the WAV and
            # enqueues PCM. Transcription (local inference or network send)
            # runs on a dedicated worker thread so it can never stall capture.
            frame_queue: queue.Queue = queue.Queue()
            meter = LevelMeter()

            def on_frames(frames: np.ndarray) -> None:
                try:
                    wav.write(frames)
                    frame_queue.put(frames)
                except Exception:
                    log.exception("frame handling failed")
                try:  # display only: must never affect the recording
                    level = meter.feed(frames)
                    if level:
                        broadcast({"type": "level", **level})
                except Exception:
                    log.debug("level meter failed", exc_info=True)

            def _worker() -> None:
                while True:
                    frames = frame_queue.get()
                    if frames is None:
                        return
                    if frames is END_UTTERANCE:
                        try:
                            if engine["streamer"] is not None:
                                engine["streamer"].end_utterance()
                            elif engine["transcriber"] is not None:
                                engine["transcriber"].flush()
                        except Exception:
                            log.warning("could not finalize utterance", exc_info=True)
                        continue
                    try:
                        start = pos["fed"]
                        if engine["pending"] is not None:
                            fresh, engine["pending"] = engine["pending"], None
                            if engine["transcriber"] is not None:
                                engine["transcriber"].flush()
                            _use_stream(*fresh)
                            log.info("streaming STT restored")
                            broadcast({"type": "stt_restored"})
                        pos["fed"] += len(frames)
                        if engine["streamer"] is None:
                            _feed_local(start, frames)
                            continue
                        unconfirmed.append((start, frames))
                        done = (engine["base"] + getattr(
                            engine["streamer"], "confirmed_sec", 0.0)) * 16000
                        while unconfirmed and (
                                unconfirmed[0][0] + len(unconfirmed[0][1]) <= done
                                or start - unconfirmed[0][0] > 16000 * 120):
                            unconfirmed.popleft()
                        try:
                            engine["streamer"].feed(frames)
                        except Exception:
                            log.warning("stream lost; falling back to local whisper",
                                        exc_info=True)
                            _to_local()
                    except Exception:
                        log.exception("transcription failed; continuing")

            worker = threading.Thread(target=_worker, daemon=True,
                                      name="transcribe-worker")
            mic = mic_factory(on_frames=on_frames)
            try:
                mic.start()
            except Exception:
                log.exception("mic start failed")
                state["mic_ok"] = False
                wav.close()
                stopping.set()
                for part in (engine["streamer"], (engine["pending"] or [None])[0]):
                    if part is not None:  # billed while open
                        _quiet(part.stop)
                shutil.rmtree(session.dir, ignore_errors=True)
                return JSONResponse(
                    {"error": "マイクが見つかりません。入力デバイスを確認してください。"},
                    status_code=503)
            session.update_meta(started_epoch=time.time(), pauses=[])
            worker.start()
            state["mic_ok"] = True
            rec = Recording(session, mic, engine, wav,
                            summarizer, frame_queue, worker)
            rec.jp_queue, rec.jp_worker = jp_queue, jp_worker
            rec.stopping = stopping
            rec_ref.append(rec)
            state["recording"] = rec
            date = session.meta["started_at"][:10]
            return {"dirname": session.dir.name, "date": date,
                    "meta": session.meta}

    def _end_pause(rec) -> None:
        pauses = [list(p) for p in rec.session.meta.get("pauses", [])]
        if pauses and pauses[-1][1] is None:
            pauses[-1][1] = time.time()
            rec.session.update_meta(pauses=pauses)

    @app.post("/api/sessions/stop")
    def stop_session():
        with lock:
            rec: Recording | None = state["recording"]
            if rec is None:
                return JSONResponse({"error": "not recording"}, status_code=409)
            state["recording"] = None
            rec.stopping.set()
        # Every step runs even if an earlier one fails, so the WAV is always
        # closed and the (billed) streaming connection is always released.
        def _step(name, fn):
            try:
                fn()
            except Exception:
                log.exception("stop step failed: %s", name)

        if rec.paused:
            _step("pause time", lambda: _end_pause(rec))
        else:
            _step("mic", rec.mic.stop)
        rec.frame_queue.put(None)
        rec.worker.join(timeout=30)
        # engines first: their final segments still need translating
        if rec.engine["pending"] is not None:
            _step("pending", rec.engine["pending"][0].stop)
        for name in ("transcriber", "streamer"):
            part = rec.engine[name]
            if part is not None:
                _step(name, part.stop)
        if rec.jp_worker is not None:
            rec.jp_queue.put(None)  # drained in the background
        _step("wav", rec.wav.close)
        rec.session.finalize()
        state["hq_pending"].append(rec.session.dir.name)

        def _hq():
            # translating and summarizing wait on the network: never delay stop
            try:
                if rec.jp_worker is not None:
                    rec.jp_worker.join(timeout=60)
                tail = rec.summarizer.summarize()
                if tail:
                    broadcast({"type": "summary", "text": tail,
                               "dirname": rec.session.dir.name})
                if rec.summarizer.current:
                    (rec.session.dir / "summary.md").write_text(
                        rec.summarizer.current, encoding="utf-8")
            except Exception:
                log.exception("summary persistence failed")
            try:
                segs = transcribe_file_fn(rec.session.dir / "audio.wav",
                                          initial_prompt=glossary_prompt)
                meta = rec.session.meta
                lines = [f"# [{meta['id']}] {meta['title']} (high quality)", ""]
                from .store import _fmt_ts
                lines += [f"[{_fmt_ts(ts)}] {text}" for ts, text in segs]
                (rec.session.dir / "transcript_hq.md").write_text(
                    "\n".join(lines), encoding="utf-8")
            except Exception:
                log.exception("HQ transcription failed")
            finally:
                state["hq_pending"].remove(rec.session.dir.name)
                broadcast({"type": "hq_done", "dirname": rec.session.dir.name})

        threading.Thread(target=_hq, daemon=True).start()
        return {"dirname": rec.session.dir.name}

    def _get_session(date: str, dirname: str):
        _check_date(date)
        try:
            return store.get(date, Path(dirname).name)
        except FileNotFoundError:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="session not found")

    @app.post("/api/sessions/pause")
    def pause_session():
        rec: Recording | None = state["recording"]
        if rec is None or rec.paused:
            return JSONResponse({"error": "not recording or already paused"},
                                status_code=409)
        rec.mic.stop()
        rec.paused = True
        try:
            rec.session.update_meta(pauses=[*rec.session.meta.get("pauses", []),
                                            [time.time(), None]])
        except OSError:
            log.exception("pause time not saved")
        rec.frame_queue.put(END_UTTERANCE)
        return {"ok": True}

    @app.post("/api/sessions/resume")
    def resume_session():
        rec: Recording | None = state["recording"]
        if rec is None or not rec.paused:
            return JSONResponse({"error": "not paused"}, status_code=409)
        try:
            rec.mic.start()
        except Exception:
            log.exception("mic restart failed")
            return JSONResponse({"error": "マイクの再開に失敗しました"},
                                status_code=503)
        rec.paused = False
        try:
            _end_pause(rec)
        except OSError:
            log.exception("resume time not saved")
        return {"ok": True}

    @app.patch("/api/sessions/{date}/{dirname}/memo")
    def patch_memo(date: str, dirname: str, body: MemoBody):
        session = _get_session(date, dirname)
        session.update_memo(body.memo)
        return {"ok": True}

    @app.delete("/api/sessions/{date}/{dirname}")
    def delete_session(date: str, dirname: str):
        session = _get_session(date, dirname)
        rec: Recording | None = state["recording"]
        if rec is not None and rec.session.dir == session.dir:
            return JSONResponse({"error": "録音中のセッションは削除できません"},
                                status_code=409)
        if session.dir.name in state["hq_pending"]:
            return JSONResponse({"error": "高精度版の作成が終わってから削除してください"},
                                status_code=409)
        store.trash(session)
        return {"ok": True}

    @app.get("/api/trash")
    def list_trash():
        return {"items": [{"date": s.dir.parent.name, "dirname": s.dir.name,
                           "meta": s.meta} for s in store.trashed()]}

    def _trashed(date: str, dirname: str):
        _check_date(date)
        for s in store.trashed():
            if s.dir.parent.name == date and s.dir.name == dirname:
                return s
        raise HTTPException(status_code=404, detail="not in trash")

    @app.post("/api/trash/{date}/{dirname}/restore")
    def restore_session(date: str, dirname: str):
        return {"dirname": store.restore(_trashed(date, dirname)).name}

    @app.delete("/api/trash/{date}/{dirname}")
    def purge_session(date: str, dirname: str):
        shutil.rmtree(_trashed(date, dirname).dir)
        return {"ok": True}

    @app.get("/api/recording-days")
    def recording_days():
        days: dict[str, int] = {}
        for s in store.all():
            d = s.meta.get("schedule_date") or s.dir.parent.name
            days[d] = days.get(d, 0) + 1
        return {"days": days}

    @app.get("/api/sessions")
    def list_sessions(date: str):
        _check_date(date)
        sessions = store.list_for_date(date)
        return {"sessions": [
            {"dirname": s.dir.name, "meta": s.meta,
             "photos": len(photos.load(photos.folder(s))),
             "summary": (s.dir / "summary.md").exists(),
             "hq": (s.dir / "transcript_hq.md").exists()} for s in sessions]}

    @app.post("/api/sessions/{date}/{dirname}/chat")
    def chat(date: str, dirname: str, body: ChatBody):
        session = _get_session(date, dirname)
        if not llm.available():
            return JSONResponse(
                {"error": "LLM が未設定です。LLM_BASE_URL / LLM_API_KEY / LLM_MODEL を設定してください。"},
                status_code=503)
        transcript = session.best_transcript() + photos.slide_text(session)
        answer = transcript_chat(llm, transcript, history=session.chat_history()[-20:],
                                 question=body.question)
        if answer is None:
            return JSONResponse({"error": "AIサービスの残高が不足しています。" if getattr(llm, "last_error", None) == "payment"
                                 else "AIサービスが混雑しているか、接続できません。もう一度送信してください。"},
                                status_code=502)
        session.append_chat("user", body.question)
        session.append_chat("assistant", answer)
        return {"answer": answer}

    @app.post("/api/sessions/{date}/{dirname}/web-search")
    def web_search(date: str, dirname: str, body: ChatBody):
        session = _get_session(date, dirname)
        if not getattr(llm, "can_search", lambda: False)():
            return JSONResponse({"error": "Web検索には OpenAI の設定が必要です。"}, status_code=503)
        found = llm.web_search(body.question)
        if found is None:
            return JSONResponse({"error": "Web検索に失敗しました。もう一度お試しください。"},
                                status_code=502)
        text, sources = found
        lines = [text] + (["", "出典:"] + [f"{x['title']}\n{x['url']}" for x in sources]
                          if sources else ["", "(出典なし)"])
        answer = "\n".join(lines)
        session.append_chat("web", answer)
        return {"answer": answer}

    @app.post("/api/daily-report")
    def daily_report(date: str, body: ReportBody | None = None):
        _check_date(date)
        sessions = store.list_for_date(date)
        md = build_daily_report(sessions, report_llm, photos.slide_text)
        slack = build_slack_post(sessions, report_llm, md,
                                 body.mention if body else "")
        day_dir = settings.data_dir / date
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "daily_summary.md").write_text(md, encoding="utf-8")
        (day_dir / "daily_slack.txt").write_text(slack, encoding="utf-8")
        return {"markdown": md, "slack": slack}

    @app.post("/api/daily-report/send")
    def send_report(date: str, body: MailBody):
        _check_date(date)
        return _send_mail(body, store.list_for_date(date))

    @app.post("/api/sessions/{date}/{dirname}/send-summary")
    def send_summary(date: str, dirname: str, body: MailBody):
        return _send_mail(body, [_get_session(date, dirname)])

    def _send_mail(body: MailBody, sessions: list):
        if not (settings.gmail_address and settings.gmail_app_password):
            return JSONResponse({"error": ".env に GMAIL_ADDRESS と GMAIL_APP_PASSWORD を設定し、アプリを再起動してください。"},
                                status_code=503)
        if not body.body.strip():
            return JSONResponse({"error": "本文がありません。"}, status_code=422)
        files = []
        if body.attach_transcript:
            files = [(f"{s.dir.name}.txt", s.transcript_export())
                     for s in sessions]
        images, total = [], 0
        for i, p in enumerate(body.photos, 1):
            path = _photo(photos.folder(_get_session(str(p.get("date")), str(p.get("dirname")))),
                          str(p.get("id")))
            images.append((f"{Path(str(p.get('dirname'))).name}_{i:02d}.jpg", path.read_bytes()))
            total += len(images[-1][1])
        if total > 15_000_000:
            return JSONResponse({"error": "写真の合計が大きすぎます(15MBまで)。枚数を減らしてください。"},
                                status_code=422)
        try:
            msg = build_message(settings.gmail_address, body.to, body.subject,
                                body.body, files, images)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        try:
            mail_sender(settings.gmail_address, settings.gmail_app_password, msg)
        except smtplib.SMTPAuthenticationError:
            return JSONResponse({"error": "Gmail にログインできません(未送信)。アドレスとアプリパスワードを確認してください。"},
                                status_code=502)
        except Exception as e:
            log.warning("mail send failed: %s", type(e).__name__)
            return JSONResponse({"error": "送信できたか確認できません。Gmail の送信済みを確認してください。"},
                                status_code=502)
        return {"ok": True, "attachments": len(files) + len(images)}

    @app.get("/api/sessions/{date}/{dirname}/audio")
    def download_audio(date: str, dirname: str):
        s = _get_session(date, dirname)
        rec: Recording | None = state["recording"]
        if rec is not None and rec.session.dir == s.dir:
            return JSONResponse({"error": "録音を停止してからダウンロードしてください"},
                                status_code=409)
        if not (s.dir / "audio.wav").exists():
            return JSONResponse({"error": "音声がありません"}, status_code=404)
        return FileResponse(s.dir / "audio.wav", media_type="audio/wav",
                            filename=f"{s.dir.name}.wav")

    @app.get("/api/sessions/{date}/{dirname}/transcript.txt")
    def download_transcript(date: str, dirname: str):
        s = _get_session(date, dirname)
        return Response(s.transcript_export().encode("utf-8-sig"), media_type="text/plain",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote(s.dir.name)}.txt"})

    @app.get("/api/sessions/{date}/{dirname}/summary.txt")
    def download_summary(date: str, dirname: str):
        s = _get_session(date, dirname)
        if not s.summary_text().strip():
            return JSONResponse({"error": "要約がありません"}, status_code=404)
        return Response(s.summary_export().encode("utf-8-sig"), media_type="text/plain",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote(s.dir.name)}_summary.txt"})

    # ---------- photos ----------
    def _photo(folder: Path, pid: str) -> Path:
        try:
            return photos.jpeg(pid, folder)
        except KeyError:
            raise HTTPException(status_code=404, detail="photo not found")

    @app.post("/api/photos/upload")
    async def upload_photo(request: Request, name: str = "photo"):
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_BYTES:
                return JSONResponse({"error": "ファイルが大きすぎます"}, status_code=413)
        try:
            return await asyncio.to_thread(photos.add, name, bytes(data))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)

    @app.get("/api/photos/inbox")
    def photo_inbox():
        sessions = store.all()
        tz = ZoneInfo(settings.event_tz)
        recs = []
        for s in reversed(sessions):
            a, b = (datetime.fromtimestamp(x, tz) for x in span(s))
            recs.append({"date": s.dir.parent.name, "dirname": s.dir.name,
                         "label": f"{a:%m/%d %H:%M}–{b:%H:%M} [{s.meta['id']}] {s.meta['title']}"})
        items = sorted(photos.load(photos.inbox), key=lambda p: p["taken"] or 0)
        return {"timezone": settings.event_tz, "recordings": recs, "items": [
            {**p, "suggest": suggest(p["taken"], sessions),
             "when": f"{datetime.fromtimestamp(p['taken'], tz):%m/%d %H:%M:%S}" if p["taken"] else None}
            for p in items]}

    @app.get("/api/photos/inbox/{pid}.jpg")
    def inbox_image(pid: str):
        return FileResponse(_photo(photos.inbox, pid), media_type="image/jpeg")

    @app.post("/api/photos/commit")
    def commit_photos(body: CommitBody):
        waiting = {p["id"] for p in photos.load(photos.inbox)}
        moves = [(i["id"], _get_session(str(i["date"]), str(i["dirname"])))
                 for i in body.items if i.get("id") in waiting]
        for pid, s in moves:
            photos.move(pid, photos.inbox, photos.folder(s))
        return {"saved": len(moves)}

    @app.delete("/api/photos/inbox")
    def clear_inbox():
        shutil.rmtree(photos.inbox, ignore_errors=True)
        return {"ok": True}

    @app.get("/api/sessions/{date}/{dirname}/photos/{pid}.jpg")
    def session_photo(date: str, dirname: str, pid: str):
        return FileResponse(_photo(photos.folder(_get_session(date, dirname)), pid),
                            media_type="image/jpeg")

    @app.delete("/api/sessions/{date}/{dirname}/photos/{pid}")
    def delete_photo(date: str, dirname: str, pid: str):
        folder = photos.folder(_get_session(date, dirname))
        _photo(folder, pid)
        photos.remove(pid, folder)
        return {"ok": True}

    @app.post("/api/sessions/{date}/{dirname}/photos/{pid}/move")
    def move_photo(date: str, dirname: str, pid: str, body: MoveBody):
        folder = photos.folder(_get_session(date, dirname))
        _photo(folder, pid)
        photos.move(pid, folder, photos.folder(_get_session(body.date, body.dirname)))
        return {"ok": True}

    @app.post("/api/sessions/{date}/{dirname}/photos/{pid}/read")
    def read_session_photo(date: str, dirname: str, pid: str):
        folder = photos.folder(_get_session(date, dirname))
        path = _photo(folder, pid)
        if not llm.available():
            return JSONResponse({"error": "LLM が未設定です。"}, status_code=503)
        text = read_photo(report_llm, path)
        if not text:
            return JSONResponse({"error": "読み取りに失敗しました。もう一度お試しください。"},
                                status_code=502)
        photos.set_text(pid, folder, text)
        return {"text": text}

    @app.post("/api/sessions/{date}/{dirname}/resummarize")
    def resummarize(date: str, dirname: str):
        s = _get_session(date, dirname)
        rec: Recording | None = state["recording"]
        if (rec is not None and rec.session.dir == s.dir) or s.dir.name in state["hq_pending"]:
            return JSONResponse({"error": "録音の処理が終わってからお試しください。"}, status_code=409)
        out = summarize_whole(s, report_llm, photos.slide_text(s))
        if not out:
            return JSONResponse({"error": "要約を作れませんでした。もう一度お試しください。"},
                                status_code=502)
        (s.dir / "summary.md").write_text(out, encoding="utf-8")
        return {"summary": out}

    @app.post("/api/sessions/{date}/{dirname}/photo-shift")
    def shift_photos(date: str, dirname: str, body: ShiftBody):
        _get_session(date, dirname).update_meta(photo_shift_sec=body.seconds)
        return {"ok": True}

    @app.get("/api/sessions/{date}/{dirname}/article.zip")
    def article(date: str, dirname: str):
        s = _get_session(date, dirname)
        return Response(photos.article_zip(s), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote(s.dir.name)}_article.zip"})

    @app.post("/api/schedule/import")
    def import_schedule(body: ScheduleBody):
        year, items = event_now().year, []
        for i in body.items:
            try:
                begin = datetime.strptime(f"{year}-{i.month}-{i.day} {i.start}", "%Y-%m-%d %H:%M")
                end = datetime.strptime(f"{year}-{i.month}-{i.day} {i.end}", "%Y-%m-%d %H:%M")
            except ValueError:
                begin = end = datetime.min
            if end <= begin or not i.title.strip():
                return JSONResponse({"error": "読み取った予定に不正な値があるため、更新しませんでした。"},
                                    status_code=422)
            items.append({"date": f"{begin:%Y-%m-%d}", "start": i.start, "end": i.end,
                          "title": i.title, "venue": i.venue, "kind": i.kind})
        before = len(load_schedule(SCHEDULE_CSV))
        if not items or (len(items) < before / 2 and not body.force):
            return JSONResponse({"error": f"予定が {before} 件から {len(items)} 件に減ります。",
                                 "confirm": bool(items)}, status_code=409)
        return save_schedule(SCHEDULE_CSV, items)

    @app.get("/api/sessions/{date}/{dirname}")
    def session_detail(date: str, dirname: str):
        s = _get_session(date, dirname)
        return {"meta": s.meta, "segments": s.segments(),
                "summary": s.summary_text(), "chat": s.chat_history(),
                "photos": photos.for_session(s)}

    @app.get("/api/status")
    def status():
        rec: Recording | None = state["recording"]
        session = None
        if rec is not None:
            session = {"dirname": rec.session.dir.name,
                       "date": rec.session.meta["started_at"][:10],
                       "meta": rec.session.meta}
        return {"recording": rec is not None, "session": session,
                "paused": rec.paused if rec else False,
                "mic_ok": state["mic_ok"], "llm_ok": llm.available(),
                "mail_ok": bool(settings.gmail_address and settings.gmail_app_password),
                "web_ok": getattr(llm, "can_search", lambda: False)(),
                "llm_error": getattr(llm, "last_error", None),
                "stt": None if rec is None else
                       "online" if rec.engine["streamer"] else "local",
                "model": model, "hq_pending": list(state["hq_pending"])}

    @app.get("/api/schedule")
    def schedule():
        now = event_now()
        done = {x.meta.get("schedule_key") for x in store.all()}
        return {"now": now.isoformat(timespec="seconds"),
                "today": f"{now:%Y-%m-%d}", "timezone": settings.event_tz,
                "items": [{**i, "recorded": i["key"] in done}
                          for i in load_schedule(SCHEDULE_CSV)]}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.discard(ws)

    if STATIC_DIR.is_dir():
        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
