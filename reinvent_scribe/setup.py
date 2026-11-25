"""Everything the in-app setup screen needs. Secrets go in, never out."""
import os
import re
import subprocess
import sys
import threading
from dataclasses import fields
from pathlib import Path

from .config import Settings

ALLOWED = {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "ASSEMBLYAI_API_KEY",
           "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"}
EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"
HQ_MODEL = "large-v3-turbo"


def save_env(path: Path, values: dict[str, str]) -> None:
    """Update KEY=VALUE lines in place; other lines and comments stay."""
    path = Path(path)
    for key, value in values.items():
        if key not in ALLOWED or not re.fullmatch(r"[\x20-\x7e]+", value.strip()):
            raise ValueError("使えない文字が含まれています。貼り付けた値を確認してください。")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = {k: v.strip() for k, v in values.items()}
    out = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if not line.lstrip().startswith("#") and key in values:
            if key in left:  # a repeated key keeps only one line
                out.append(f"{key}={left.pop(key)}")
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in left.items()]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def apply(settings: Settings, values: dict[str, str]) -> None:
    """Use the new values right away, without a restart."""
    os.environ.update({k: v.strip() for k, v in values.items()})
    fresh = Settings.from_env()
    for f in fields(Settings):
        if f.name not in ("data_dir", "whisper_model"):
            setattr(settings, f.name, getattr(fresh, f.name))


# ---------- speech models ----------

_download = {"running": False, "error": None}


def has_model(name: str) -> bool:
    try:
        from faster_whisper import download_model
        download_model(name, local_files_only=True)
        return True
    except Exception:
        return False


def download_models(names: list[str]) -> None:
    if _download["running"]:
        return
    _download.update(running=True, error=None)

    def run():
        try:
            from faster_whisper import download_model
            for n in names:
                download_model(n)
        except Exception:
            _download["error"] = "ダウンロードできませんでした。通信を確認して、もう一度押してください。"
        finally:
            _download["running"] = False

    threading.Thread(target=run, daemon=True, name="model-download").start()


def models_status(realtime: str) -> dict:
    return {"realtime": has_model(realtime), "hq": has_model(HQ_MODEL), **_download}


# ---------- checks ----------

def test_llm(llm) -> str | None:
    if not llm.available():
        return "APIキーを入力してください。"
    if llm.chat([{"role": "user", "content": "OK とだけ返してください"}]):
        return None
    return {"auth": "APIキーが正しくありません。", "payment": "残高がありません。クレジットを購入してください。",
            "rate_limited": "利用上限か残高不足です。支払い設定を確認してください。",
            "network": "インターネットに接続できません。", "busy": "混雑しています。少し待ってからお試しください。",
            }.get(llm.last_error, "接続できませんでした。キーとモデル名を確認してください。")


def test_stt(key: str | None) -> str | None:
    if not key:
        return "APIキーを入力してください。"
    from .streaming import AssemblyAIStreamer
    s = AssemblyAIStreamer(key, on_segment=lambda ts, t: None)
    try:
        s.start()
        # a bad key is accepted at first and then closed by the service
        return None if not s._terminated.wait(2.0) else "APIキーか支払い設定を確認してください。"
    except Exception as e:
        return ("APIキーが正しくありません。" if "401" in str(e) or "403" in str(e)
                else "接続できませんでした。")
    finally:
        s.stop()


def test_gmail(address: str | None, password: str | None) -> str | None:
    import smtplib
    if not (address and password):
        return "メールアドレスとアプリパスワードを入力してください。"
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as m:
            m.login(address, password.replace(" ", ""))
        return None
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected):
        return "ログインできません。アドレスとアプリパスワードを確認してください。"
    except Exception:
        return "Gmail に接続できませんでした。"


def test_mic(seconds: float = 2.0) -> dict:
    import numpy as np
    import sounddevice as sd
    try:
        name = sd.query_devices(kind="input")["name"]
        data = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
        sd.wait()
    except Exception:
        return {"error": "マイクを使えません。接続と、OS のマイク許可を確認してください。"}
    rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
    if rms < 1:
        return {"name": name, "error": "音が入っていません。OS の設定でマイクを許可してください。"}
    return {"name": name, "rms": round(rms)}


def open_extension_folder() -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(EXTENSION_DIR)])
    elif sys.platform == "win32":
        os.startfile(str(EXTENSION_DIR))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(EXTENSION_DIR)])
