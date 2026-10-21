@echo off
rem WastedDaily starten: Pipeline laeuft im minimierten Fenster, Dashboard oeffnet sich im Browser.
rem Beenden: das minimierte Fenster "WastedDaily" schliessen.
cd /d "%~dp0.."
if not exist ".venv\Scripts\wdvi.exe" (
  echo Erst einrichten: powershell -ExecutionPolicy Bypass -File scripts\dev-setup.ps1
  pause
  exit /b 1
)
set PYTHONIOENCODING=utf-8
start "WastedDaily" /min cmd /k ".venv\Scripts\wdvi.exe run"
timeout /t 6 /nobreak >nul
start "" http://127.0.0.1:8420
