@echo off
title Local AI Stack

:: ─────────────────────────────────────────
::  LOCAL AI STACK - ONE CLICK LAUNCH
::  RX 6600 XT (Vulkan) - 50+ tok/s
:: ─────────────────────────────────────────

echo.
echo  Starting Local AI Stack...
echo.

:: Start Ollama with Vulkan GPU if not already running
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if errorlevel 1 (
    echo  [1/3] Starting Ollama GPU...
    set OLLAMA_VULKAN=1
    set OLLAMA_HOST=0.0.0.0:11434
    start /min "" "%USERPROFILE%\AppData\Local\Programs\Ollama\ollama.exe" serve
    timeout /t 8 /nobreak >nul
) else (
    echo  [1/3] Ollama already running
)

:: Start Web UI
echo  [2/3] Starting Web UI...
start /min "" "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe" "C:\Users\nickn\local-ai-production\studio.py"
timeout /t 3 /nobreak >nul

:: Get local IP for phone access
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    set LOCAL_IP=%%a
    goto :gotip
)
:gotip
set LOCAL_IP=%LOCAL_IP: =%

:: Open browser
echo  [3/3] Opening browser...
start "" http://localhost:3000

echo.
echo  ========================================
echo   READY at http://localhost:3000
echo   GPU: RX 6600 XT Vulkan (50+ tok/s)
echo.
echo   PHONE / REMOTE ACCESS:
echo   http://%LOCAL_IP%:3000
echo  ========================================
echo.
echo  Close this window to stop the Web UI.
echo  Ollama keeps running in the background.
echo.
pause
