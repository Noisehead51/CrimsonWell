@echo off
echo.
echo Production Stack Health Check
echo.

echo Container Status:
docker-compose ps
echo.

echo Ollama API Health:
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 0 (
    echo [OK] Ollama is responding
    echo.
    echo Loaded Models:
    curl -s http://localhost:11434/api/tags
) else (
    echo [ERROR] Ollama not responding
)
echo.

echo Open WebUI Health:
curl -s http://localhost:3000 >nul 2>&1
if errorlevel 0 (
    echo [OK] Open WebUI responding at http://localhost:3000
) else (
    echo [ERROR] Open WebUI not responding
)
echo.

echo Resource Usage:
docker stats --no-stream ollama open-webui
echo.
pause
