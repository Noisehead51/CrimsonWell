# CrimsonWell — Local AI for Everyone

> **I built this for my own setup (AMD RX 6600 XT, Windows 11).**
> It works on any GPU or CPU though — AMD, NVIDIA, Intel Arc, or pure CPU.
> **Feel free to use it, fork it, and contribute.** PRs welcome.

---

## What is it?

CrimsonWell is a noob-proof local AI stack that auto-routes your requests to the right model and agent — no manual configuration needed. Just describe what you want and it figures out the rest.

**Key features:**
- **Auto intent routing** — detects if you want code, 3D scripts, research, writing, math, or an agent task and picks the right AI persona + model automatically
- **VRAM-aware model selection** — never crashes from OOM; picks the best model that fits your GPU
- **AMD Vulkan first** — works on ALL AMD GPUs, even without ROCm (RX 400 series and up)
- **Streaming responses** — see tokens as they're generated, no waiting for full replies
- **Autonomous agent mode** — tell it to *do* something and it uses tools to actually execute it
- **Usage learner** — tracks which models you use most and pre-warms them on startup
- **Skill scanner** — drop a `.py` file in `skills/` and it auto-registers as a new capability
- **Zero cloud dependency** — 100% local, nothing leaves your machine

---

## Quick Start

### 1. Install requirements
- **Python 3.9+** → [python.org/downloads](https://www.python.org/downloads/)
  Check "Add to PATH" during install
- **Ollama** → [ollama.com/download](https://ollama.com/download)

### 2. First-time setup
```
Double-click SETUP.bat
```
This detects your GPU, recommends the right model for your VRAM, and downloads it.

### 3. Launch
```
Double-click LAUNCH.bat
```
Opens `http://localhost:3000` automatically.

---

## GPU Support

| GPU Type | Backend | Notes |
|----------|---------|-------|
| AMD RX 400–9000 series | Vulkan | Auto-detected, no ROCm needed |
| NVIDIA GTX/RTX | CUDA | Auto-detected |
| Intel Arc / Iris | Vulkan | Auto-detected |
| No GPU / CPU only | CPU | Works, use small models |

**For AMD GPUs:** Ollama uses Vulkan automatically on Windows. If your GPU isn't detected or runs on CPU, try adding `HSA_OVERRIDE_GFX_VERSION=10.3.0` to LAUNCH.bat (uncomment the line for your GPU generation).

---

## Model Guide (What to download)

| Your VRAM | Recommended | Why |
|-----------|-------------|-----|
| ≤2GB / CPU | `phi3:mini` | Tiny, surprisingly capable |
| 4GB | `llama3.2:3b` | Good balance |
| 6GB | `mistral:7b` | Fast all-rounder |
| 8GB | `llama3.1:8b` | Best quality for 8GB |
| 8GB (coding) | `qwen2.5-coder:7b` | Best coding model |
| 8GB (reasoning) | `deepseek-r1:8b` | Best for research/math |

To download a model: `ollama pull <model-name>`
Or click it in the CrimsonWell sidebar.

---

## Architecture

```
User input
    │
    ▼
Intent Router          ← keyword-based, <1ms
    │ (intent: code / 3d / research / math / write / agent / chat)
    ▼
VRAM Planner           ← checks GPU VRAM, picks best installed model
    │ (model: qwen2.5-coder:7b)
    ▼
Ollama (streaming)     ← local inference, your GPU
    │
    ▼
UI (streaming tokens)

Usage Learner          ← runs in background, pre-warms top model on next startup
Skill Scanner          ← scans skills/ on startup, auto-registers new skills
```

---

## Project Structure

```
CrimsonWell/
├── crimsonwell.py        # Main server + embedded UI (start here)
├── agent_engine.py       # Autonomous ReAct agent with tool use
├── core/
│   ├── gpu_detect.py     # GPU/VRAM detection (AMD/NVIDIA/Intel/CPU)
│   ├── vram_planner.py   # Model catalog + VRAM-aware selection
│   ├── intent_router.py  # Fast keyword-based intent routing
│   └── usage_learner.py  # Usage tracking + model pre-warming
├── skills/               # Drop .py files here to add new skills
├── LAUNCH.bat            # One-click launcher (auto GPU setup)
├── SETUP.bat             # First-time setup wizard
└── README.md
```

---

## Adding Skills

Drop a `.py` file in the `skills/` folder. CrimsonWell scans it on startup and registers it automatically. No config needed.

Example skill structure (coming soon — PRs welcome):
```python
# skills/image_gen.py
SKILL_NAME = "Image Generation"
SKILL_ICON = "🎨"
KEYWORDS = ["generate image", "draw", "stable diffusion", "image of"]

def handle(message: str) -> str:
    # Your skill logic here
    ...
```

---

## Contributing

This project is open to contributions of any size:

- **New skills** — image gen, voice, web scraping, file management
- **More GPU support** — Linux ROCm setup, macOS Metal
- **Better UI** — themes, mobile improvements, conversation history
- **More models** — add entries to `core/vram_planner.py`'s `MODEL_CATALOG`
- **Bug fixes** — always welcome

**To contribute:**
1. Fork the repo
2. Create a branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Submit a PR with a short description of what you changed and why

No contribution is too small. Even fixing a typo helps.

---

## Troubleshooting

**"Ollama not running" alert**
→ Run LAUNCH.bat, or open a terminal and run: `ollama serve`

**Slow generation / CPU fallback**
→ Check that your GPU drivers are up to date
→ For AMD: verify Ollama is using Vulkan in its logs
→ Try a smaller model (e.g. `llama3.2:3b` instead of `llama3.1:8b`)

**"No models installed"**
→ Run SETUP.bat, or: `ollama pull llama3.2:3b`

**Port 3000 already in use**
→ Change `PORT = 3000` in `crimsonwell.py` to another port (e.g. `3001`)

---

## License

MIT — do whatever you want with it.

---

*Built on Windows 11, AMD RX 6600 XT, 16GB RAM. Your mileage may vary on other setups — that's what the issues tab is for.*
