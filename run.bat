@echo off
REM Continuity Studio launcher.
REM IMPORTANT: use "python -m uvicorn" (NOT bare "uvicorn") — a stray uvicorn.exe
REM on PATH may belong to a different venv that lacks Pillow and will crash with
REM "ModuleNotFoundError: No module named 'PIL'".

cd /d "%~dp0"

REM Install deps on first run if FastAPI is missing.
python -c "import fastapi" 2>NUL
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install -r requirements.txt
)

echo.
echo Continuity Studio -^> http://localhost:8000
echo (Ctrl+C to stop)
echo.
REM ui_patch installs the runtime auth/data hardening layer before serving.
python -m uvicorn ui_patch:app --port 8000
