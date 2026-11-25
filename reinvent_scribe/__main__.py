import argparse
import logging
import socket
import threading
import webbrowser

import uvicorn

from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="reinvent-scribe")
    parser.add_argument("--port", type=int, default=8470)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    with socket.socket() as s:  # a second double-click just shows the running app
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", args.port)) == 0:
            print("すでに起動しています:", url)
            if not args.no_browser:
                webbrowser.open(url)
            return

    logging.basicConfig(level=logging.INFO)
    from .config import Settings
    from .setup import has_model
    from .transcribe import pick_model
    settings = Settings.from_env()
    if not has_model(settings.whisper_model):
        print("音声認識モデルをダウンロードしています(初回のみ・数百MB)…")
    model = pick_model(settings.whisper_model)
    app = create_app(settings=settings, model_name=model)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open,
                        args=(f"http://127.0.0.1:{args.port}",)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port,
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
