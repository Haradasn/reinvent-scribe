import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)

_HEAD_CHARS = 2000
_TAIL_CHARS = 6000


class LLMClient:
    """OpenAI-compatible Chat Completions client. Never raises from chat()."""

    def __init__(self, settings: Settings, timeout: float = 15.0):
        self.settings = settings
        self._client = httpx.Client(timeout=timeout)

    def available(self) -> bool:
        s = self.settings
        return bool(s.llm_base_url and s.llm_api_key and s.llm_model)

    def chat(self, messages: list[dict], max_tokens: int = 1000) -> str | None:
        if not self.available():
            return None
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            resp = self._client.post(
                url,
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json={"model": self.settings.llm_model,
                      "messages": messages,
                      "max_tokens": max_tokens},
            )
            if resp.status_code != 200:
                log.warning("LLM API returned %d: %s", resp.status_code,
                            resp.text[:200])
                return None
            return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            log.warning("LLM request failed", exc_info=True)
            return None


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
