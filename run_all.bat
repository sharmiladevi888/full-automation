@echo off
REM Continuity Studio + 9Router launcher.
REM Starts the local 9Router (token-saver proxy for all Claude calls) in tray
REM mode if it isn't already running, then boots the studio.

cd /d "%~dp0"

REM -- 9Router (dashboard: http://localhost:20128) --
netstat -ano | findstr ":20128" | findstr "LISTENING" >NUL
if errorlevel 1 (
  echo Starting 9Router in system tray...
  start "" /min cmd /c "9router --tray --no-browser --skip-update"
  timeout /t 6 /nobreak >NUL
) else (
  echo 9Router already running.
)

REM -- Continuity Studio --
python -c "import fastapi" 2>NUL
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install -r requirements.txt
)

REM -- June 6 build (version switcher target, header dropdown) --
if exist "E:\full-automation\app.py" (
  netstat -ano | findstr ":8010" | findstr "LISTENING" >NUL
  if errorlevel 1 (
    echo Starting June 6 build on :8010...
    start "Continuity Studio - June 6" /min cmd /c "cd /d E:\full-automation && python -m uvicorn app:app --port 8010"
  )
)

echo.
echo Continuity Studio (current) -^> http://localhost:8000
echo Continuity Studio (June 6)  -^> http://localhost:8010
echo 9Router                     -^> http://localhost:20128
echo (Ctrl+C to stop)
echo.
REM ui_patch installs the runtime auth/data hardening layer before serving.
python -m uvicorn ui_patch:app --port 8000
