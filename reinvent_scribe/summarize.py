import logging
import re
import threading
import time

from .store import Session

log = logging.getLogger(__name__)

RETRY_WAITS = (2, 6)  # seconds; reports can wait out a temporary 503


def _chat_with_retry(llm, prompt: str) -> str | None:
    for wait in (0, *RETRY_WAITS):
        time.sleep(wait)
        out = llm.chat([{"role": "user", "content": prompt}])
        if out:
            return out
    return None


class RollingSummarizer:
    """Accumulates transcript text and produces a cumulative Japanese summary."""

    def __init__(self, llm, interval_sec: int):
        self.llm = llm
        self.interval_sec = interval_sec
        self.current: str = ""
        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._running = False
        self._last_run = time.monotonic()

    def add(self, text: str) -> None:
        with self._lock:
            self._pending.append(text)

    def due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            return (bool(self._pending) and not self._running
                    and (now - self._last_run) >= self.interval_sec)

    def summarize(self) -> str | None:
        with self._lock:
            self._last_run = time.monotonic()
            if not self._pending or self._running or not self.llm.available():
                return None
            self._running = True
            batch, self._pending = self._pending, []
        try:
            return self._summarize_batch(batch)
        finally:
            with self._lock:
                self._running = False

    def _summarize_batch(self, batch: list[str]) -> str | None:
        new_text = "\n".join(batch)
        prompt = (
            "あなたは技術カンファレンスのセッションを日本語で要約するアシスタントです。"
            "これまでの要約と新しい英語文字起こしを統合し、ここまでの内容がわかる"
            "累積の日本語要約(箇条書き、重要な発表・数字を優先)を返してください。\n\n"
            f"## これまでの要約\n{self.current or '(まだなし)'}\n\n"
            f"## 新しい文字起こし\n{new_text}"
        )
        out = self.llm.chat([{"role": "user", "content": prompt}])
        if out is None:
            with self._lock:  # quiet skip; re-queue batch for next attempt
                self._pending = batch + self._pending
            return None
        self.current = out
        return out


def _session_table(sessions: list[Session]) -> str:
    rows = ["| 時刻 | ID | セッション |", "| --- | --- | --- |"]
    for s in sessions:
        started = s.meta["started_at"][11:16]
        rows.append(f"| {started} | {s.meta['id']} | {s.meta['title']} |")
    return "\n".join(rows)


def build_daily_report(sessions: list[Session], llm) -> str:
    """Markdown 出張日報: 行動概要 → 各セッション要点 → 所感."""
    date = sessions[0].meta["started_at"][:10] if sessions else ""
    parts = [f"# 出張日報 {date}".rstrip(), ""]
    if not sessions:
        parts.append("本日の出席セッションはありません。")
        return "\n".join(parts)

    parts += ["## 本日の行動概要", "", _session_table(sessions), ""]

    parts.append("## 各セッションの要点")
    for s in sessions:
        parts += ["", f"### [{s.meta['id']}] {s.meta['title']}"]
        summary = None
        if llm.available():
            prompt = (
                "以下は技術カンファレンスのセッションの英語文字起こしです。"
                "上司への出張日報に載せる日本語の要点を2〜4行で書いてください。"
                "ブログ記事のように、何が発表され何が重要かが読んでわかる文体で。"
                "文字起こしにない内容を推測で補わず、内容が乏しい場合は"
                "「内容を確認できませんでした」とだけ書くこと。\n\n"
                + s.best_transcript()[:200000]
            )
            summary = _chat_with_retry(llm, prompt)
        parts.append(summary or "(要約を生成できませんでした)")

    memos = [f"- [{s.meta['id']}] {s.meta['memo']}" for s in sessions if s.meta.get("memo")]
    if memos:
        parts += ["", "## 所感・特筆事項", *memos]
    return "\n".join(parts)


def _to_slack_text(text: str) -> str:
    """Plain text that survives pasting into Slack's editor."""
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"^(\s*)[-*]\s+", r"\1・", text, flags=re.M)
    return text.replace("**", "").strip()


def build_slack_post(sessions: list[Session], llm, report_md: str,
                     mention: str) -> str:
    """Short post to the manager; copy-pasted into Slack by the user."""
    head = [mention.strip()] if mention.strip() else []
    if not sessions:
        return "\n".join(head + ["本日の出席セッションはありません。"])
    date = sessions[0].meta["started_at"][:10]
    head.append(f"お疲れ様です。re:Invent 出張日報({date})です。")
    body = None
    if llm.available():
        body = _chat_with_retry(llm,
            "以下の出張日報をもとに、上司へSlackで報告する投稿の本文を日本語で書いてください。"
            "です・ます調、全体で400字程度。【参加セッション】【主なポイント】【所感】の順。"
            "所感は日報の「所感・特筆事項」に書かれた内容だけを使い、なければ【所感】ごと省略。"
            "日報にない事実や感想を足さない。宛名・挨拶・表・マークダウン記号は不要で、"
            "箇条書きは「・」を使うこと。本文のみを返す。"
            "要約を生成できなかったセッションは【主なポイント】に含めない。\n\n" + report_md)
    if not body:
        lines = ["【参加セッション】"]
        lines += [f"・{s.meta['started_at'][11:16]} [{s.meta['id']}] {s.meta['title']}"
                  for s in sessions]
        memos = [f"・[{s.meta['id']}] {s.meta['memo']}" for s in sessions if s.meta.get("memo")]
        if memos:
            lines += ["", "【所感】", *memos]
        body = "\n".join(lines)
    return "\n".join(head) + "\n\n" + _to_slack_text(body)
