@echo off
setlocal

REM ========================== CONFIG (port only) ==========================
REM Paths are derived from this script's own location (%~dp0), which is the
REM repo root - so moving or copying the project can't leave them pointing at
REM a stale checkout. Only override them if your venv lives elsewhere.
set "PROJECT=%~dp0"
set "VENV_ACTIVATE=%~dp0s1env\Scripts\activate.bat"
set "PORT=8765"
REM ========================================================================

REM %~dp0 carries a trailing backslash; strip it so quoted paths behave.
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"

REM --- Fail CLOSED: never open a public tunnel without the admin gate set ---
REM S1DROPS_ADMIN_PASSWORD is expected from your persistent User environment.
if "%S1DROPS_ADMIN_PASSWORD%"=="" (
  echo S1DROPS_ADMIN_PASSWORD is empty - refusing to open a public tunnel.
  echo Set it with PowerShell, then open a NEW window:
  echo   [Environment]::SetEnvironmentVariable("S1DROPS_ADMIN_PASSWORD","your-demo-password","User")
  pause & exit /b 1
)

REM cd to the REPO ROOT (not the package dir): `python -m` puts the working
REM directory first on sys.path, so the checkout you launched from is the one
REM that gets imported even if the venv's editable install points elsewhere.
cd /d "%PROJECT%" || (echo Could not cd to %PROJECT% & pause & exit /b 1)
if not exist "%PROJECT%\s1drops\app\main.py" (
  echo "%PROJECT%" does not look like the s1drops repo - s1drops\app\main.py is missing.
  pause & exit /b 1
)
call "%VENV_ACTIVATE%" || (echo Could not activate the venv at %VENV_ACTIVATE% & pause & exit /b 1)

REM Launch the Solara app in its own window, bound to localhost only.
REM It inherits S1DROPS_ADMIN_PASSWORD from this process's environment.
REM Report which copy of the package actually got imported, so a stale install
REM is visible in the app window instead of silently serving old code.
python -c "import s1drops, os; print('serving s1drops from', os.path.dirname(s1drops.__file__))"
start "s1drops app" cmd /k python -m solara run s1drops.app.main --host 127.0.0.1 --port %PORT%

REM Wait until the port actually accepts connections (max ~60s), so the
REM tunnel never points at a dead socket and 502s on the first hit.
echo Waiting for s1drops on port %PORT% ...
set /a TRIES=0
:waitloop
set /a TRIES+=1
if %TRIES% gtr 60 (echo App did not come up within 60s - check the app window for a traceback. & pause & exit /b 1)
timeout /t 1 /nobreak >nul
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',%PORT%);$c.Close();exit 0}catch{exit 1}"
if errorlevel 1 goto waitloop

echo.
echo App is up. Opening the Cloudflare tunnel below.
echo NOTE: the admin panel is reachable on this PUBLIC URL, gated only by the password.
echo Share the trycloudflare.com URL with your team. Ctrl+C in THIS window closes the tunnel.
echo.
cloudflared tunnel --url http://localhost:%PORT%

endlocal
