import threading
import time

import numpy as np
from fastapi.testclient import TestClient

import reinvent_scribe.server as server
from reinvent_scribe.config import Settings
from reinvent_scribe.streaming import AssemblyAIStreamer
from tests.test_server import FakeLLM, FakeMic, fake_transcribe_file

SEC = np.zeros(16000, dtype=np.int16)


def test_outage_replays_unconfirmed_audio_then_returns_online(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RECONNECT_SEC", 0.05)
    opened, mics, heard = [], [], []

    class Mic(FakeMic):
        def start(self):
            mics.append(self)

    class Stream:
        def __init__(self, on_segment, on_partial):
            self.on_segment, self.sec, self.confirmed_sec = on_segment, 0, 0.0
            self.stopped = False
            opened.append(self)

        def start(self):
            if len(opened) == 2:
                raise ConnectionError("still offline")

        def feed(self, pcm):
            if len(opened) == 1 and self.sec == 4:
                raise ConnectionError("gone")
            self.sec += 1
            if len(opened) == 1 and self.sec == 2:
                self.confirmed_sec = 2.0
            self.on_segment(self.sec - 1, f"online{len(opened)}")

        def stop(self):
            self.stopped = True

    class Local:
        def __init__(self, model_name, initial_prompt, on_segment, model=None):
            self.on_segment, self.n = on_segment, 0

        def feed(self, pcm):
            self.on_segment(self.n, "local")
            self.n += len(pcm) // 16000

        def flush(self): pass
        def stop(self): pass

    app = server.create_app(settings=Settings(data_dir=tmp_path), mic_factory=Mic,
                            transcriber_factory=Local, transcribe_file_fn=fake_transcribe_file,
                            llm=FakeLLM(), model_name="x", streamer_factory=Stream)

    def wait(f):
        for _ in range(200):
            if f():
                return
            time.sleep(0.02)
        raise AssertionError("timeout")

    path = lambda: next(tmp_path.glob("*/*/transcript.md")).read_text()
    with TestClient(app) as c:
        c.post("/api/sessions", json={"session_id": "K", "title": "t"})
        for _ in range(5):  # seconds 0-4; the link dies on the fifth
            mics[0].on_frames(SEC)
        wait(lambda: path().count("local") == 3)
        assert c.get("/api/status").json()["stt"] == "local"
        wait(lambda: len(opened) == 3)
        time.sleep(0.1)
        mics[0].on_frames(SEC)  # second 5, online again
        wait(lambda: "online3" in path())
        assert c.get("/api/status").json()["stt"] == "online"
        c.post("/api/sessions/stop")
    body = path()
    # 2-4 were sent but never confirmed, so they are transcribed locally
    for line in ("[00:00:02] local", "[00:00:03] local", "[00:00:04] local", "[00:00:05] online3"):
        assert line in body
    assert opened[0].stopped and opened[2].stopped


def test_silent_link_is_cut_even_while_send_would_block():
    cut = threading.Event()

    class Sock:
        def shutdown(self, how): cut.set()

    class WS:
        socket = Sock()
        def ping(self): return threading.Event()  # pong never comes

    s = AssemblyAIStreamer(api_key="k", on_segment=lambda ts, t: None)
    s._ws, s._DEAD_SEC = WS(), 0.2
    s._last_ok = time.monotonic()
    threading.Thread(target=s._watchdog, daemon=True).start()
    assert cut.wait(3) and not s.alive()
    try:
        s.feed(SEC)
        raise AssertionError("feed must fail")
    except ConnectionError:
        pass
