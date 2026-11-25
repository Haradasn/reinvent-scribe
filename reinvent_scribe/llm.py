import logging
import time

import httpx

from .config import Settings

log = logging.getLogger(__name__)

_HEAD_CHARS = 2000
_TAIL_CHARS = 6000

# tried in order when the configured model is overloaded (503/429/timeout)
_GEMINI_FALLBACKS = "gemini-3.6-flash,gemini-3.5-flash"
_PREFER_SEC = 120


class LLMClient:
    """OpenAI-compatible Chat Completions client. Never raises from chat()."""

    def __init__(self, settings: Settings, timeout: float = 10.0):
        self.settings = settings
        self._client = httpx.Client(timeout=timeout)
        self._preferred = (None, 0.0)
        self.last_error: str | None = None

    def available(self) -> bool:
        s = self.settings
        return bool(s.llm_base_url and s.llm_api_key and s.llm_model)

    def chat(self, messages: list[dict], max_tokens: int = 1000) -> str | None:
        if not self.available():
            return None
        last_error = "unavailable"
        for model in self._model_order():
            out, last_error, retryable = self._request(model, messages, max_tokens)
            if out:
                self._preferred = (model, time.monotonic() + _PREFER_SEC)
                self.last_error = None
                return out
            if not retryable:
                break
        self.last_error = last_error
        return None

    def _models(self) -> list[str]:
        s = self.settings
        extra = s.llm_fallback_models
        if extra is None:
            extra = _GEMINI_FALLBACKS if "googleapis.com" in s.llm_base_url else ""
        names = [s.llm_model, *(m.strip() for m in extra.split(","))]
        return list(dict.fromkeys(m for m in names if m))

    def _model_order(self) -> list[str]:
        models = self._models()
        model, until = self._preferred
        if model in models and time.monotonic() < until:
            models.remove(model)
            models.insert(0, model)  # skip the overloaded one for a while
        return models

    def _request(self, model, messages, max_tokens):
        """-> (text, error, worth trying another model)"""
        s = self.settings
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if "googleapis.com" in s.llm_base_url:
            # Gemini counts hidden thinking against max_tokens, which would
            # cut the visible answer short.
            body.update(max_tokens=max(max_tokens, 8192), reasoning_effort="low")
        try:
            resp = self._client.post(
                s.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {s.llm_api_key}"},
                json=body,
            )
            if resp.status_code != 200:
                log.warning("LLM %s returned %d: %s", model, resp.status_code,
                            resp.text[:700])
                error = {503: "busy", 429: "rate_limited", 401: "auth",
                         403: "auth", 402: "payment"}.get(resp.status_code, "error")
                return None, error, error not in ("auth", "payment")
            choice = resp.json()["choices"][0]
            if choice.get("finish_reason") == "length":
                log.warning("LLM %s answer was cut off", model)
                return None, "error", True  # never pass on half an answer
            out = choice["message"].get("content")
            return out, "error", True
        except httpx.TimeoutException:
            log.warning("LLM %s timed out", model)
            return None, "busy", True
        except httpx.TransportError as e:
            log.warning("LLM unreachable: %r", e)
            return None, "network", False
        except Exception:
            log.warning("LLM request failed", exc_info=True)
            return None, "error", True


def _clip_transcript(transcript: str) -> str:
    if len(transcript) <= _HEAD_CHARS + _TAIL_CHARS:
        return transcript
    return (transcript[:_HEAD_CHARS] + "\n...(中略)...\n"
            + transcript[-_TAIL_CHARS:])


def transcript_chat(llm: LLMClient, transcript: str, history: list[dict],
                    question: str) -> str | None:
    system = (
        "あなたは技術カンファレンスのセッション聴講を支援するアシスタントです。"
        "以下はセッションの英語文字起こしです。これを文脈として、ユーザーの質問に"
        "日本語で簡潔に答えてください。\n\n---\n"
        + _clip_transcript(transcript)
    )
    messages = [{"role": "system", "content": system}, *history,
                {"role": "user", "content": question}]
    return llm.chat(messages)
