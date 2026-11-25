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


# --- the last utterance must survive stop and pause ---

class LateWS:
    """Delivers the final turn only after Terminate, like the real service."""
    def __init__(self):
        import queue
        self.q, self.sent, self.closed = queue.Queue(), [], False

    def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and "Terminate" in data:
            def later():
                import time
                time.sleep(0.2)
                if not self.closed:
                    self.q.put(json.dumps({"type": "Turn", "transcript": "last words",
                                           "end_of_turn": True, "words": []}))
                    self.q.put(json.dumps({"type": "Termination"}))
            threading.Thread(target=later).start()

    def close(self):
        self.closed = True
        self.q.put(None)

    def __iter__(self):
        return iter(self.q.get, None)


def late_streamer(got):
    s = AssemblyAIStreamer(api_key="k", on_segment=lambda ts, t: got.append(t))
    s._ws = LateWS()
    s._receiver = threading.Thread(target=s._recv_loop, daemon=True)
    s._receiver.start()
    return s


def test_stop_waits_for_final_turn_and_sends_buffered_audio():
    got = []
    s = late_streamer(got)
    s.feed(np.zeros(800, dtype=np.int16))  # 50ms, still buffered
    s.stop()
    assert got == ["last words"]
    assert len(s._ws.sent[0]) == 1600


def test_end_utterance_forces_endpoint():
    s = late_streamer([])
    s.end_utterance()
    assert json.loads(s._ws.sent[-1]) == {"type": "ForceEndpoint"}
    s.stop()


def test_pause_finalizes_current_utterance(tmp_path):
    class S(FakeStreamer):
        ended = 0

        def end_utterance(self):
            S.ended += 1

    with make_client(tmp_path, S) as c:
        c.post("/api/sessions", json={"session_id": "P", "title": "t"})
        assert c.post("/api/sessions/pause").status_code == 200
        import time
        for _ in range(50):
            if S.ended:
                break
            time.sleep(0.02)
        c.post("/api/sessions/stop")
    assert S.ended == 1


def test_stop_does_not_wait_for_slow_summary(tmp_path):
    import time

    class SlowLLM:
        def available(self):
            return True

        def chat(self, messages, max_tokens=1000):
            if "これまでの要約" in str(messages):
                time.sleep(1.5)
            return "x"

    app = create_app(settings=Settings(data_dir=tmp_path), mic_factory=FakeMic,
                     transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, llm=SlowLLM(),
                     model_name="x", streamer_factory=None)
    with TestClient(app) as c:
        c.post("/api/sessions", json={"session_id": "Q", "title": "t"})
        t = time.time()
        c.post("/api/sessions/stop")
        assert time.time() - t < 1.0
        for _ in range(100):
            if not c.get("/api/status").json()["hq_pending"]:
                break
            time.sleep(0.05)
    assert list(tmp_path.glob("*/*/summary.md"))


def partial(end, text, turn=0):
    return json.dumps({"type": "Turn", "transcript": text, "end_of_turn": False,
                       "turn_order": turn, "words": [{"start": 0, "end": end}]})


def test_long_turn_is_split_once_at_sentence_end():
    s = AssemblyAIStreamer(api_key="k", on_segment=lambda ts, t: None)
    s._ws = SentWS()
    s._handle(partial(5000, "Short."))
    s._handle(partial(9000, "Still in the middle"))
    assert s._ws.sent == []
    s._handle(partial(10000, "Done now."))
    s._handle(partial(11000, "Done now. Next."))
    assert len(s._ws.sent) == 1
    s._handle(partial(21000, "no punctuation", turn=1))
    assert len(s._ws.sent) == 2


def test_failed_translation_is_retried(tmp_path):
    import time

    class Flaky:
        n = 0

        def available(self):
            return True

        def chat(self, messages, max_tokens=1000):
            if "翻訳" in str(messages):
                Flaky.n += 1
                return None if Flaky.n == 1 else "こん\nにちは"
            return "x"

    app = create_app(settings=Settings(data_dir=tmp_path), mic_factory=FakeMic,
                     transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, llm=Flaky(),
                     model_name="x", streamer_factory=None)
    with TestClient(app) as c:
        c.post("/api/sessions", json={"session_id": "Q", "title": "t"})
        assert c.get("/api/status").json()["stt"] == "local"
        c.post("/api/sessions/stop")
        for _ in range(100):
            if not c.get("/api/status").json()["hq_pending"]:
                break
            time.sleep(0.05)
    assert "(JA) こん にちは" in next(tmp_path.glob("*/*/transcript.md")).read_text()


def test_stream_url_sets_model_and_keyterms():
    from urllib.parse import parse_qs, urlparse
    from reinvent_scribe.streaming import stream_url
    q = parse_qs(urlparse(stream_url("m", ["Step Functions", "Karpenter"])).query)
    assert q["speech_model"] == ["m"] and q["sample_rate"] == ["16000"]
    assert json.loads(q["keyterms_prompt"][0]) == ["Step Functions", "Karpenter"]
    assert "keyterms_prompt" not in stream_url("m", [])
    assert "assemblyai-stt-3-7-realtime" in stream_url("")
