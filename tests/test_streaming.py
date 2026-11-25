import json
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from reinvent_scribe.config import Settings
from reinvent_scribe.server import create_app
from reinvent_scribe.streaming import AssemblyAIStreamer

from tests.test_server import (FakeLLM, FakeMic, FakeTranscriber,
                               fake_transcribe_file)


# --- message parsing ---

def make_streamer(events):
    s = AssemblyAIStreamer(api_key="k",
                           on_segment=lambda ts, t: events.append(("final", ts, t)),
                           on_partial=lambda t: events.append(("partial", t)))
    return s


def test_turn_end_emits_segment():
    events = []
    s = make_streamer(events)
    s._handle(json.dumps({
        "type": "Turn", "transcript": "Hello world.", "end_of_turn": True,
        "words": [{"start": 2500, "text": "Hello"}]}))
    assert events == [("final", 2.5, "Hello world.")]


def test_partial_turn_emits_partial():
    events = []
    s = make_streamer(events)
    s._handle(json.dumps({
        "type": "Turn", "transcript": "Hello wor", "end_of_turn": False,
        "words": [{"start": 2500, "text": "Hello"}]}))
    assert events == [("partial", "Hello wor")]


def test_non_turn_messages_ignored():
    events = []
    s = make_streamer(events)
    s._handle(json.dumps({"type": "Begin", "id": "x"}))
    s._handle("not json at all")
    assert events == []


# --- server integration: streaming preferred, local fallback ---

class FakeStreamer:
    instances = []

    def __init__(self, on_segment, on_partial):
        self.on_segment = on_segment
        self.fed = []
        FakeStreamer.instances.append(self)

    def start(self):
        pass

    def feed(self, pcm):
        self.fed.append(pcm)
        self.on_segment(0.0, "from stream")

    def stop(self):
        pass


class BrokenStreamer(FakeStreamer):
    def start(self):
        raise ConnectionError("no network")


def make_client(tmp_path, streamer_factory):
    app = create_app(settings=Settings(data_dir=tmp_path),
                     mic_factory=FakeMic, transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file,
                     llm=FakeLLM(), model_name="small.en",
                     streamer_factory=streamer_factory)
    return TestClient(app)


def test_streaming_used_when_available(tmp_path):
    FakeStreamer.instances = []
    with make_client(tmp_path, FakeStreamer) as c:
        r = c.post("/api/sessions", json={"session_id": "S1", "title": "t", "memo": ""})
        dirname, date = r.json()["dirname"], r.json()["date"]
        c.post("/api/sessions/stop")
        text = (tmp_path / date / dirname / "transcript.md").read_text()
    assert "from stream" in text
    assert FakeStreamer.instances and FakeStreamer.instances[0].fed


def test_falls_back_to_local_when_stream_fails(tmp_path):
    with make_client(tmp_path, BrokenStreamer) as c:
        r = c.post("/api/sessions", json={"session_id": "S2", "title": "t", "memo": ""})
        dirname, date = r.json()["dirname"], r.json()["date"]
        c.post("/api/sessions/stop")
        text = (tmp_path / date / dirname / "transcript.md").read_text()
    assert "hello from fake" in text  # local FakeTranscriber output


def test_no_streamer_factory_uses_local(tmp_path):
    with make_client(tmp_path, None) as c:
        r = c.post("/api/sessions", json={"session_id": "S3", "title": "t", "memo": ""})
        dirname, date = r.json()["dirname"], r.json()["date"]
        c.post("/api/sessions/stop")
        text = (tmp_path / date / dirname / "transcript.md").read_text()
    assert "hello from fake" in text


# --- feed buffering: AAI requires 50ms-1000ms chunks ---

class SentWS:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


def test_feed_buffers_small_chunks():
    s = AssemblyAIStreamer(api_key="k", on_segment=lambda ts, t: None)
    s._ws = SentWS()
    small = np.zeros(480, dtype=np.int16)  # 30ms @16k
    s.feed(small)
    assert s._ws.sent == []  # below 50ms: held back
    s.feed(small)
    s.feed(small)  # 90ms total -> flushed
    assert len(s._ws.sent) == 1
    assert len(s._ws.sent[0]) == 480 * 3 * 2  # bytes of int16
