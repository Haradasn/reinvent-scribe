from datetime import datetime

import pytest

from reinvent_scribe.config import Settings
from reinvent_scribe.llm import LLMClient
from reinvent_scribe.store import SessionStore
from reinvent_scribe.summarize import RollingSummarizer, build_daily_report


class FakeLLM:
    def __init__(self, reply="要約です", up=True):
        self.reply = reply
        self.up = up
        self.calls = []

    def available(self):
        return self.up

    def chat(self, messages, max_tokens=1000):
        self.calls.append(messages)
        return self.reply if self.up else None


def test_due_respects_interval():
    rs = RollingSummarizer(FakeLLM(), interval_sec=180)
    rs.add("hello")
    assert rs.due(now=10.0) is False
    rs._last_run = 0.0
    assert rs.due(now=200.0) is True


def test_summarize_skips_when_unavailable():
    rs = RollingSummarizer(FakeLLM(up=False), interval_sec=1)
    rs.add("some text")
    assert rs.summarize() is None  # no exception, quiet skip


def test_summarize_accumulates():
    llm = FakeLLM(reply="ここまでの要約")
    rs = RollingSummarizer(llm, interval_sec=1)
    rs.add("new transcript text")
    out = rs.summarize()
    assert out == "ここまでの要約"
    assert rs.current == "ここまでの要約"
    rs.add("more")
    rs.summarize()
    # second call passes previous summary as context
    assert "ここまでの要約" in str(llm.calls[1])


def test_summarize_without_new_text_returns_current():
    llm = FakeLLM()
    rs = RollingSummarizer(llm, interval_sec=1)
    assert rs.summarize() is None
    assert llm.calls == []


@pytest.fixture
def day_store(tmp_path):
    store = SessionStore(tmp_path)
    s = store.create("KEY001", "Keynote", memo="great demos",
                     started_at=datetime(2026, 12, 1, 8, 30))
    s.append_segment(0.0, "Welcome to re:Invent.")
    return store


def test_daily_report_with_llm(day_store):
    llm = FakeLLM(reply="- 新サービス発表\n- 性能改善の話")
    md = build_daily_report(day_store.list_for_date("2026-12-01"), llm)
    assert "KEY001" in md and "Keynote" in md
    assert "新サービス発表" in md
    assert "great demos" in md


def test_daily_report_offline_fallback(day_store):
    md = build_daily_report(day_store.list_for_date("2026-12-01"), FakeLLM(up=False))
    assert "KEY001" in md
    assert "great demos" in md


def test_daily_report_empty_day():
    md = build_daily_report([], FakeLLM())
    assert "出席セッションはありません" in md
