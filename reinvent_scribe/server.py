import asyncio
import logging
import queue
import re
import shutil
import smtplib
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio import MicStream, WavWriter
from .config import Settings
from .mail import build_message, send_gmail
from .schedule import load_schedule, save_schedule
from .llm import LLMClient, transcript_chat
from .store import SessionStore
from .summarize import RollingSummarizer, build_daily_report, build_slack_post
from .transcribe import Transcriber, load_glossary, transcribe_file

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SCHEDULE_CSV = Path("sessions.csv")
GLOSSARY = Path("glossary.txt")
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
                        retry.append((ts, text, tries + 1))

            def _translate(ts, text, last) -> bool:
                try:
                    out = llm.chat([{"role": "user", "content":
                                     "次の発言を自然な日本語に翻訳してください。"
                                     "すでに日本語ならそのまま返すこと。"
                                     "訳文のみを返すこと。\n\n" + text}],
                                   max_tokens=1000)
                    if out:
                        # one line: it may be saved after later captions
                        out = " ".join(out.split("\n")).strip()
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

            engine = {"transcriber": None, "streamer": None}

            def _local_transcriber():
                if engine["transcriber"] is None:
                    engine["transcriber"] = transcriber_factory(
                        model_name=model, initial_prompt=glossary_prompt,
                        on_segment=on_segment)
                return engine["transcriber"]

            if streamer_factory is not None:
                try:
                    streamer = streamer_factory(
                        on_segment=on_segment,
                        on_partial=lambda t: broadcast({"type": "partial", "text": t}))
                    streamer.start()
                    engine["streamer"] = streamer
                    log.info("using streaming STT (AssemblyAI)")
                except Exception:
                    log.warning("streaming STT unavailable; using local whisper",
                                exc_info=True)
            if engine["streamer"] is None:
                _local_transcriber()

            # The audio callback must stay fast: it only writes the WAV and
            # enqueues PCM. Transcription (local inference or network send)
            # runs on a dedicated worker thread so it can never stall capture.
            frame_queue: queue.Queue = queue.Queue()

            def on_frames(frames: np.ndarray) -> None:
                try:
                    wav.write(frames)
                    frame_queue.put(frames)
                except Exception:
                    log.exception("frame handling failed")

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
                        if engine["streamer"] is not None:
                            try:
                                engine["streamer"].feed(frames)
                                continue
                            except Exception:
                                log.warning("stream feed failed; falling back "
                                            "to local whisper", exc_info=True)
                                dead, engine["streamer"] = engine["streamer"], None
                                try:
                                    dead.stop()
                                except Exception:
                                    log.exception("streamer close failed")
                                broadcast({"type": "stt_fallback"})
                        _local_transcriber().feed(
                            frames.astype(np.float32) / 32768.0)
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
                shutil.rmtree(session.dir, ignore_errors=True)
                return JSONResponse(
                    {"error": "マイクが見つかりません。入力デバイスを確認してください。"},
                    status_code=503)
            worker.start()
            state["mic_ok"] = True
            rec = Recording(session, mic, engine, wav,
                            summarizer, frame_queue, worker)
            rec.jp_queue, rec.jp_worker = jp_queue, jp_worker
            state["recording"] = rec
            date = session.meta["started_at"][:10]
            return {"dirname": session.dir.name, "date": date,
                    "meta": session.meta}

    @app.post("/api/sessions/stop")
    def stop_session():
        with lock:
            rec: Recording | None = state["recording"]
            if rec is None:
                return JSONResponse({"error": "not recording"}, status_code=409)
            state["recording"] = None
        # Every step runs even if an earlier one fails, so the WAV is always
        # closed and the (billed) streaming connection is always released.
        def _step(name, fn):
            try:
                fn()
            except Exception:
                log.exception("stop step failed: %s", name)

        if not rec.paused:
            _step("mic", rec.mic.stop)
        rec.frame_queue.put(None)
        rec.worker.join(timeout=30)
        # engines first: their final segments still need translating
        for name, part in list(rec.engine.items()):
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
        shutil.rmtree(session.dir)
        return {"ok": True}

    @app.get("/api/sessions")
    def list_sessions(date: str):
        _check_date(date)
        sessions = store.list_for_date(date)
        return {"sessions": [
            {"dirname": s.dir.name, "meta": s.meta,
             "hq": (s.dir / "transcript_hq.md").exists()} for s in sessions]}

    @app.post("/api/sessions/{date}/{dirname}/chat")
    def chat(date: str, dirname: str, body: ChatBody):
        session = _get_session(date, dirname)
        if not llm.available():
            return JSONResponse(
                {"error": "LLM が未設定です。LLM_BASE_URL / LLM_API_KEY / LLM_MODEL を設定してください。"},
                status_code=503)
        hq = session.dir / "transcript_hq.md"
        transcript = hq.read_text(encoding="utf-8") if hq.exists() \
            else session.transcript_text()
        answer = transcript_chat(llm, transcript, history=session.chat_history()[-20:],
                                 question=body.question)
        if answer is None:
            return JSONResponse({"error": "AIサービスが混雑しているか、接続できません。もう一度送信してください。"},
                                status_code=502)
        session.append_chat("user", body.question)
        session.append_chat("assistant", answer)
        return {"answer": answer}

    @app.post("/api/daily-report")
    def daily_report(date: str, body: ReportBody | None = None):
        _check_date(date)
        sessions = store.list_for_date(date)
        md = build_daily_report(sessions, report_llm)
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
        if not (settings.gmail_address and settings.gmail_app_password):
            return JSONResponse({"error": ".env に GMAIL_ADDRESS と GMAIL_APP_PASSWORD を設定し、アプリを再起動してください。"},
                                status_code=503)
        if not body.body.strip():
            return JSONResponse({"error": "先に日報を生成してください。"}, status_code=422)
        files = []
        if body.attach_transcript:
            files = [(f"{s.dir.name}.txt", s.transcript_export())
                     for s in store.list_for_date(date)]
        try:
            msg = build_message(settings.gmail_address, body.to, body.subject,
                                body.body, files)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        try:
            mail_sender(settings.gmail_address, settings.gmail_app_password, msg)
        except smtplib.SMTPAuthenticationError:
            return JSONResponse({"error": "Gmail にログインできません。アプリパスワードを確認してください。"},
                                status_code=502)
        except Exception as e:
            log.warning("mail send failed: %s", type(e).__name__)
            return JSONResponse({"error": "送信できたか確認できません。Gmail の送信済みを確認してください。"},
                                status_code=502)
        return {"ok": True, "attachments": len(files)}

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
                "summary": s.summary_text(), "chat": s.chat_history()}

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
