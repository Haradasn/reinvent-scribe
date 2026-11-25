import logging
import time

from .store import Session

log = logging.getLogger(__name__)


class RollingSummarizer:
    """Accumulates transcript text and produces a cumulative Japanese summary."""

    def __init__(self, llm, interval_sec: int):
        self.llm = llm
        self.interval_sec = interval_sec
        self.current: str = ""
        self._pending: list[str] = []
        self._last_run = time.monotonic()

    def add(self, text: str) -> None:
        self._pending.append(text)

    def due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return bool(self._pending) and (now - self._last_run) >= self.interval_sec

    def summarize(self) -> str | None:
        self._last_run = time.monotonic()
        if not self._pending or not self.llm.available():
            return None
        new_text = "\n".join(self._pending)
        prompt = (
            "あなたは技術カンファレンスのセッションを日本語で要約するアシスタントです。"
            "これまでの要約と新しい英語文字起こしを統合し、ここまでの内容がわかる"
            "累積の日本語要約(箇条書き、重要な発表・数字を優先)を返してください。\n\n"
            f"## これまでの要約\n{self.current or '(まだなし)'}\n\n"
            f"## 新しい文字起こし\n{new_text}"
        )
        out = self.llm.chat([{"role": "user", "content": prompt}])
        if out is None:
            return None  # quiet skip; pending kept for next attempt
        self._pending = []
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
                "ブログ記事のように、何が発表され何が重要かが読んでわかる文体で。\n\n"
                + s.transcript_text()[:12000]
            )
            summary = llm.chat([{"role": "user", "content": prompt}])
        parts.append(summary or "(要約なし: オフラインのためメモのみ)")

    memos = [f"- [{s.meta['id']}] {s.meta['memo']}" for s in sessions if s.meta.get("memo")]
    if memos:
        parts += ["", "## 所感・特筆事項", *memos]
    return "\n".join(parts)
