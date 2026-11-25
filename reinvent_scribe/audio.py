import struct
from collections import deque
from pathlib import Path

import numpy as np


class WavWriter:
    """Incremental 16-bit mono WAV writer.

    The RIFF/data size headers are rewritten after every write so the file
    on disk is always a valid WAV, even if the process dies mid-recording.
    """

    def __init__(self, path: Path, samplerate: int = 16000):
        self.path = Path(path)
        self.samplerate = samplerate
        self._data_bytes = 0
        self._f = open(self.path, "wb")
        self._write_header()

    def _write_header(self) -> None:
        f = self._f
        f.seek(0)
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + self._data_bytes))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, self.samplerate,
                            self.samplerate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", self._data_bytes))

    def write(self, frames: np.ndarray) -> None:
        data = np.asarray(frames, dtype=np.int16).tobytes()
        self._f.seek(44 + self._data_bytes)
        self._f.write(data)
        self._data_bytes += len(data)
        self._write_header()
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._write_header()
            self._f.close()


class LevelMeter:
    """Input level every 100ms, and whether speech is too quiet or clipping.

    Judged on the loud part of the last 15s, so ordinary pauses between
    sentences don't count. Thresholds come from real recordings: captions
    lost words around 70 and were complete above about 160.
    """

    BLOCK, WINDOW = 1600, 150
    LOW, LOW_CLEAR = 130, 180

    def __init__(self):
        self._buf = np.zeros(0, dtype=np.int16)
        self._rms: deque = deque(maxlen=self.WINDOW)
        self._clips: deque = deque(maxlen=self.WINDOW)
        self.warning: str | None = None

    def feed(self, frames: np.ndarray) -> dict | None:
        self._buf = np.concatenate([self._buf, frames])
        rms = None
        while len(self._buf) >= self.BLOCK:
            block = self._buf[:self.BLOCK].astype(np.float32)
            self._buf = self._buf[self.BLOCK:]
            rms = float(np.sqrt(np.mean(block ** 2)))
            self._rms.append(rms)
            self._clips.append(bool((np.abs(block) >= 32700).sum() >= 8))
        if rms is None:
            return None
        loud = float(np.percentile(self._rms, 90))
        if sum(self._clips) >= 15:
            self.warning = "loud"
        elif len(self._rms) < self.WINDOW:
            self.warning = None
        elif loud < (self.LOW_CLEAR if self.warning == "low" else self.LOW):
            self.warning = "low"
        else:
            self.warning = None
        return {"rms": round(rms), "warning": self.warning}


class MicStream:
    """Thin wrapper around sounddevice.InputStream (16kHz mono int16)."""

    def __init__(self, on_frames, samplerate: int = 16000):
        self.on_frames = on_frames
        self.samplerate = samplerate
        self._stream = None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            import logging
            logging.getLogger(__name__).warning("audio input status: %s", status)
        self.on_frames(np.asarray(indata).reshape(-1).copy())

    def start(self) -> None:
        import sounddevice as sd
        self._stream = sd.InputStream(
            samplerate=self.samplerate, channels=1, dtype="int16",
            callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def list_devices() -> list[str]:
    import sounddevice as sd
    return [d["name"] for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
