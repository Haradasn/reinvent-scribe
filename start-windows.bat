@echo off
rem Double-click to start. The first run installs what is needed (a few minutes).
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3.11 or newer is required. Install it from the page that opens, then try again.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup...
  %PY% -m venv .venv || (echo Setup failed. & pause & exit /b 1)
)
fc /b pyproject.toml ".venv\installed.toml" >nul 2>&1
if errorlevel 1 (
  echo Installing components. This takes a few minutes...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || (echo Install failed. Check your connection. & pause & exit /b 1)
  copy /y pyproject.toml ".venv\installed.toml" >nul
)
echo Starting. Keep this window open: closing it stops the app.
".venv\Scripts\python.exe" -m reinvent_scribe
if errorlevel 1 pause
