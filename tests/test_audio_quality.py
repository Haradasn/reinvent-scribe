from datetime import datetime

import numpy as np

from reinvent_scribe.store import SessionStore
from reinvent_scribe.transcribe import level_audio


def test_quiet_speech_is_raised_and_loud_is_untouched():
    t = np.arange(16000 * 10) / 16000
    tone = np.sin(2 * np.pi * 200 * t).astype(np.float32)
    quiet = level_audio(tone * 0.01)
    assert 0.05 < np.sqrt((quiet ** 2).mean()) < 0.07 and quiet.dtype == np.float32
    assert np.allclose(level_audio(tone * 0.5), tone * 0.5)
    assert len(level_audio(np.zeros(0, np.float32))) == 0


def test_incomplete_second_pass_is_not_used(tmp_path):
    s = SessionStore(tmp_path).create("K", "t", started_at=datetime(2026, 11, 25))
    s.append_segment(0, "word " * 200)
    (s.dir / "transcript_hq.md").write_text("short", encoding="utf-8")
    assert s.best_transcript().count("word") == 200
    (s.dir / "transcript_hq.md").write_text("better " * 200, encoding="utf-8")
    assert "better" in s.best_transcript()


def test_level_meter_warns_on_quiet_and_clipping_only():
    from reinvent_scribe.audio import LevelMeter

    def run(meter, amp, sec):
        t = np.arange(16000 * sec) / 16000
        x = (np.sin(2 * np.pi * 200 * t) * amp).clip(-32768, 32767).astype(np.int16)
        out = None
        for i in range(0, len(x), 512):
            out = meter.feed(x[i:i + 512]) or out
        return out["warning"]

    m = LevelMeter()
    assert run(m, 100, 10) is None  # too early to judge
    assert run(m, 100, 6) == "low"
    assert run(m, 1000, 5) is None
    assert run(m, 0, 5) is None  # a pause between sentences
    assert run(m, 0, 16) == "low"
    assert run(m, 200000, 5) == "loud"
