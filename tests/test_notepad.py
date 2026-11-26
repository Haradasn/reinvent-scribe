import io
import zipfile

from reinvent_scribe.photos import PhotoStore
from reinvent_scribe.summarize import build_daily_report, build_slack_post
from tests.test_photos import client, rec  # noqa: F401  (fixture)
from tests.test_summarize import FakeLLM

PAD = "料金の話\n\n> [00:12:34] 料金は半額になります\n→ 公式で確認する\n"


def test_multiline_memo_stays_under_its_session(tmp_path):
    s = rec(tmp_path, 1000, 1600, memo=PAD)
    md = build_daily_report([s], FakeLLM(up=False))
    assert "- [K] 料金の話\n  > [00:12:34] 料金は半額になります\n  → 公式で確認する" in md
    slack = build_slack_post([s], FakeLLM(up=False), md, "")
    assert "・[K] 料金の話" in slack and "→ 公式で確認する" in slack


def test_memo_in_chat_context_and_zip(tmp_path):
    s = rec(tmp_path, 1000, 1600, memo=PAD)
    assert "聴講者のメモ" in s.memo_text() and "→ 公式で確認する" in s.memo_text()
    assert rec(tmp_path, 1000, 1600, name="E").memo_text() == ""
    z = zipfile.ZipFile(io.BytesIO(PhotoStore(tmp_path, "UTC").article_zip(s)))
    assert "## 自分のメモ\n\n料金の話\n> [00:12:34]" in z.read("article_source.md").decode()


def test_memo_patched_while_recording(client):  # noqa: F811
    d = client.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
    base = f"/api/sessions/{d['date']}/{d['dirname']}"
    client.patch(base + "/memo", json={"memo": "あとで確認"})
    client.post("/api/sessions/stop")
    assert client.get(base).json()["meta"]["memo"] == "あとで確認"
