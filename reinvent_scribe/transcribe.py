import logging
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

# initial_prompt is clipped around 224 tokens; ~1.3 tokens per word
_MAX_PROMPT_WORDS = 170


def load_glossary(path: Path) -> tuple[str, bool]:
    path = Path(path)
    if not path.exists():
        return "", False
    words = path.read_text(encoding="utf-8").split()
    if not words:
        return "", False
    truncated = len(words) > _MAX_PROMPT_WORDS
    if truncated:
        log.warning("glossary.txt exceeds ~224 tokens; using first %d words",
                    _MAX_PROMPT_WORDS)
        words = words[:_MAX_PROMPT_WORDS]
    return ", ".join(words), truncated


def _load_model(model_name: str):
    from faster_whisper import WhisperModel
    return WhisperModel(model_name, compute_type="int8")


def pick_model(preferred: str = "small.en") -> str:
    """Benchmark the preferred model on silence; fall back if slower than 1x."""
    try:
        model = _load_model(preferred)
        audio = np.zeros(16000 * 10, dtype=np.float32)
        start = time.monotonic()
        segments, _ = model.transcribe(audio, language="en")
        list(segments)
        elapsed = time.monotonic() - start
        if elapsed > 10.0:
            log.warning("%s too slow (%.1fs for 10s audio); falling back to base.en",
                        preferred, elapsed)
            return "base.en"
        return preferred
    except Exception:
        log.exception("benchmark failed; falling back to base.en")
        return "base.en"


class Transcriber:
    """Buffers 16kHz float32 PCM and emits confirmed segments via on_segment."""

    CHUNK_SEC = 5.0

    def __init__(self, model_name: str, initial_prompt: str,
                 on_segment: Callable[[float, str], None], model=None):
        self.model = model if model is not None else _load_model(model_name)
        self.initial_prompt = initial_prompt
        self.on_segment = on_segment
        self._buffer = np.zeros(0, dtype=np.float32)
        self._consumed_sec = 0.0

    def feed(self, pcm: np.ndarray) -> None:
        self._buffer = np.concatenate([self._buffer, pcm.astype(np.float32)])
        if len(self._buffer) >= int(self.CHUNK_SEC * 16000):
            self._transcribe_buffer()

    def flush(self) -> None:
        if len(self._buffer) > 0:
            self._transcribe_buffer()

    def stop(self) -> None:
        self.flush()

    def _transcribe_buffer(self) -> None:
        audio = self._buffer
        self._buffer = np.zeros(0, dtype=np.float32)
        kwargs = dict(language="en", vad_filter=True)
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
        try:
            segments, _ = self.model.transcribe(audio, **kwargs)
            for seg in segments:
                text = seg.text.strip()
                if text:
                    self.on_segment(self._consumed_sec + seg.start, text)
        except Exception:
            log.exception("transcription chunk failed; continuing")
        self._consumed_sec += len(audio) / 16000.0


def transcribe_file(wav_path: Path, model_name: str = "large-v3-turbo",
                    initial_prompt: str = "") -> list[tuple[float, str]]:
    """Second-pass high-quality transcription of a saved WAV."""
    model = _load_model(model_name)
    kwargs = dict(language="en", vad_filter=True)
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    segments, _ = model.transcribe(str(wav_path), **kwargs)
    return [(seg.start, seg.text.strip()) for seg in segments if seg.text.strip()]
