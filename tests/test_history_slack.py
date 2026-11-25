import pytest
from fastapi.testclient import TestClient

from reinvent_scribe.config import Settings
from reinvent_scribe.server import create_app
from reinvent_scribe.store import SessionStore
from reinvent_scribe.summarize import build_daily_report, build_slack_post
from tests.test_server import FakeMic, FakeTranscriber, fake_transcribe_file


class LLM:
    def __init__(self, reply="回答", up=True):
        self.reply, self.up, self.calls = reply, up, []

    def available(self):
        return self.up

    def chat(self, messages, max_tokens=1000):
        self.calls.append(messages)
        return self.reply if self.up else None


def make(tmp_path, llm):
    return TestClient(create_app(
        settings=Settings(data_dir=tmp_path), mic_factory=FakeMic,
        transcriber_factory=FakeTranscriber,
        transcribe_file_fn=fake_transcribe_file, llm=llm, model_name="x",
        streamer_factory=None))


def record(c, sid):
    r = c.post("/api/sessions", json={"session_id": sid, "title": "T"}).json()
    c.post("/api/sessions/stop")
    return f"/api/sessions/{r['date']}/{r['dirname']}"


def test_chat_is_kept_per_recording(tmp_path):
    llm = LLM("1行目\n\n2行目")
    with make(tmp_path, llm) as c:
        a, b = record(c, "A"), record(c, "B")
        c.post(a + "/chat", json={"question": "Aの質問"})
        c.post(b + "/chat", json={"question": "Bの質問"})
        da, db = c.get(a).json(), c.get(b).json()
    assert da["chat"] == [{"role": "user", "content": "Aの質問"},
                          {"role": "assistant", "content": "1行目\n\n2行目"}]
    assert db["chat"][0]["content"] == "Bの質問"


def test_detail_has_captions_translation_and_summary(tmp_path):
    with make(tmp_path, LLM("訳")) as c:
        d = c.get(record(c, "A")).json()
    assert d["segments"] == [{"ts": 0, "text": "hello from fake", "ja": "訳"}]
    assert d["summary"] == "訳"


def test_followup_question_gets_history(tmp_path):
    llm = LLM("最初の回答")
    with make(tmp_path, llm) as c:
        a = record(c, "A")
        c.post(a + "/chat", json={"question": "最初"})
        c.post(a + "/chat", json={"question": "続き"})
    assert [m["content"] for m in llm.calls[-1][1:]] == ["最初", "最初の回答", "続き"]


def test_missing_detail_404(tmp_path):
    with make(tmp_path, LLM()) as c:
        assert c.get("/api/sessions/2026-12-01/none").status_code == 404


@pytest.fixture
def sessions(tmp_path):
    s = SessionStore(tmp_path).create("KEY001", "Keynote", memo="社内で検証したい")
    s.append_segment(0, "x" * 50000 + " ENDING")
    return [s]


def test_slack_post_mentions_manager_without_markdown(sessions):
    out = build_slack_post(sessions, LLM("## 見出し\n**太字**\n- 項目"), "日報", "@山田")
    assert out.startswith("@山田\n")
    assert "見出し\n太字\n・項目" in out
    assert "#" not in out and "**" not in out


def test_slack_post_offline_uses_facts_only(sessions):
    out = build_slack_post(sessions, LLM(up=False), "日報", "@山田")
    assert out.startswith("@山田\n")
    assert "[KEY001] Keynote" in out and "社内で検証したい" in out


def test_slack_post_empty_day():
    assert "出席セッションはありません" in build_slack_post([], LLM(), "", "@山田")


def test_report_reads_whole_session_and_prefers_hq(sessions):
    llm = LLM()
    build_daily_report(sessions, llm)
    assert "ENDING" in str(llm.calls)
    (sessions[0].dir / "transcript_hq.md").write_text("HQTEXT", encoding="utf-8")
    llm2 = LLM()
    build_daily_report(sessions, llm2)
    assert "HQTEXT" in str(llm2.calls)


def test_report_endpoint_returns_and_saves_slack(tmp_path):
    with make(tmp_path, LLM("本文")) as c:
        url = record(c, "A")
        date = url.split("/")[3]
        r = c.post(f"/api/daily-report?date={date}", json={"mention": "@山田"}).json()
    assert r["slack"].startswith("@山田\n")
    assert (tmp_path / date / "daily_slack.txt").read_text() == r["slack"]
    assert "出張日報" in r["markdown"]


class Flaky(LLM):
    def __init__(self, fails):
        super().__init__("復帰した要約")
        self.fails = fails

    def chat(self, messages, max_tokens=1000):
        super().chat(messages)
        return None if len(self.calls) <= self.fails else self.reply


def test_report_retries_temporary_llm_failure(sessions, monkeypatch):
    monkeypatch.setattr("reinvent_scribe.summarize.RETRY_WAITS", (0, 0))
    assert "復帰した要約" in build_daily_report(sessions, Flaky(2))


def test_failed_summary_is_not_reported_as_offline(sessions, monkeypatch):
    monkeypatch.setattr("reinvent_scribe.summarize.RETRY_WAITS", (0, 0))
    md = build_daily_report(sessions, Flaky(99))
    assert "オフライン" not in md and "要約を生成できませんでした" in md
