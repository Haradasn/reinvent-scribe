import time

from tests.test_history_slack import LLM, make, record


def _wait_for(path, text, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return True
        time.sleep(0.02)
    return False


def test_summary_is_redone_from_whole_transcript_after_stop(tmp_path):
    llm = LLM("全体の要約")
    with make(tmp_path, llm) as c:
        url = record(c, "W")
        date, dirname = url.split("/")[-2:]
        assert _wait_for(tmp_path / date / dirname / "summary.md", "全体の要約")
    assert any("全体の日本語要約" in str(m) for m in llm.calls)


def test_offline_keeps_rolling_summary(tmp_path):
    llm = LLM(up=False)
    with make(tmp_path, llm) as c:
        url = record(c, "O")
        time.sleep(0.2)
    date, dirname = url.split("/")[-2:]
    assert not (tmp_path / date / dirname / "summary.md").exists()
    assert not any("全体の日本語要約" in str(m) for m in llm.calls)


def test_rolling_prompt_keeps_earlier_topics():
    from reinvent_scribe.summarize import RollingSummarizer

    seen = []

    class Rec:
        def available(self):
            return True

        def chat(self, messages, max_tokens=1000):
            seen.append(messages[0]["content"])
            return "要約"

    s = RollingSummarizer(Rec(), 0)
    s.add("hello world")
    s.summarize()
    assert "前半の話題を省かない" in seen[0]
