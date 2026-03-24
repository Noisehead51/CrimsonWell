# Production Local AI Stack

## Quick Start

```bash
# Windows
start.bat

# Linux/Mac
chmod +x start.sh && ./start.sh
```

Then open: **http://localhost:3000**

---

## What You Get

| Component | Version | Purpose |
|-----------|---------|---------|
| **Ollama** | 0.18.2+ | Local LLM runtime with GPU acceleration |
| **Open WebUI** | v0.6.35+ | Production-grade web UI with agents |
| **Qwen3.5:9B** | Latest | Best agent model (6GB, ~6B active params) |
| **Mistral** | Latest | Fast fallback model (3.8GB) |

---

## Architecture

```
┌─────────────────────────────────────┐
│      Open WebUI (3000)              │
│  - Chat interface                   │
│  - Agent orchestration              │
│  - Pipelines (custom tools)         │
│  - RAG support                      │
└──────────────┬──────────────────────┘
               │
┌──────────────┴──────────────────────┐
│      Ollama (11434)                 │
│  - Model inference                  │
│  - GPU acceleration (AMD/NVIDIA)    │
│  - Model management                 │
└─────────────────────────────────────┘
```

---

## Performance Numbers (2026 Latest)

| Metric | Value |
|--------|-------|
| **Qwen3.5:9B** | ~30-40 tokens/sec (RTX 4090) |
| **8GB VRAM card** | 40+ tokens/sec with Q4_K_M |
| **Context window** | 32K (Qwen3.5:9B) |
| **Agent response** | 2-5 sec (typical) |

Sources:
- [Ollama GPU Optimization 2026](https://dasroot.net/posts/2026/03/ollama-gpu-optimization-configuration-2026/)
- [Best LLMs for 8GB VRAM](https://medium.com/@rosgluk/best-llms-for-ollama-on-16gb-vram-gpu-c1bf6c3a10be)
- [Qwen 3.5 Benchmarks](https://localaimaster.com/blog/small-language-models-guide-2026)

---

## Security Features

✅ **Enabled by Default:**
- Secret key generation (auto)
- API key authentication
- No signup allowed (admin only)
- Encrypted database support
- Multi-worker deployment ready

⚠️ **Before Production:**
1. Change `WEBUI_SECRET_KEY` in `.env.prod`
2. Set strong admin password first login
3. Configure firewall rules
4. Use reverse proxy (nginx/Caddy) for SSL
5. Never expose to internet without auth

### Critical Security Fix
⚠️ **IMPORTANT**: Open WebUI v0.6.34 and earlier has a critical RCE vulnerability. Your setup uses **v0.6.35+** which fixes this. Keep updated!

Sources:
- [Open WebUI Security Docs](https://docs.openwebui.com/security/)
- [CVE Alert - v0.6.34 RCE](https://www.csoonline.com/article/4113139/open-webui-bug-turns-free-model-into-an-enterprise-backdoor.html)

---

## Usage

### Chat
1. Go to http://localhost:3000
2. Select **qwen3.5:9b** from model dropdown
3. Type and chat

### Run an Agent
1. In Open WebUI, click "+" → "Agent"
2. Name it (e.g., "Research Agent")
3. Assign tools (Web search, calculator, etc.)
4. Start chatting

### Custom Tools (Pipelines)
Place Python files in `pipelines/` - Open WebUI loads them automatically.

Example pipeline:
```python
# pipelines/calculator.py
class Calculator:
    def __init__(self):
        self.name = "calculator"
        self.description = "Evaluate math expressions"

    async def __call__(self, expression: str) -> str:
        return str(eval(expression))
```

---

## Commands

| Command | Purpose |
|---------|---------|
| `./start.sh` | Start everything |
| `./stop.sh` | Stop everything |
| `./gpu-check.sh` | Health check |
| `./logs.sh ollama` | View Ollama logs |
| `./logs.sh webui` | View Open WebUI logs |
| `./logs.sh errors` | Show errors only |

---

## Model Management

### Pull a New Model
```bash
docker exec ollama ollama pull llama2:13b
```

### View Models
```bash
docker exec ollama ollama list
```

### Memory Requirements
- **Qwen3.5:4B**: 2.5GB
- **Qwen3.5:7B**: 4.5GB
- **Qwen3.5:9B**: 6GB ← **Recommended**
- **Llama2:13B**: 8GB

---

## Troubleshooting

### "Connection refused" on startup
```bash
# Wait a bit longer, services are starting
sleep 30 && ./gpu-check.sh
```

### High memory usage
```bash
# See which container is using memory
docker stats

# Unload unused models
docker exec ollama ollama rm modelname
```

### GPU not being used
Check logs:
```bash
./logs.sh ollama | grep -i "gpu\|cuda\|rocm"
```

For AMD GPU, may need:
```bash
# In docker-compose.yml, set:
HSA_OVERRIDE_GFX_VERSION: gfx90a  # Adjust for your GPU
```

---

## Production Deployment

For production with multiple users/GPUs:

1. **Use PostgreSQL** instead of SQLite:
   ```yaml
   # docker-compose.yml
   postgres:
     image: postgres:16-alpine
     environment:
       POSTGRES_DB: webui
   ```

2. **Enable reverse proxy** (nginx/Caddy with SSL)

3. **Configure LDAP/OIDC** for SSO

4. **Set resource limits**:
   ```yaml
   services:
     ollama:
       deploy:
         resources:
           limits:
             cpus: '4'
             memory: 16G
   ```

5. **Monitor with Prometheus/Grafana**

---

## Latest Updates (March 2026)

- ✅ Ollama v0.18.2+ with improved model scheduling
- ✅ Open WebUI v0.6.35+ (critical security fix)
- ✅ Qwen3.5 series (small/medium/large)
- ✅ AMD ROCm v7 support
- ✅ Reduced vLLM vs Ollama performance gap to ~15%

Sources:
- [Ollama v0.18 Release Notes](https://ollama.com/blog)
- [Open WebUI Releases](https://github.com/open-webui/open-webui/releases)
- [Qwen3.5 Performance Analysis](https://apidog.com/blog/best-qwen-models/)

---

## Support

- 🐛 **Issues**: https://github.com/open-webui/open-webui/issues
- 📚 **Docs**: https://docs.openwebui.com
- 💬 **Community**: https://openwebui.com/o/agents

---

**Status**: Production Ready ✓
**Last Updated**: March 2026
**Security**: v0.6.35+ (patched)
