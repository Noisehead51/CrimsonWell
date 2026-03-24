@echo off
title CrimsonWell - Local AI
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo.
echo   CrimsonWell ^| Local AI for Everyone
echo   AMD Vulkan / NVIDIA CUDA / Intel Arc / CPU
echo.

:: ─── GPU DETECTION ───────────────────────────────────────────────────────────
set GPU_VENDOR=CPU
set GPU_NAME=CPU only

for /f "tokens=2 delims==" %%a in (
    'wmic path win32_videocontroller get name /value 2^>nul ^| findstr /i "Name="'
) do (
    set "GPU_NAME=%%a"
    echo %%a | findstr /i "AMD Radeon RX Vega RDNA" >nul && set GPU_VENDOR=AMD
    echo %%a | findstr /i "NVIDIA GeForce RTX GTX Quadro" >nul && set GPU_VENDOR=NVIDIA
    echo %%a | findstr /i "Intel Arc Iris UHD" >nul && set GPU_VENDOR=Intel
    goto :gpu_done
)
:gpu_done

echo   GPU: !GPU_NAME!
echo   Backend: !GPU_VENDOR!
echo.

:: ─── GPU-SPECIFIC OLLAMA ENV ─────────────────────────────────────────────────
if "!GPU_VENDOR!"=="AMD" (
    echo   [AMD] Vulkan acceleration enabled
    set OLLAMA_GPU_OVERHEAD=0
    :: If you have an older AMD GPU and it defaults to CPU, uncomment:
    :: set HSA_OVERRIDE_GFX_VERSION=10.3.0
)
if "!GPU_VENDOR!"=="CPU" (
    echo   [!] No GPU detected — running on CPU. Try phi3:mini or llama3.2:3b for best speed.
)

:: ─── START OLLAMA ─────────────────────────────────────────────────────────────
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %errorlevel% neq 0 (
    echo   [1/2] Starting Ollama...
    set OLLAMA_HOST=0.0.0.0:11434

    set OLLAMA_EXE=
    if exist "!LOCALAPPDATA!\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=!LOCALAPPDATA!\Programs\Ollama\ollama.exe"
    if "!OLLAMA_EXE!"=="" (
        where ollama >nul 2>&1 && set OLLAMA_EXE=ollama
    )
    if "!OLLAMA_EXE!"=="" (
        echo.
        echo   [ERROR] Ollama not found. Download from: https://ollama.com/download
        echo   Then run SETUP.bat and try again.
        pause & exit /b 1
    )
    start /min "" "!OLLAMA_EXE!" serve
    timeout /t 6 /nobreak >nul
    echo   Ollama started.
) else (
    echo   [1/2] Ollama already running
)

:: ─── FIND PYTHON ─────────────────────────────────────────────────────────────
set PYTHON_EXE=
where python >nul 2>&1 && set PYTHON_EXE=python
if "!PYTHON_EXE!"=="" where python3 >nul 2>&1 && set PYTHON_EXE=python3
if "!PYTHON_EXE!"=="" (
    for %%v in (313 312 311 310) do (
        if exist "!LOCALAPPDATA!\Programs\Python\Python%%v\python.exe" (
            set "PYTHON_EXE=!LOCALAPPDATA!\Programs\Python\Python%%v\python.exe"
            goto :py_done
        )
    )
)
:py_done
if "!PYTHON_EXE!"=="" (
    echo   [ERROR] Python not found. Download from: https://python.org/downloads
    pause & exit /b 1
)

:: ─── START CRIMSONWELL ────────────────────────────────────────────────────────
echo   [2/2] Starting CrimsonWell...
set SCRIPT_DIR=%~dp0
start /min "" "!PYTHON_EXE!" "!SCRIPT_DIR!crimsonwell.py"
timeout /t 3 /nobreak >nul

:: ─── LOCAL IP FOR PHONE/REMOTE ACCESS ────────────────────────────────────────
set LOCAL_IP=
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    set "LOCAL_IP=%%a"
    set "LOCAL_IP=!LOCAL_IP: =!"
    goto :got_ip
)
:got_ip

start "" http://localhost:3000

echo.
echo   ============================================
echo    CRIMSONWELL READY
echo    Local:   http://localhost:3000
if defined LOCAL_IP echo    Network: http://!LOCAL_IP!:3000
echo   ============================================
echo.
echo   Press any key to stop CrimsonWell.
pause >nul
taskkill /f /fi "WINDOWTITLE eq CrimsonWell*" >nul 2>&1
