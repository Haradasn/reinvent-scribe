import asyncio
import csv
import logging
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
    def __init__(self, session, mic, transcriber, wav, summarizer):
        self.session = session
        self.mic = mic
        self.transcriber = transcriber
        self.wav = wav
        self.summarizer = summarizer


def create_app(settings: Settings | None = None, mic_factory=MicStream,
               transcriber_factory=Transcriber,
               transcribe_file_fn=transcribe_file, llm: LLMClient | None = None,
               model_name: str | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
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

            def on_segment(ts: float, text: str) -> None:
                session.append_segment(ts, text)
                summarizer.add(text)
                broadcast({"type": "segment", "ts": ts, "text": text})
                if summarizer.due():
                    threading.Thread(target=_run_summary, daemon=True).start()

            def _run_summary() -> None:
                out = summarizer.summarize()
                if out:
                    broadcast({"type": "summary", "text": out})

            transcriber = transcriber_factory(
                model_name=model, initial_prompt=glossary_prompt,
                on_segment=on_segment)

            def on_frames(frames: np.ndarray) -> None:
                try:
                    wav.write(frames)
                    transcriber.feed(frames.astype(np.float32) / 32768.0)
                except Exception:
                    log.exception("frame handling failed")

            mic = mic_factory(on_frames=on_frames)
            try:
                mic.start()
            except Exception:
                log.exception("mic start failed")
                state["mic_ok"] = False
                wav.close()
                return JSONResponse(
                    {"error": "マイクが見つかりません。入力デバイスを確認してください。"},
                    status_code=503)
            state["mic_ok"] = True
            state["recording"] = Recording(session, mic, transcriber, wav, summarizer)
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
        rec.mic.stop()
        rec.transcriber.stop()
        rec.wav.close()
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

    @app.patch("/api/sessions/{date}/{dirname}/memo")
    def patch_memo(date: str, dirname: str, body: MemoBody):
        session = store.get(date, dirname)
        session.update_memo(body.memo)
        return {"ok": True}

    @app.get("/api/sessions")
    def list_sessions(date: str):
        sessions = store.list_for_date(date)
        return {"sessions": [
            {"dirname": s.dir.name, "meta": s.meta,
             "hq": (s.dir / "transcript_hq.md").exists()} for s in sessions]}

    @app.post("/api/sessions/{date}/{dirname}/chat")
    def chat(date: str, dirname: str, body: ChatBody):
        if not llm.available():
            return JSONResponse(
                {"error": "LLM が未設定です。LLM_BASE_URL / LLM_API_KEY / LLM_MODEL を設定してください。"},
                status_code=503)
        session = store.get(date, dirname)
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
        sessions = store.list_for_date(date)
        md = build_daily_report(sessions, llm)
        day_dir = settings.data_dir / date
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "daily_summary.md").write_text(md, encoding="utf-8")
        return {"markdown": md}

    @app.get("/api/status")
    def status():
        return {"recording": state["recording"] is not None,
                "mic_ok": state["mic_ok"], "llm_ok": llm.available(),
                "model": model, "hq_pending": list(state["hq_pending"])}

    @app.get("/api/schedule")
    def schedule(now: str | None = None):
        ts = datetime.fromisoformat(now) if now else datetime.now()
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
