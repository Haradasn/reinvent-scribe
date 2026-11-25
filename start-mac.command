#!/bin/bash
# Double-click to start. The first run installs what is needed (a few minutes).
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 以上が必要です。開いたページからインストールして、もう一度ダブルクリックしてください。"
  open "https://www.python.org/downloads/"
  read -r -p "Enter キーで閉じます"; exit 1
fi
[ -x .venv/bin/python ] || { echo "初回の準備をしています…"; "$PY" -m venv .venv || { read -r -p "準備に失敗しました。Enter で閉じます"; exit 1; }; }
if ! cmp -s pyproject.toml .venv/installed.toml; then
  echo "必要な部品を入れています(数分かかります)…"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e . || { read -r -p "インストールに失敗しました。通信を確認してください。Enter で閉じます"; exit 1; }
  cp pyproject.toml .venv/installed.toml
fi
echo "起動しています。このウィンドウは閉じないでください(閉じるとアプリが終了します)。"
.venv/bin/python -m reinvent_scribe || read -r -p "エラーで終了しました。Enter で閉じます"
