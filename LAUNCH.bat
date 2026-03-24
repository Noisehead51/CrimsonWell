@echo off
title CrimsonWell - Local AI
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo.
echo   CrimsonWell ^| Local AI for Everyone
echo   AMD Vulkan / NVIDIA CUDA / Intel Arc / CPU
echo.

:: ─── GPU DETECTION ────────────────────────────────────────────────────────────
set "GPU_VENDOR=CPU"
set "GPU_NAME=CPU only"

for /f "tokens=2 delims==" %%a in ('wmic path win32_videocontroller get name /value 2^>nul ^| findstr /i "Name="') do (
    set "GPU_NAME=%%a"
    echo %%a | findstr /i "AMD Radeon RX Vega RDNA" >nul 2>&1
    if !errorlevel!==0 set "GPU_VENDOR=AMD"
    echo %%a | findstr /i "NVIDIA GeForce RTX GTX Quadro" >nul 2>&1
    if !errorlevel!==0 set "GPU_VENDOR=NVIDIA"
    echo %%a | findstr /i "Intel Arc Iris UHD" >nul 2>&1
    if !errorlevel!==0 set "GPU_VENDOR=Intel"
    goto :gpu_done
)
:gpu_done

echo   GPU   : !GPU_NAME!
echo   Vendor: !GPU_VENDOR!
echo.

:: ─── GPU ENV VARS ─────────────────────────────────────────────────────────────
if "!GPU_VENDOR!"=="AMD" (
    echo   [AMD] Enabling Vulkan acceleration...
    set "OLLAMA_VULKAN=1"
    set "OLLAMA_GPU_OVERHEAD=0"
)
if "!GPU_VENDOR!"=="NVIDIA" (
    echo   [NVIDIA] CUDA acceleration active
)
if "!GPU_VENDOR!"=="Intel" (
    echo   [Intel] Vulkan acceleration active
    set "OLLAMA_VULKAN=1"
)
if "!GPU_VENDOR!"=="CPU" (
    echo   [i] No discrete GPU - running on CPU
    echo   Tip: phi3:mini or llama3.2:3b work well on CPU
)

:: ─── FIND OLLAMA ──────────────────────────────────────────────────────────────
set "OLLAMA_EXE="
if exist "!LOCALAPPDATA!\Programs\Ollama\ollama.exe" (
    set "OLLAMA_EXE=!LOCALAPPDATA!\Programs\Ollama\ollama.exe"
    goto :ollama_found
)
where ollama >nul 2>&1
if !errorlevel!==0 (
    set "OLLAMA_EXE=ollama"
    goto :ollama_found
)
echo.
echo   [ERROR] Ollama not found!
echo   Download: https://ollama.com/download
echo.
pause
exit /b 1
:ollama_found

:: ─── START OLLAMA (restart to pick up GPU env vars) ──────────────────────────
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul 2>&1
if !errorlevel!==0 (
    echo   [1/2] Restarting Ollama with GPU settings...
    taskkill /IM ollama.exe /F >nul 2>&1
    timeout /t 2 /nobreak >nul
) else (
    echo   [1/2] Starting Ollama...
)
set "OLLAMA_HOST=0.0.0.0:11434"
start "Ollama" /min "!OLLAMA_EXE!" serve
timeout /t 6 /nobreak >nul
echo   Ollama started.
echo.

:: ─── FIND PYTHON ──────────────────────────────────────────────────────────────
set "PYTHON_EXE="
where python >nul 2>&1
if !errorlevel!==0 (
    set "PYTHON_EXE=python"
    goto :py_found
)
where python3 >nul 2>&1
if !errorlevel!==0 (
    set "PYTHON_EXE=python3"
    goto :py_found
)
for %%v in (313 312 311 310 39) do (
    if exist "!LOCALAPPDATA!\Programs\Python\Python%%v\python.exe" (
        set "PYTHON_EXE=!LOCALAPPDATA!\Programs\Python\Python%%v\python.exe"
        goto :py_found
    )
)
echo.
echo   [ERROR] Python not found!
echo   Download: https://python.org/downloads
echo.
pause
exit /b 1
:py_found
echo   Python: !PYTHON_EXE!

:: ─── START CRIMSONWELL ────────────────────────────────────────────────────────
echo   [2/2] Starting CrimsonWell...
set "SCRIPT_DIR=%~dp0"
start "CrimsonWell" "!PYTHON_EXE!" "!SCRIPT_DIR!crimsonwell.py"
timeout /t 4 /nobreak >nul

:: ─── LOCAL IP ─────────────────────────────────────────────────────────────────
set "LOCAL_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    set "LOCAL_IP=%%a"
    set "LOCAL_IP=!LOCAL_IP: =!"
    goto :got_ip
)
:got_ip

start "" "http://localhost:3000"

echo.
echo   ============================================
echo    CRIMSONWELL READY
echo    Local:   http://localhost:3000
if defined LOCAL_IP echo    Network: http://!LOCAL_IP!:3000
echo   ============================================
echo.
echo   Keep this window open.
echo   Press any key to STOP CrimsonWell + Ollama.
pause >nul

taskkill /FI "WINDOWTITLE eq CrimsonWell" /F >nul 2>&1
taskkill /IM ollama.exe /F >nul 2>&1
echo   Stopped.
timeout /t 1 /nobreak >nul
