import httpx
import pytest

from reinvent_scribe.config import Settings
from reinvent_scribe.llm import LLMClient, transcript_chat


def settings(**kw):
    base = dict(llm_base_url="https://api.example.com/v1",
                llm_api_key="sk-test", llm_model="gpt-4o-mini")
    base.update(kw)
    return Settings(**base)


def client_with(handler):
    c = LLMClient(settings())
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_available_false_without_config():
    assert LLMClient(Settings()).available() is False
    assert LLMClient(settings()).available() is True


def test_chat_returns_content():
    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "こんにちは"}}]})
    c = client_with(handler)
    assert c.chat([{"role": "user", "content": "hi"}]) == "こんにちは"


def test_chat_returns_none_on_error():
    c = client_with(lambda request: httpx.Response(500, text="boom"))
    assert c.chat([{"role": "user", "content": "hi"}]) is None

    def raise_timeout(request):
        raise httpx.ConnectTimeout("timeout")
    c2 = client_with(raise_timeout)
    assert c2.chat([{"role": "user", "content": "hi"}]) is None


def test_chat_none_when_unconfigured():
    assert LLMClient(Settings()).chat([{"role": "user", "content": "hi"}]) is None


def test_transcript_chat_truncates_long_transcript():
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "answer"}}]})

    c = client_with(handler)
    transcript = "HEAD " + ("x" * 20000) + " TAIL"
    out = transcript_chat(c, transcript, history=[], question="what?")
    assert out == "answer"
    system = captured["body"]["messages"][0]["content"]
    assert "HEAD" in system and "TAIL" in system
    assert len(system) < 12000


def test_overloaded_model_falls_back_and_is_skipped_next_time():
    import json
    tried = []

    def handler(request):
        m = json.loads(request.content)["model"]
        tried.append(m)
        if m == "gpt-4o-mini":
            return httpx.Response(503, text="high demand")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = client_with(handler)
    c.settings.llm_fallback_models = "backup"
    assert c.chat([]) == "ok" and c.chat([]) == "ok"
    assert tried == ["gpt-4o-mini", "backup", "backup"]
    assert c.last_error is None


def test_error_is_reported_and_bad_key_is_not_retried():
    n = []

    def handler(request):
        n.append(1)
        return httpx.Response(401)

    c = client_with(handler)
    c.settings.llm_fallback_models = "backup"
    assert c.chat([]) is None
    assert c.last_error == "auth" and len(n) == 1


def test_cut_off_answer_is_not_used_and_gemini_gets_room_for_thinking():
    import json
    bodies = []

    def handler(request):
        b = json.loads(request.content)
        bodies.append(b)
        cut = b["model"] == "gpt-4o-mini"
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length" if cut else "stop",
            "message": {"content": "half" if cut else "whole"}}]})

    c = client_with(handler)
    c.settings.llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    c.settings.llm_fallback_models = "backup"
    assert c.chat([]) == "whole"
    assert bodies[0]["max_tokens"] == 8192
    assert bodies[0]["reasoning_effort"] == "low"


def test_depleted_credits_are_reported_without_trying_other_models():
    n = []

    def handler(request):
        n.append(1)
        return httpx.Response(402)

    c = client_with(handler)
    c.settings.llm_fallback_models = "backup"
    assert c.chat([]) is None and c.last_error == "payment" and len(n) == 1


def test_openai_request_and_web_search_send_only_the_question():
    import json
    got = []

    def handler(request):
        b = json.loads(request.content)
        got.append(b)
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"status": "completed", "output": [
                {"type": "web_search_call"}, {"type": "message", "content": [{
                    "type": "output_text", "text": "答え。([a](https://a.com))",
                    "annotations": [{"title": "A", "url": "https://a.com/?utm_source=openai"},
                                    {"title": "B", "url": "javascript:x"}]}]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = client_with(handler)
    c.settings.llm_base_url, c.settings.llm_model = "https://api.openai.com/v1", "gpt-6-luna"
    assert c.chat([]) == "ok"
    assert got[0]["reasoning_effort"] == "none" and "max_tokens" not in got[0]
    assert c.web_search("質問") == ("答え。", [{"title": "A", "url": "https://a.com/"}])
    assert got[1]["input"].endswith("質問") and got[1]["store"] is False
    assert LLMClient(settings()).can_search() is False
