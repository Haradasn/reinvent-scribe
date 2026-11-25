import argparse
import logging
import threading
import webbrowser

import uvicorn

from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="reinvent-scribe")
    parser.add_argument("--port", type=int, default=8470)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open,
                        args=(f"http://127.0.0.1:{args.port}",)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
