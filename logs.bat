@echo off
if "%1"=="ollama" (
    docker-compose logs -f ollama
) else if "%1"=="webui" (
    docker-compose logs -f open-webui
) else if "%1"=="errors" (
    echo Error logs:
    docker-compose logs ollama open-webui 2>&1 | findstr /i "error exception fatal"
) else (
    echo Usage: logs.bat [ollama^|webui^|errors]
    echo.
    echo Recent logs:
    docker-compose logs --tail=50
)
