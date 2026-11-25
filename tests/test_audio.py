import wave

import numpy as np

from reinvent_scribe.audio import WavWriter


def _frames(n):
    return (np.sin(np.linspace(0, 100, n)) * 10000).astype(np.int16)


def test_wav_written_incrementally(tmp_path):
    path = tmp_path / "audio.wav"
    w = WavWriter(path)
    w.write(_frames(16000))
    w.write(_frames(8000))
    # without closing, the file on disk must already be a valid WAV
    with wave.open(str(path), "rb") as r:
        assert r.getframerate() == 16000
        assert r.getnchannels() == 1
        assert r.getnframes() == 24000
    w.close()


def test_close_finalizes(tmp_path):
    path = tmp_path / "audio.wav"
    w = WavWriter(path)
    w.write(_frames(1000))
    w.close()
    with wave.open(str(path), "rb") as r:
        assert r.getnframes() == 1000


def test_mic_stream_wires_callback():
    from reinvent_scribe.audio import MicStream
    received = []
    ms = MicStream(on_frames=received.append)
    fake = np.zeros((480, 1), dtype=np.int16)
    ms._callback(fake, 480, None, None)
    assert len(received) == 1
    assert received[0].shape == (480,)
