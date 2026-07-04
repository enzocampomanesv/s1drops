@echo off
setlocal

REM ===================== CONFIG — edit these two lines =====================
set "PROJECT=C:\Users\enzoc\Desktop\PhD\Evictions\eviction_detection\s1drops"
set "VENV_ACTIVATE=C:\Users\enzoc\Desktop\PhD\Evictions\eviction_detection\s1env\Scripts\activate.bat"
set "PORT=8765"
REM ========================================================================

REM --- Fail CLOSED: never open a public tunnel without the admin gate set ---
REM S1DROPS_ADMIN_PASSWORD is expected from your persistent User environment.
if "%S1DROPS_ADMIN_PASSWORD%"=="" (
  echo S1DROPS_ADMIN_PASSWORD is empty - refusing to open a public tunnel.
  echo Set it with PowerShell, then open a NEW window:
  echo   [Environment]::SetEnvironmentVariable("S1DROPS_ADMIN_PASSWORD","your-demo-password","User")
  pause & exit /b 1
)

cd /d "%PROJECT%" || (echo Could not cd to %PROJECT% & pause & exit /b 1)
call "%VENV_ACTIVATE%" || (echo Could not activate the venv at %VENV_ACTIVATE% & pause & exit /b 1)

REM Launch the Solara app in its own window, bound to localhost only.
REM It inherits S1DROPS_ADMIN_PASSWORD from this process's environment.
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
