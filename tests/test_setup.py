import os

import pytest
from fastapi.testclient import TestClient

from reinvent_scribe import setup
from reinvent_scribe.config import Settings
from reinvent_scribe.server import create_app
from tests.test_server import FakeMic, FakeTranscriber, fake_transcribe_file


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in setup.ALLOWED:
        monkeypatch.setenv(k, "x")  # so teardown restores whatever tests set
        monkeypatch.delenv(k)
    monkeypatch.setattr(setup, "has_model", lambda n: n == "small.en")
    app = create_app(settings=Settings(data_dir=tmp_path / "d"), mic_factory=FakeMic,
                     transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, model_name="small.en")
    with TestClient(app) as c:
        yield c


def test_save_env_keeps_other_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# note\nLLM_API_KEY=old\n#LLM_MODEL=kept\nOTHER=1\nLLM_API_KEY=dup\n")
    setup.save_env(p, {"LLM_API_KEY": " new ", "GMAIL_ADDRESS": "a@example.com"})
    assert p.read_text() == "# note\nLLM_API_KEY=new\n#LLM_MODEL=kept\nOTHER=1\nGMAIL_ADDRESS=a@example.com\n"
    if os.name == "posix":
        assert p.stat().st_mode & 0o077 == 0
    for bad in ({"PATH": "x"}, {"LLM_API_KEY": "a\nDATA_DIR=/"}, {"LLM_API_KEY": "日本"}):
        with pytest.raises(ValueError):
            setup.save_env(p, bad)


def test_keys_take_effect_at_once_and_are_never_returned(client, tmp_path, monkeypatch):
    s = client.get("/api/setup").json()
    assert not s["llm"]["set"] and not s["gmail"]["set"] and s["models"]["realtime"]
    r = client.post("/api/setup", json={"values": {
        "LLM_API_KEY": "sk-secret", "LLM_BASE_URL": "https://api.openai.com/v1",
        "LLM_MODEL": "gpt-6-luna", "GMAIL_ADDRESS": ""}})
    assert r.status_code == 200
    body = client.get("/api/setup")
    assert body.json()["llm"] == {"set": True, "model": "gpt-6-luna", "provider": "openai"}
    assert "sk-secret" not in body.text + client.get("/api/status").text
    assert client.get("/api/status").json()["llm_ok"] is True
    assert "GMAIL" not in (tmp_path / ".env").read_text()
    assert client.post("/api/setup", json={"values": {"PATH": "x"}}).status_code == 422
    assert "APIキー" in client.post("/api/setup/test/stt").json()["error"]
    assert client.post("/api/setup/test/nope").status_code == 404


def test_setup_is_refused_while_recording(client):
    client.post("/api/sessions", json={"session_id": "K", "title": "t"})
    assert client.post("/api/setup", json={"values": {"LLM_API_KEY": "x"}}).status_code == 409
    assert client.post("/api/setup/test/mic").status_code == 409
    client.post("/api/sessions/stop")


def test_other_sites_cannot_call_the_app(client):
    evil = {"origin": "https://evil.example"}
    assert client.post("/api/setup/open-extension", headers=evil).status_code == 403
    assert client.get("/api/status", headers={"origin": "http://testserver"}).status_code == 200
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers=evil) as ws:
            ws.receive_text()


def test_extension_path_hides_user_name(monkeypatch, tmp_path):
    from pathlib import Path
    from reinvent_scribe import setup
    home = (tmp_path / "taro").resolve()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(setup, "EXTENSION_DIR", home / "app" / "extension")
    assert setup.shown_extension_dir() == str(home.parent / "xxxx" / "app" / "extension")
    monkeypatch.setattr(setup, "EXTENSION_DIR", tmp_path.resolve() / "app" / "extension")
    assert setup.shown_extension_dir() == "…/app/extension"
