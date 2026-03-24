@echo off
title CrimsonWell Setup
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo.
echo   ============================================
echo    CrimsonWell — First-Time Setup Wizard
echo   ============================================
echo.

:: ─── CHECK PYTHON ─────────────────────────────────────────────────────────────
echo   [1/4] Checking Python...
set PYTHON_EXE=
where python >nul 2>&1 && set PYTHON_EXE=python
if "!PYTHON_EXE!"=="" where python3 >nul 2>&1 && set PYTHON_EXE=python3
if "!PYTHON_EXE!"=="" (
    for %%v in (313 312 311 310) do (
        if exist "!LOCALAPPDATA!\Programs\Python\Python%%v\python.exe" (
            set "PYTHON_EXE=!LOCALAPPDATA!\Programs\Python\Python%%v\python.exe"
            goto :py_found
        )
    )
    echo   [ERROR] Python 3.9+ not found.
    echo   Download from: https://python.org/downloads
    echo   Make sure to check "Add to PATH" during install!
    pause & exit /b 1
)
:py_found
for /f "tokens=*" %%v in ('"!PYTHON_EXE!" --version 2^>^&1') do echo   Python: %%v
echo   [OK] Python found: !PYTHON_EXE!
echo.

:: ─── CHECK OLLAMA ─────────────────────────────────────────────────────────────
echo   [2/4] Checking Ollama...
set OLLAMA_EXE=
if exist "!LOCALAPPDATA!\Programs\Ollama\ollama.exe" (
    set "OLLAMA_EXE=!LOCALAPPDATA!\Programs\Ollama\ollama.exe"
) else (
    where ollama >nul 2>&1 && set OLLAMA_EXE=ollama
)

if "!OLLAMA_EXE!"=="" (
    echo.
    echo   [!] Ollama not found.
    echo   Download from: https://ollama.com/download
    echo   Install it, then re-run this setup script.
    echo.
    set /p OPEN_OLLAMA="   Open download page now? (y/n): "
    if /i "!OPEN_OLLAMA!"=="y" start "" "https://ollama.com/download"
    pause & exit /b 1
)
echo   [OK] Ollama found: !OLLAMA_EXE!
echo.

:: ─── DETECT GPU AND VRAM ──────────────────────────────────────────────────────
echo   [3/4] Detecting your GPU...
set GPU_NAME=Unknown
set GPU_VENDOR=CPU
set VRAM_GB=0

for /f "tokens=2 delims==" %%a in (
    'wmic path win32_videocontroller get name /value 2^>nul ^| findstr /i "Name="'
) do (
    set "GPU_NAME=%%a"
    echo %%a | findstr /i "AMD Radeon RX Vega" >nul && set GPU_VENDOR=AMD
    echo %%a | findstr /i "NVIDIA GeForce RTX GTX" >nul && set GPU_VENDOR=NVIDIA
    echo %%a | findstr /i "Intel Arc Iris" >nul && set GPU_VENDOR=Intel
    goto :gfx_done
)
:gfx_done

:: Try to get VRAM
for /f "tokens=2 delims==" %%a in (
    'wmic path win32_videocontroller get AdapterRAM /value 2^>nul ^| findstr /i "AdapterRAM="'
) do (
    set /a VRAM_BYTES=%%a 2>nul
    set /a VRAM_GB=!VRAM_BYTES! / 1073741824 2>nul
    goto :vram_done
)
:vram_done

echo   GPU:    !GPU_NAME!
echo   Vendor: !GPU_VENDOR!
echo   VRAM:   !VRAM_GB! GB
echo.

:: ─── RECOMMEND A MODEL ────────────────────────────────────────────────────────
echo   [4/4] Recommending a model for your setup...
echo.
set RECOMMENDED_MODEL=llama3.2:3b
set RECOMMENDED_DESC=Good balance for most GPUs

if !VRAM_GB! GEQ 8 (
    set RECOMMENDED_MODEL=llama3.1:8b
    set RECOMMENDED_DESC=Great quality, fits 8GB VRAM
)
if !VRAM_GB! GEQ 6 (
    if !VRAM_GB! LSS 8 (
        set RECOMMENDED_MODEL=mistral:7b
        set RECOMMENDED_DESC=Fast and capable for 6GB VRAM
    )
)
if !VRAM_GB! GEQ 4 (
    if !VRAM_GB! LSS 6 (
        set RECOMMENDED_MODEL=llama3.2:3b
        set RECOMMENDED_DESC=Efficient for 4GB VRAM
    )
)
if !VRAM_GB! LSS 4 (
    set RECOMMENDED_MODEL=phi3:mini
    set RECOMMENDED_DESC=Tiny but smart, for low VRAM or CPU
)
if "!GPU_VENDOR!"=="CPU" (
    set RECOMMENDED_MODEL=llama3.2:3b
    set RECOMMENDED_DESC=Best for CPU-only setups
)

echo   Recommended for your setup:
echo   Model: !RECOMMENDED_MODEL!
echo   Why:   !RECOMMENDED_DESC!
echo.

echo   Other options for your VRAM (!VRAM_GB!GB):
if !VRAM_GB! GEQ 2  echo     phi3:mini         (1.8GB) — Tiny, very fast
if !VRAM_GB! GEQ 2  echo     llama3.2:3b       (2.2GB) — Good balance
if !VRAM_GB! GEQ 4  echo     mistral:7b         (4.1GB) — Fast all-rounder
if !VRAM_GB! GEQ 4  echo     qwen2.5:7b         (4.4GB) — Great for writing
if !VRAM_GB! GEQ 4  echo     qwen2.5-coder:7b   (4.4GB) — Best coding model
if !VRAM_GB! GEQ 5  echo     llama3.1:8b        (4.7GB) — Meta's best 8B
if !VRAM_GB! GEQ 6  echo     deepseek-r1:8b     (4.9GB) — Reasoning/research
if !VRAM_GB! GEQ 6  echo     qwen2.5:9b         (5.8GB) — High quality
echo.

:: Start Ollama for the pull
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %errorlevel% neq 0 (
    echo   Starting Ollama to download model...
    start /min "" "!OLLAMA_EXE!" serve
    timeout /t 5 /nobreak >nul
)

set /p PULL_MODEL="   Download !RECOMMENDED_MODEL! now? (y/n/other model name): "
if /i "!PULL_MODEL!"=="n" goto :skip_pull
if /i "!PULL_MODEL!"=="y" (
    set PULL_MODEL=!RECOMMENDED_MODEL!
)
echo.
echo   Downloading !PULL_MODEL! — this may take a few minutes...
echo   (Larger models take longer on slower internet)
echo.
ollama pull !PULL_MODEL!
echo.
:skip_pull

echo   ============================================
echo    Setup complete!
echo    Run LAUNCH.bat to start CrimsonWell.
echo   ============================================
echo.
pause
