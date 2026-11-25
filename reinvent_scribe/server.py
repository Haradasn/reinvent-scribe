import asyncio
import csv
import logging
import queue
import re
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio import MicStream, WavWriter
from .config import Settings
from .llm import LLMClient, transcript_chat
from .store import SessionStore
from .summarize import RollingSummarizer, build_daily_report
from .transcribe import Transcriber, load_glossary, transcribe_file

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SCHEDULE_CSV = Path("sessions.csv")
GLOSSARY = Path("glossary.txt")


class StartBody(BaseModel):
    session_id: str
    title: str
    memo: str = ""


class MemoBody(BaseModel):
    memo: str


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
               model_name: str | None = None, streamer_factory="auto") -> FastAPI:
    settings = settings or Settings.from_env()
    if streamer_factory == "auto":
        if settings.assemblyai_api_key:
            from .streaming import AssemblyAIStreamer
            key = settings.assemblyai_api_key

            def streamer_factory(on_segment, on_partial):
                return AssemblyAIStreamer(key, on_segment=on_segment,
                                          on_partial=on_partial)
        else:
            streamer_factory = None
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
            session = store.create(body.session_id, body.title, body.memo)
            wav = WavWriter(session.dir / "audio.wav")
            summarizer = RollingSummarizer(llm, settings.summary_interval_sec)

            # Per-segment Japanese translation: a single worker keeps order
            # and its failures never touch captions or recording.
            jp_queue: queue.Queue = queue.Queue()

            def _jp_worker() -> None:
                while True:
                    item = jp_queue.get()
                    if item is None:
                        return
                    ts, text = item
                    out = llm.chat([{"role": "user", "content":
                                     "次の英語を自然な日本語に翻訳してください。"
                                     "訳文のみを返すこと。\n\n" + text}],
                                   max_tokens=300)
                    if out:
                        session.append_segment(ts, "(JA) " + out.strip())
                        broadcast({"type": "jp", "ts": ts, "text": out.strip()})

            jp_worker = None
            if llm.available():
                jp_worker = threading.Thread(target=_jp_worker, daemon=True,
                                             name="jp-translate")
                jp_worker.start()

            def on_segment(ts: float, text: str) -> None:
                session.append_segment(ts, text)
                summarizer.add(text)
                broadcast({"type": "segment", "ts": ts, "text": text})
                if jp_worker is not None:
                    jp_queue.put((ts, text))
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
                    broadcast({"type": "summary", "text": out})

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
            rec.jp_queue.put(None)
            rec.jp_worker.join(timeout=20)
        _step("wav", rec.wav.close)
        # flush any un-summarized tail, then persist the rolling summary
        try:
            tail = rec.summarizer.summarize()
            if tail:
                broadcast({"type": "summary", "text": tail})
            if rec.summarizer.current:
                (rec.session.dir / "summary.md").write_text(
                    rec.summarizer.current, encoding="utf-8")
        except Exception:
            log.exception("summary persistence failed")
        rec.session.finalize()
        state["hq_pending"].append(rec.session.dir.name)

        def _hq():
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
        answer = transcript_chat(llm, transcript, history=[], question=body.question)
        if answer is None:
            return JSONResponse({"error": "LLM 呼び出しに失敗しました。"},
                                status_code=502)
        session.append_chat("user", body.question)
        session.append_chat("assistant", answer)
        return {"answer": answer}

    @app.post("/api/daily-report")
    def daily_report(date: str):
        _check_date(date)
        sessions = store.list_for_date(date)
        md = build_daily_report(sessions, llm)
        day_dir = settings.data_dir / date
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "daily_summary.md").write_text(md, encoding="utf-8")
        return {"markdown": md}

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
                "model": model, "hq_pending": list(state["hq_pending"])}

    @app.get("/api/schedule")
    def schedule(now: str | None = None):
        try:
            ts = datetime.fromisoformat(now) if now else datetime.now()
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="invalid now")
        suggestions = []
        if SCHEDULE_CSV.exists():
            with open(SCHEDULE_CSV, encoding="utf-8") as f:
                for row in csv.reader(f):
                    if len(row) < 4:
                        continue
                    date, start, sid, title = [c.strip() for c in row[:4]]
                    try:
                        begin = datetime.fromisoformat(f"{date}T{start}")
                    except ValueError:
                        continue
                    if abs(ts - begin) <= timedelta(minutes=30):
                        suggestions.append({"session_id": sid, "title": title})
        return {"suggestions": suggestions}

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
