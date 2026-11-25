"""Send the daily report through the user's own Gmail (app password)."""
import html
import re
import smtplib
from email.message import EmailMessage


def markdown_to_html(md: str) -> str:
    """Just the Markdown the daily report uses: headings, tables, lists, bold."""
    def inline(t):
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(t))

    out, mode = [], None

    def switch(new):
        nonlocal mode
        if mode != new:
            if mode:
                out.append(f"</{mode}>")
            if new == "table":
                out.append('<table border="1" cellpadding="6" style="border-collapse:collapse">')
            elif new:
                out.append(f"<{new}>")
            mode = new

    for line in md.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split(" | ")]
            if set(s) <= set("|-: "):
                continue
            tag = "td" if mode == "table" else "th"
            switch("table")
            out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
        elif re.match(r"[-*・]\s*\S", s) and not s.startswith("**"):
            switch("ul")
            text = re.sub(r"^[-*・]\s*", "", s)
            out.append(f"<li>{inline(text)}</li>")
        elif m := re.match(r"(#{1,4})\s+(.*)", s):
            switch(None)
            n = len(m[1]) + 1
            out.append(f"<h{n}>{inline(m[2])}</h{n}>")
        else:
            switch(None)
            if s:
                out.append(f"<p>{inline(s)}</p>")
    switch(None)
    return "\n".join(out)


def build_message(sender: str, to: str, subject: str, body_md: str,
                  attachments: list[tuple[str, str]]) -> EmailMessage:
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if not recipients or not all(re.fullmatch(r"[^@\s<>,]+@[^@\s<>,]+\.[^@\s<>,]+", a)
                                 for a in recipients):
        raise ValueError("宛先のメールアドレスを確認してください。")
    msg = EmailMessage()
    msg["From"], msg["To"] = sender, ", ".join(recipients)
    msg["Subject"] = " ".join(subject.split()) or "出張日報"
    msg.set_content(body_md)
    msg.add_alternative(markdown_to_html(body_md), subtype="html")
    for name, text in attachments:
        msg.add_attachment(text.encode("utf-8-sig"), maintype="text",
                           subtype="csv", filename=name)
    return msg


def send_gmail(address: str, app_password: str, msg: EmailMessage) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(address, app_password.replace(" ", ""))
        smtp.send_message(msg)
