# Windows Production Setup

## Option 1: Docker Desktop (Recommended for Production)

### Prerequisites
1. **Install Docker Desktop** → https://www.docker.com/products/docker-desktop
2. **Enable WSL 2** (Windows Subsystem for Linux)
3. **Allocate GPU to Docker** (Settings → Resources → GPU)

### Start
```batch
cd C:\Users\nickn\local-ai-production
start.bat
```

**If Docker not found error:**
1. Ensure Docker Desktop is running
2. Restart terminal
3. Run `docker --version` to verify

---

## Option 2: Native Ollama + Web UI (Simpler)

Use this if Docker Desktop causes issues.

### Setup

```batch
# 1. Keep Ollama running in background
# Ollama already installed from previous step

# 2. Pull the model
ollama pull qwen3.5:9b

# 3. Start Open WebUI locally (via Python)
pip install open-webui
open-webui serve
```

This starts:
- **Ollama**: http://localhost:11434
- **Open WebUI**: http://localhost:3000

---

## Option 3: Manual Control (For Testing)

### Start Ollama Service

```batch
# Start Ollama as service
net start Ollama

# Or if not registered:
C:\Users\%USERNAME%\AppData\Local\Programs\Ollama\ollama.exe serve
```

### Pull Model

```batch
ollama pull qwen3.5:9b
```

### Start Open WebUI

```batch
# Install if not present
pip install open-webui

# Start
open-webui serve
```

### Test

```batch
# In PowerShell
curl http://localhost:11434/api/tags
curl http://localhost:3000
```

---

## Recommended for Your System

**Since Docker on Windows can be complex:**

### **Use Option 2 (Native + Web UI)**

```batch
# Terminal 1: Start Ollama
cd "C:\Users\nickn\AppData\Local\Programs\Ollama"
ollama.exe serve

# Terminal 2: Start Web UI
pip install open-webui
open-webui serve

# Terminal 3: Pull model
ollama pull qwen3.5:9b
```

Then open: **http://localhost:3000**

---

## Quick Start (Copy-Paste)

### PowerShell (as Administrator)

```powershell
# Start Ollama service
Start-Process "C:\Users\nickn\AppData\Local\Programs\Ollama\ollama.exe" -ArgumentList "serve" -WindowStyle Normal

# Wait 5 seconds
Start-Sleep -Seconds 5

# Pull model
& "C:\Users\nickn\AppData\Local\Programs\Ollama\ollama.exe" pull qwen3.5:9b

# Install & start Open WebUI
pip install open-webui
open-webui serve
```

Then navigate to: http://localhost:3000

---

## Performance on Windows

| Component | Notes |
|-----------|-------|
| **Ollama** | Native binary, full GPU support |
| **WSL 2** | GPU pass-through for Docker |
| **Open WebUI** | Works as web app, no WSL needed |

**Best setup**: Native Ollama + Native Open WebUI = No Docker complexity

---

## Troubleshooting

### "Ollama is not recognized"
```powershell
# Add to PATH manually
$env:Path += ";C:\Users\$env:USERNAME\AppData\Local\Programs\Ollama"
ollama --version
```

### "Port 11434 already in use"
```powershell
# Find what's using it
netstat -ano | findstr ":11434"
taskkill /PID <PID> /F

# Or change Ollama port
$env:OLLAMA_HOST = "0.0.0.0:11435"
```

### "GPU not detected"
```powershell
# Check NVIDIA
& "C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"

# Check AMD
# Use Radeon Software to verify ROCm installation
```

### "Open WebUI won't start"
```powershell
# Check if port 3000 is in use
netstat -ano | findstr ":3000"

# Change port
open-webui serve --port 8000
# Then access: http://localhost:8000
```

---

## One-Line Setup (Windows PowerShell)

```powershell
# Download and execute native setup
$setup = @"
Add-Content -Path $PROFILE -Value '`$env:Path += ""C:\Users\$env:USERNAME\AppData\Local\Programs\Ollama""' -Force
& "C:\Users\$env:USERNAME\AppData\Local\Programs\Ollama\ollama.exe" pull qwen3.5:9b
pip install open-webui
open-webui serve
"@

Invoke-Expression $setup
```

---

## Next Steps

1. **Choose setup option** (recommend Option 2 for Windows)
2. **Run setup** (copy-paste commands above)
3. **Open** http://localhost:3000
4. **Create agent** (see example-agents.md)
5. **Test** with any prompt

---

**Support**: All the same commands work on Windows as Linux
- `gpu-check.sh` → Works in PowerShell or WSL
- Models sync across Ollama instances
- Open WebUI fully portable

---

**Status**: Windows Ready ✓
