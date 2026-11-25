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
