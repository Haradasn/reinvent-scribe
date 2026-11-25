from types import SimpleNamespace

import numpy as np

from reinvent_scribe.transcribe import Transcriber, load_glossary, transcribe_file


def test_load_glossary_missing_returns_empty(tmp_path):
    prompt, truncated = load_glossary(tmp_path / "nope.txt")
    assert prompt == ""
    assert truncated is False


def test_load_glossary_truncates_long(tmp_path):
    path = tmp_path / "glossary.txt"
    path.write_text("\n".join(f"Service{i}" for i in range(500)))
    prompt, truncated = load_glossary(path)
    assert truncated is True
    assert len(prompt.split()) <= 180
    assert prompt.startswith("Service0")


class FakeModel:
    def __init__(self, segments):
        self._segments = segments
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        if not hasattr(self, "calls_audio"):
            self.calls_audio = []
        self.calls_audio.append(audio)
        segs = [SimpleNamespace(start=s, end=s + 1.0, text=t)
                for s, t in self._segments]
        return iter(segs), SimpleNamespace()


def test_transcriber_emits_segments():
    model = FakeModel([(0.0, " Hello world.")])
    got = []
    t = Transcriber(model_name="small.en", initial_prompt="AWS Lambda",
                    on_segment=lambda ts, text: got.append((ts, text)),
                    model=model)
    t.feed(np.zeros(16000 * 5, dtype=np.float32))
    t.flush()
    assert got and got[0][1] == "Hello world."
    assert model.calls[0]["initial_prompt"] == "AWS Lambda"


def test_transcriber_offsets_across_chunks():
    model = FakeModel([(0.0, " a")])
    got = []
    t = Transcriber(model_name="small.en", initial_prompt="",
                    on_segment=lambda ts, text: got.append(ts), model=model)
    t.feed(np.zeros(16000 * 5, dtype=np.float32))
    t.flush()
    t.feed(np.zeros(16000 * 5, dtype=np.float32))
    t.flush()
    assert got[1] == 5.0  # second chunk's segment offset by consumed audio


def test_transcribe_file_returns_segments(tmp_path, monkeypatch):
    import reinvent_scribe.transcribe as tr
    model = FakeModel([(0.0, " High quality."), (4.0, " Second.")])
    monkeypatch.setattr(tr, "_load_model", lambda name: model)
    wav = tmp_path / "a.wav"
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    segs = transcribe_file(wav, model_name="large-v3-turbo", initial_prompt="x")
    assert segs == [(0.0, "High quality."), (4.0, "Second.")]


def test_glossary_keeps_multiword_terms_and_skips_comments(tmp_path):
    from reinvent_scribe.transcribe import load_keyterms
    path = tmp_path / "glossary.txt"
    path.write_text("# note\nStep Functions\n\nKarpenter\nKarpenter\n" + "x" * 51 + "\n")
    assert load_glossary(path) == ("Step Functions, Karpenter, " + "x" * 51, False)
    assert load_keyterms(path) == ["Step Functions", "Karpenter"]


def test_keyterms_capped_at_100(tmp_path):
    from reinvent_scribe.transcribe import load_keyterms
    path = tmp_path / "glossary.txt"
    path.write_text("\n".join(f"T{i}" for i in range(150)))
    assert load_keyterms(path) == [f"T{i}" for i in range(100)]


def test_bundled_glossary_fits_assemblyai():
    from pathlib import Path
    from reinvent_scribe.transcribe import _glossary_terms, load_keyterms
    path = Path(__file__).parent.parent / "glossary.txt"
    terms = _glossary_terms(path)
    assert all(len(t) <= 50 for t in terms)  # none silently dropped
    assert load_keyterms(path) == terms[:100]


def test_old_space_separated_line_is_split_for_assemblyai(tmp_path):
    from reinvent_scribe.transcribe import load_keyterms
    path = tmp_path / "glossary.txt"
    path.write_text("AWS Amazon EC2 S3 Lambda DynamoDB Aurora RDS Redshift SageMaker\nKiro\n")
    assert load_keyterms(path)[:3] == ["AWS", "Amazon", "EC2"]
    assert load_keyterms(path)[-1] == "Kiro"


def test_long_first_term_does_not_empty_whisper_prompt(tmp_path):
    path = tmp_path / "glossary.txt"
    path.write_text(" ".join(["w"] * 200) + "\nKiro\nKarpenter\n")
    assert load_glossary(path) == ("Kiro, Karpenter", True)


def test_personal_glossary_comes_first(tmp_path):
    from reinvent_scribe.transcribe import load_keyterms
    mine, shared = tmp_path / "mine.txt", tmp_path / "shared.txt"
    shared.write_text("Kiro\nKarpenter\n")
    mine.write_text("AgentCore\nKiro\n")
    assert load_keyterms([mine, shared]) == ["AgentCore", "Kiro", "Karpenter"]
    assert load_keyterms([tmp_path / "none.txt", shared]) == ["Kiro", "Karpenter"]
