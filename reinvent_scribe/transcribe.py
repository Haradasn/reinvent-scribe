import logging
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

# initial_prompt is clipped around 224 tokens; ~1.3 tokens per word
_MAX_PROMPT_WORDS = 170


def _glossary_terms(path: Path) -> list[str]:
    """One term per line; blank lines and # comments are skipped."""
    path = Path(path)
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in terms:
            terms.append(line)
    return terms


def load_glossary(path: Path) -> tuple[str, bool]:
    """Whisper initial_prompt built from glossary.txt."""
    terms, words, truncated = _glossary_terms(path), 0, False
    kept = []
    for t in terms:
        words += len(t.split())
        if words > _MAX_PROMPT_WORDS:
            truncated = True
            words -= len(t.split())
            continue  # a shorter term later may still fit
        kept.append(t)
    if truncated:
        log.warning("glossary.txt exceeds ~224 tokens; using the first %d terms",
                    len(kept))
    return ", ".join(kept), truncated


# AssemblyAI keyterms_prompt: more than 100 terms is an error, longer
# than 50 characters is ignored
_MAX_KEYTERMS, _MAX_KEYTERM_CHARS = 100, 50


def load_keyterms(path: Path) -> list[str]:
    terms = []
    for t in _glossary_terms(path):
        if len(t) <= _MAX_KEYTERM_CHARS:
            terms.append(t)
        else:  # likely the old format: many terms on one line
            log.warning("glossary.txt line over %d characters; sending its words "
                        "one by one: %s", _MAX_KEYTERM_CHARS, t[:60])
            terms += [w for w in t.split()
                      if w not in terms and len(w) <= _MAX_KEYTERM_CHARS]
    if len(terms) > _MAX_KEYTERMS:
        log.warning("glossary.txt has %d terms; AssemblyAI gets the first %d",
                    len(terms), _MAX_KEYTERMS)
    return terms[:_MAX_KEYTERMS]


_model_cache: dict = {}


def _load_model(model_name: str):
    if model_name not in _model_cache:
        from faster_whisper import WhisperModel
        _model_cache[model_name] = WhisperModel(model_name, compute_type="int8")
    return _model_cache[model_name]


def _read_wav(path: Path) -> np.ndarray:
    """Load a 16kHz mono int16 WAV as float32, avoiding PyAV entirely."""
    import wave
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def level_audio(x: np.ndarray, target: float = 0.06) -> np.ndarray:
    """Bring quiet speech up to a steady level.

    A distant speaker is recorded so softly that Whisper's voice detection
    drops whole sentences. Uses the loud part of the surrounding ~6s so
    pauses between words are not blown up.
    """
    n, k = 8000, len(x) // 8000 + 1
    if len(x) == 0:
        return x
    rms = np.array([np.sqrt(np.mean(b ** 2) + 1e-12) if len(b) else 1e-6
                    for b in (x[i * n:(i + 1) * n] for i in range(k))])
    loud = np.array([np.percentile(rms[max(0, i - 6):i + 7], 80) for i in range(k)])
    gain = np.clip(target / loud, 1, 20)
    centers = np.arange(k) * n + n / 2
    out = np.empty(len(x), dtype=np.float32)
    for s in range(0, len(x), 960000):  # a minute at a time: keep memory flat
        e = min(s + 960000, len(x))
        out[s:e] = np.clip(x[s:e] * np.interp(np.arange(s, e), centers, gain), -1, 1)
    return out


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

    CHUNK_SEC = 3.0

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
            segments, _ = self.model.transcribe(level_audio(audio), **kwargs)
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
    kwargs = dict(language="en", vad_filter=True, condition_on_previous_text=False,
                  vad_parameters=dict(threshold=0.25, min_silence_duration_ms=2000,
                                      speech_pad_ms=600))
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    segments, _ = model.transcribe(level_audio(_read_wav(Path(wav_path))), **kwargs)
    return [(seg.start, seg.text.strip()) for seg in segments if seg.text.strip()]
