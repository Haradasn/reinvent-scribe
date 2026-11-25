"""Tests for final-review fixes."""
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from reinvent_scribe.config import Settings
from reinvent_scribe.server import create_app
from reinvent_scribe.summarize import RollingSummarizer

from tests.test_server import (BrokenMic, FakeLLM, FakeMic, FakeTranscriber,
                               fake_transcribe_file)


# --- Critical 1: transcription must not run on the audio callback thread ---

class ThreadRecordingTranscriber(FakeTranscriber):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        ThreadRecordingTranscriber.feed_thread = None
        ThreadRecordingTranscriber.fed = threading.Event()

    def feed(self, pcm):
        ThreadRecordingTranscriber.feed_thread = threading.current_thread()
        ThreadRecordingTranscriber.fed.set()


class CallbackThreadMic(FakeMic):
    """Calls on_frames from a named thread, like PortAudio does."""
    def start(self):
        def run():
            CallbackThreadMic.cb_thread = threading.current_thread()
            self.on_frames(np.zeros(1600, dtype=np.int16))
        t = threading.Thread(target=run, name="audio-callback")
        t.start()
        t.join()


def test_transcription_runs_off_audio_callback_thread(tmp_path):
    app = create_app(settings=Settings(data_dir=tmp_path),
                     mic_factory=CallbackThreadMic,
                     transcriber_factory=ThreadRecordingTranscriber,
                     transcribe_file_fn=fake_transcribe_file,
                     llm=FakeLLM(), model_name="small.en")
    with TestClient(app) as c:
        c.post("/api/sessions", json={"session_id": "K", "title": "t", "memo": ""})
        assert ThreadRecordingTranscriber.fed.wait(3.0)
        c.post("/api/sessions/stop")
    assert ThreadRecordingTranscriber.feed_thread is not CallbackThreadMic.cb_thread


# --- Important 2: failed mic start must not leave an orphan session ---

def test_failed_mic_start_leaves_no_session(tmp_path):
    app = create_app(settings=Settings(data_dir=tmp_path),
                     mic_factory=BrokenMic,
                     transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file,
                     llm=FakeLLM(), model_name="small.en")
    with TestClient(app) as c:
        r = c.post("/api/sessions", json={"session_id": "K", "title": "t", "memo": ""})
        assert r.status_code == 503
        from datetime import date
        r2 = c.get(f"/api/sessions?date={date.today().isoformat()}")
        assert r2.json()["sessions"] == []


# --- Important 3: summarizer must not lose segments added during the LLM call ---

class RaceLLM:
    def __init__(self, summarizer_holder):
        self.holder = summarizer_holder

    def available(self):
        return True

    def chat(self, messages, max_tokens=1000):
        self.holder["rs"].add("late segment")  # arrives mid-call
        return "summary"


def test_summarizer_keeps_segments_added_during_call():
    holder = {}
    rs = RollingSummarizer(RaceLLM(holder), interval_sec=1)
    holder["rs"] = rs
    rs.add("early")
    assert rs.summarize() == "summary"
    assert rs._pending == ["late segment"]


class FailingLLM:
    def available(self):
        return True

    def chat(self, messages, max_tokens=1000):
        return None


def test_summarizer_requeues_on_failure():
    rs = RollingSummarizer(FailingLLM(), interval_sec=1)
    rs.add("text")
    assert rs.summarize() is None
    assert "text" in rs._pending  # retried next time


# --- Important 5: invalid params return 4xx, not 500 ---

@pytest.fixture
def client(tmp_path):
    app = create_app(settings=Settings(data_dir=tmp_path),
                     mic_factory=FakeMic, transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file,
                     llm=FakeLLM(), model_name="small.en")
    with TestClient(app) as c:
        yield c


def test_missing_session_404(client):
    r = client.post("/api/sessions/2026-12-01/nope/chat", json={"question": "q"})
    assert r.status_code == 404
    r2 = client.patch("/api/sessions/2026-12-01/nope/memo", json={"memo": "m"})
    assert r2.status_code == 404


def test_bad_date_422(client):
    assert client.post("/api/daily-report?date=../../evil").status_code == 422
    assert client.get("/api/sessions?date=not-a-date").status_code == 422


# --- Important 4: startup wires pick_model fallback ---

def test_main_uses_pick_model(monkeypatch):
    import reinvent_scribe.__main__ as m
    import sys
    captured = {}
    monkeypatch.setattr("reinvent_scribe.transcribe.pick_model",
                        lambda preferred: "base.en")
    monkeypatch.setattr(m, "create_app",
                        lambda settings, model_name: captured.update(model=model_name))
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["reinvent-scribe", "--no-browser", "--port", "59871"])
    m.main()
    assert captured["model"] == "base.en"


# --- delete session from UI ---

def test_delete_session(client, tmp_path):
    r = client.post("/api/sessions", json={"session_id": "DEL1", "title": "t", "memo": ""})
    dirname, date = r.json()["dirname"], r.json()["date"]
    client.post("/api/sessions/stop")
    r2 = client.delete(f"/api/sessions/{date}/{dirname}")
    assert r2.status_code == 200
    assert not (tmp_path / date / dirname).exists()
    assert client.delete(f"/api/sessions/{date}/{dirname}").status_code == 404


def test_delete_active_session_409(client):
    r = client.post("/api/sessions", json={"session_id": "DEL2", "title": "t", "memo": ""})
    dirname, date = r.json()["dirname"], r.json()["date"]
    assert client.delete(f"/api/sessions/{date}/{dirname}").status_code == 409
    client.post("/api/sessions/stop")


# --- HQ: decode WAV ourselves (PyAV incompatibility) ---

def test_transcribe_file_passes_ndarray(tmp_path, monkeypatch):
    import reinvent_scribe.transcribe as tr
    import wave as wave_mod
    from tests.test_transcribe import FakeModel
    model = FakeModel([(0.0, " hi")])
    monkeypatch.setattr(tr, "_load_model", lambda name: model)
    wav = tmp_path / "a.wav"
    with wave_mod.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    tr.transcribe_file(wav, model_name="large-v3-turbo")
    audio = model.calls_audio[0]
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32


# --- status exposes the active session so the UI can restore after reload ---

def test_status_includes_active_session(client):
    r = client.post("/api/sessions", json={"session_id": "ACT1", "title": "Active", "memo": ""})
    status = client.get("/api/status").json()
    assert status["recording"] is True
    assert status["session"]["meta"]["id"] == "ACT1"
    assert status["session"]["dirname"] == r.json()["dirname"]
    client.post("/api/sessions/stop")
    assert client.get("/api/status").json()["session"] is None


# --- per-segment Japanese translation (few seconds behind captions) ---

def test_segments_get_japanese_translation(tmp_path):
    class TranslatingLLM:
        def available(self):
            return True

        def chat(self, messages, max_tokens=1000):
            return "日本語訳です"

    app = create_app(settings=Settings(data_dir=tmp_path),
                     mic_factory=FakeMic, transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file,
                     llm=TranslatingLLM(), model_name="small.en")
    with TestClient(app) as c:
        r = c.post("/api/sessions", json={"session_id": "JP1", "title": "t", "memo": ""})
        dirname, date = r.json()["dirname"], r.json()["date"]
        deadline = time.time() + 3
        text = ""
        while time.time() < deadline:
            text = (tmp_path / date / dirname / "transcript.md").read_text()
            if "日本語訳です" in text:
                break
            time.sleep(0.05)
        c.post("/api/sessions/stop")
    assert "日本語訳です" in text


# --- pause / resume ---

def test_pause_and_resume(client):
    client.post("/api/sessions", json={"session_id": "P1", "title": "t", "memo": ""})
    r = client.post("/api/sessions/pause")
    assert r.status_code == 200
    st = client.get("/api/status").json()
    assert st["recording"] is True and st["paused"] is True
    assert client.post("/api/sessions/pause").status_code == 409  # already paused
    r2 = client.post("/api/sessions/resume")
    assert r2.status_code == 200
    assert client.get("/api/status").json()["paused"] is False
    assert client.post("/api/sessions/resume").status_code == 409  # not paused
    client.post("/api/sessions/stop")


def test_pause_without_recording_409(client):
    assert client.post("/api/sessions/pause").status_code == 409
    assert client.post("/api/sessions/resume").status_code == 409


# --- rolling summary persisted per session on stop ---

def test_summary_saved_on_stop(tmp_path):
    class SummaryLLM:
        def available(self):
            return True

        def chat(self, messages, max_tokens=1000):
            return "累積要約テキスト"

    app = create_app(settings=Settings(data_dir=tmp_path, summary_interval_sec=0),
                     mic_factory=FakeMic, transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file,
                     llm=SummaryLLM(), model_name="small.en")
    with TestClient(app) as c:
        r = c.post("/api/sessions", json={"session_id": "SUM1", "title": "t", "memo": ""})
        dirname, date = r.json()["dirname"], r.json()["date"]
        deadline = time.time() + 3
        while time.time() < deadline:
            time.sleep(0.05)
            if (tmp_path / date / dirname).exists():
                break
        c.post("/api/sessions/stop")
        while c.get("/api/status").json()["hq_pending"]:
            time.sleep(0.02)
    summary = tmp_path / date / dirname / "summary.md"
    # summary.md exists iff a summary was produced; with interval 0 it ran
    assert summary.exists()
    assert "累積要約テキスト" in summary.read_text()


# --- stop must finish every step even if an earlier one fails ---

def test_stop_finalizes_even_if_mic_stop_fails(tmp_path):
    from tests.test_streaming import FakeStreamer

    class StopFailMic(FakeMic):
        def stop(self):
            raise RuntimeError("PortAudio error")

    class S(FakeStreamer):
        stopped = False

        def stop(self):
            S.stopped = True

    app = create_app(settings=Settings(data_dir=tmp_path), mic_factory=StopFailMic,
                     transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, llm=FakeLLM(),
                     model_name="small.en", streamer_factory=S)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/api/sessions", json={"session_id": "SF", "title": "t"})
        assert c.post("/api/sessions/stop").status_code == 200
        meta = list(tmp_path.glob("*/*/meta.json"))[0].read_text()
    assert '"ended_at": null' not in meta
    assert S.stopped


def test_failed_streamer_is_closed_on_fallback(tmp_path):
    from tests.test_streaming import FakeStreamer

    class S(FakeStreamer):
        stopped = False

        def feed(self, pcm):
            raise ConnectionError("dropped")

        def stop(self):
            S.stopped = True

    app = create_app(settings=Settings(data_dir=tmp_path), mic_factory=FakeMic,
                     transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, llm=FakeLLM(),
                     model_name="small.en", streamer_factory=S)
    with TestClient(app) as c:
        c.post("/api/sessions", json={"session_id": "SF2", "title": "t"})
        c.post("/api/sessions/stop")
    assert S.stopped
