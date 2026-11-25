import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from reinvent_scribe.config import Settings
from reinvent_scribe.server import create_app


class FakeMic:
    def __init__(self, on_frames, samplerate=16000):
        self.on_frames = on_frames

    def start(self):
        self.on_frames(np.zeros(1600, dtype=np.int16))

    def stop(self):
        pass


class BrokenMic(FakeMic):
    def start(self):
        raise RuntimeError("no input device")


class FakeTranscriber:
    def __init__(self, model_name, initial_prompt, on_segment, model=None):
        self.on_segment = on_segment

    def feed(self, pcm):
        self.on_segment(0.0, "hello from fake")

    def flush(self):
        pass

    def stop(self):
        pass


def fake_transcribe_file(wav_path, model_name="large-v3-turbo", initial_prompt=""):
    return [(0.0, "high quality text")]


class FakeLLM:
    def available(self):
        return False

    def chat(self, messages, max_tokens=1000):
        return None


@pytest.fixture
def client(tmp_path):
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        mic_factory=FakeMic,
        transcriber_factory=FakeTranscriber,
        transcribe_file_fn=fake_transcribe_file,
        llm=FakeLLM(),
        model_name="small.en",
    )
    with TestClient(app) as c:
        yield c


def _start(client, sid="KEY001"):
    return client.post("/api/sessions",
                       json={"session_id": sid, "title": "Keynote", "memo": ""})


def test_start_stop_session_writes_files(client, tmp_path):
    r = _start(client)
    assert r.status_code == 200
    dirname = r.json()["dirname"]
    date = r.json()["date"]
    r2 = client.post("/api/sessions/stop")
    assert r2.status_code == 200
    sdir = tmp_path / date / dirname
    assert (sdir / "audio.wav").exists()
    assert "hello from fake" in (sdir / "transcript.md").read_text()
    assert (sdir / "meta.json").exists()


def test_double_start_409(client):
    assert _start(client).status_code == 200
    assert _start(client, "KEY002").status_code == 409
    client.post("/api/sessions/stop")


def test_stop_without_recording_409(client):
    assert client.post("/api/sessions/stop").status_code == 409


def test_stop_triggers_hq_transcription(client, tmp_path):
    r = _start(client)
    dirname, date = r.json()["dirname"], r.json()["date"]
    client.post("/api/sessions/stop")
    deadline = time.time() + 5
    hq = tmp_path / date / dirname / "transcript_hq.md"
    while time.time() < deadline and not hq.exists():
        time.sleep(0.05)
    assert hq.exists()
    assert "high quality text" in hq.read_text()


def test_memo_patch(client, tmp_path):
    r = _start(client)
    dirname, date = r.json()["dirname"], r.json()["date"]
    client.post("/api/sessions/stop")
    r2 = client.patch(f"/api/sessions/{date}/{dirname}/memo",
                      json={"memo": "updated"})
    assert r2.status_code == 200
    import json as j
    assert j.loads((tmp_path / date / dirname / "meta.json").read_text())["memo"] == "updated"


def test_list_sessions(client):
    r = _start(client)
    date = r.json()["date"]
    client.post("/api/sessions/stop")
    r2 = client.get(f"/api/sessions?date={date}")
    assert r2.status_code == 200
    assert len(r2.json()["sessions"]) == 1


def test_chat_without_llm_503(client):
    r = _start(client)
    dirname, date = r.json()["dirname"], r.json()["date"]
    client.post("/api/sessions/stop")
    r2 = client.post(f"/api/sessions/{date}/{dirname}/chat",
                     json={"question": "what?"})
    assert r2.status_code == 503


def test_daily_report_endpoint(client, tmp_path):
    r = _start(client)
    date = r.json()["date"]
    client.post("/api/sessions/stop")
    r2 = client.post(f"/api/daily-report?date={date}")
    assert r2.status_code == 200
    assert "出張日報" in r2.json()["markdown"]
    assert (tmp_path / date / "daily_summary.md").exists()


def test_status_reports_mic_failure(tmp_path):
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        mic_factory=BrokenMic,
        transcriber_factory=FakeTranscriber,
        transcribe_file_fn=fake_transcribe_file,
        llm=FakeLLM(),
        model_name="small.en",
    )
    with TestClient(app) as c:
        r = c.post("/api/sessions",
                   json={"session_id": "K", "title": "t", "memo": ""})
        assert r.status_code == 503
        status = c.get("/api/status").json()
        assert status["recording"] is False
        assert status["llm_ok"] is False


def test_glossary_edit_applies_to_next_recording(tmp_path, monkeypatch):
    import reinvent_scribe.server as srv
    gl = tmp_path / "g.txt"
    gl.write_text("Kiro\n")
    monkeypatch.setattr(srv, "GLOSSARY", gl)
    prompts = []

    class T(FakeTranscriber):
        def __init__(self, model_name, initial_prompt, on_segment, model=None):
            prompts.append(initial_prompt)
            super().__init__(model_name, initial_prompt, on_segment, model)

    app = create_app(settings=Settings(data_dir=tmp_path / "d"), mic_factory=FakeMic,
                     transcriber_factory=T, transcribe_file_fn=fake_transcribe_file,
                     llm=FakeLLM(), model_name="x", streamer_factory=None)
    with TestClient(app) as c:
        _start(c, "A"); c.post("/api/sessions/stop")
        gl.write_text("Karpenter\n")
        _start(c, "B"); c.post("/api/sessions/stop")
    assert prompts[0] == "Kiro" and prompts[-1] == "Karpenter"
