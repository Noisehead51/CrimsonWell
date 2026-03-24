#!/usr/bin/env python3
"""
INDUSTRIAL AI STUDIO
Auto-routing orchestrator — just describe what you need.
"""
import http.server
import json
import urllib.request
import urllib.error
import threading
import re
import os
import subprocess
import sys
from datetime import datetime
import agent_engine

PORT = 3000
OLLAMA = "http://localhost:11434"
LOGS_DIR = r"C:\Users\nickn\ai-workspace\logs"
TRAINING_FILE = r"C:\Users\nickn\ai-workspace\training_data.jsonl"
os.makedirs(LOGS_DIR, exist_ok=True)

def log_conversation(user_msg: str, ai_msg: str, agent_id: str, model: str) -> str:
    """Save a conversation turn, return its unique ID."""
    entry_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + str(abs(hash(user_msg)))[-6:]
    entry = {
        "id": entry_id,
        "ts": datetime.now().isoformat(),
        "agent": agent_id,
        "model": model,
        "user": user_msg,
        "assistant": ai_msg,
        "rating": None
    }
    log_file = os.path.join(LOGS_DIR, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry_id

def rate_conversation(entry_id: str, rating: int):
    """Set rating (1=good, -1=bad) on a logged entry and optionally add to training data."""
    for fname in sorted(os.listdir(LOGS_DIR), reverse=True)[:7]:
        fpath = os.path.join(LOGS_DIR, fname)
        lines = []
        found = False
        try:
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("id") == entry_id:
                            entry["rating"] = rating
                            found = True
                            if rating == 1:
                                # Add to training data
                                training = {
                                    "conversations": [
                                        {"role": "user", "content": entry["user"]},
                                        {"role": "assistant", "content": entry["assistant"]}
                                    ]
                                }
                                with open(TRAINING_FILE, "a", encoding="utf-8") as tf:
                                    tf.write(json.dumps(training) + "\n")
                        lines.append(json.dumps(entry))
                    except Exception:
                        lines.append(line.rstrip())
            if found:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                return True
        except Exception:
            pass
    return False

def maybe_auto_improve_model():
    """If 20+ good examples exist and no recent model build, trigger auto Modelfile update."""
    stats = get_training_stats()
    if stats["good"] < 20:
        return
    flag = os.path.join(LOGS_DIR, ".last_model_build")
    try:
        if os.path.exists(flag):
            last = os.path.getmtime(flag)
            if time.time() - last < 86400:  # wait 24h between builds
                return
    except Exception:
        pass
    # Build improved Modelfile from top good examples
    good_examples = []
    try:
        for fname in sorted(os.listdir(LOGS_DIR), reverse=True):
            if fname.endswith(".jsonl"):
                with open(os.path.join(LOGS_DIR, fname), encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                            if e.get("rating") == 1:
                                good_examples.append(e)
                        except Exception:
                            pass
            if len(good_examples) >= 20:
                break
    except Exception:
        pass
    if len(good_examples) < 20:
        return
    # Write Modelfile with examples
    examples_block = "\n".join(
        f'MESSAGE user "{ex["user"][:200]}"\nMESSAGE assistant "{ex["assistant"][:400]}"'
        for ex in good_examples[:10]
    )
    modelfile = f"""FROM llama3.1:8b
PARAMETER temperature 0.5
PARAMETER num_ctx 4096
SYSTEM \"\"\"You are an expert AI assistant for industrial design. You specialize in:
- 3D modeling and Blender Python scripting
- Materials science and engineering (metals, plastics, composites)
- Manufacturing processes (CNC, injection molding, casting, 3D printing)
- Cost estimation and supplier selection
- Product design, ergonomics, and DFM
- Quality control and warranty documentation

Always give practical, actionable advice backed by real engineering knowledge.
Structure responses clearly: Summary → Details → Recommendations.
For code, always provide complete, runnable Blender Python scripts.
\"\"\"

{examples_block}
"""
    mf_path = os.path.join(r"C:\Users\nickn\local-ai-production", "Modelfile.auto")
    with open(mf_path, "w", encoding="utf-8") as f:
        f.write(modelfile)
    # Mark last build
    with open(flag, "w") as f:
        f.write(datetime.now().isoformat())
    # Trigger ollama create in background
    def _build():
        try:
            subprocess.run(["ollama", "create", "industrial-ai", "-f", mf_path],
                           capture_output=True, text=True, timeout=120)
        except Exception:
            pass
    threading.Thread(target=_build, daemon=True).start()

import time

def get_training_stats() -> dict:
    """Count logged conversations and training examples."""
    total = rated_good = rated_bad = 0
    try:
        for fname in os.listdir(LOGS_DIR):
            with open(os.path.join(LOGS_DIR, fname), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        total += 1
                        if e.get("rating") == 1: rated_good += 1
                        elif e.get("rating") == -1: rated_bad += 1
                    except Exception:
                        pass
    except Exception:
        pass
    training_count = 0
    try:
        with open(TRAINING_FILE, encoding="utf-8") as f:
            training_count = sum(1 for line in f if line.strip())
    except Exception:
        pass
    next_milestone = ((training_count // 50) + 1) * 50
    return {
        "total": total,
        "good": rated_good,
        "bad": rated_bad,
        "training_examples": training_count,
        "next_milestone": next_milestone,
        "progress_pct": int((training_count % 50) / 50 * 100)
    }

# ─── ROUTING RULES ───────────────────────────────────────────────────────────
# Each rule: keywords → model + agent persona
AGENTS = [
    {
        "id": "blender",
        "name": "3D Scripting",
        "icon": "cube",
        "color": "#f97316",
        "model_pref": ["qwen2.5-coder:7b", "llama3.1:8b", "mistral:7b"],
        "keywords": [
            "blender", "mesh", "geometry", "model", "3d", "object", "shape",
            "parametric", "script", "automate", "generate", "solid", "surface",
            "low poly", "lowpoly", "subdivision", "bevel", "extrude", "boolean",
            "stl", "obj", "fbx", "gltf", "cad", "part", "assembly", "sketch"
        ],
        "system": (
            "You are an expert Blender Python scripter and 3D modeling specialist for industrial design. "
            "When asked to create or modify 3D objects, generate complete, ready-to-run Blender Python scripts. "
            "Always include: import bpy, clear scene, create geometry, set materials. "
            "Explain each step in plain language for non-programmers. "
            "For parametric parts, make dimensions easy to modify at the top of the script. "
            "Format code in ```python blocks."
        )
    },
    {
        "id": "research",
        "name": "Engineering Research",
        "icon": "flask",
        "color": "#8b5cf6",
        "model_pref": ["llama3.1:8b", "mistral:7b", "deepseek-r1:8b"],
        "keywords": [
            "material", "alloy", "plastic", "metal", "steel", "aluminum", "aluminium",
            "stress", "strain", "force", "load", "weight", "strength", "hardness",
            "thermal", "temperature", "expansion", "conductivity", "resistance",
            "manufacturing", "machining", "injection", "molding", "casting", "cnc",
            "tolerance", "fit", "finish", "surface", "roughness", "cost", "price",
            "supplier", "vendor", "spec", "standard", "iso", "din", "astm",
            "physics", "simulation", "fea", "finite", "element", "fatigue",
            "corrosion", "wear", "durability", "lifespan", "maintenance"
        ],
        "system": (
            "You are a senior industrial engineer and materials scientist with 20+ years of experience. "
            "Provide detailed technical analysis covering: material properties with exact values, "
            "manufacturing process recommendations, cost estimates, tolerances, and relevant standards (ISO/DIN/ASTM). "
            "Structure answers clearly: Summary → Technical Details → Recommendations → Considerations. "
            "Always give practical, actionable advice for real manufacturing decisions. "
            "Include rough cost ranges when asked about manufacturing or materials."
        )
    },
    {
        "id": "product",
        "name": "Product & Ecommerce",
        "icon": "tag",
        "color": "#10b981",
        "model_pref": ["mistral:7b", "llama3.1:8b", "qwen3.5:4b"],
        "keywords": [
            "product", "listing", "description", "ecommerce", "shop", "store",
            "catalog", "sku", "inventory", "price", "sell", "market", "customer",
            "feature", "benefit", "specification", "datasheet", "brochure",
            "warranty", "claim", "defect", "quality", "inspection", "control",
            "return", "repair", "service", "support", "documentation"
        ],
        "system": (
            "You are an expert in product management, industrial product documentation, and ecommerce. "
            "Help create product listings, technical specifications, warranty documentation, "
            "and quality control procedures for industrial and consumer products. "
            "Be precise with technical specs while keeping customer-facing content clear and compelling. "
            "For QC and warranty: provide structured checklists and decision trees."
        )
    },
    {
        "id": "design",
        "name": "Design Thinking",
        "icon": "pencil",
        "color": "#3b82f6",
        "model_pref": ["qwen3.5:4b", "llama3.1:8b", "mistral:7b"],
        "keywords": [
            "design", "concept", "idea", "sketch", "aesthetic", "form", "function",
            "ergonomic", "ergonomics", "user", "experience", "prototype", "iteration",
            "feedback", "improve", "redesign", "optimize", "simplify", "elegant",
            "visual", "render", "presentation", "color", "texture", "finish",
            "industrial design", "product design", "form factor", "proportions"
        ],
        "system": (
            "You are a senior industrial designer with expertise in product design, ergonomics, "
            "and design thinking methodology. Help with concept development, design critique, "
            "form and function analysis, user experience considerations, and design iteration. "
            "Think like a designer: balance aesthetics, function, manufacturing feasibility, and cost. "
            "Be creative but practical — designs must be manufacturable."
        )
    },
    {
        "id": "deep",
        "name": "Deep Research",
        "icon": "brain",
        "color": "#ec4899",
        "model_pref": ["deepseek-r1:8b", "llama3.1:8b"],
        "keywords": [
            "deep research", "analyze in depth", "detailed analysis", "step by step calculation",
            "fea analysis", "failure mode", "root cause", "stress analysis", "thermal analysis",
            "finite element", "simulation", "complex", "thorough", "comprehensive"
        ],
        "system": (
            "You are a deep-thinking engineering analyst. Take your time to reason step by step. "
            "For every problem: define the problem clearly, list assumptions, work through calculations, "
            "consider edge cases, and provide a well-reasoned conclusion with confidence levels. "
            "Show your reasoning process explicitly."
        )
    },
    {
        "id": "general",
        "name": "Assistant",
        "icon": "chat",
        "color": "#6b7280",
        "model_pref": ["llama3.1:8b", "qwen3.5:4b", "mistral:7b"],
        "keywords": [],
        "system": (
            "You are a helpful AI assistant for an industrial designer. "
            "Answer questions clearly and concisely. When the topic involves 3D modeling, materials, "
            "manufacturing, or product development, lean on your knowledge of industrial design practice. "
            "Always keep answers practical and actionable."
        )
    }
]

QUICK_ACTIONS = [
    {"label": "Design a part", "prompt": "Help me design a parametric bracket for 40x40 aluminum extrusion, I need mounting holes and a gusset for strength"},
    {"label": "Material selection", "prompt": "I need a material recommendation for an outdoor housing exposed to UV and rain, needs to be lightweight and cost-effective"},
    {"label": "Generate 3D script", "prompt": "Write a Blender Python script to create a simple enclosure box 100x60x30mm with 2mm wall thickness and filleted corners"},
    {"label": "Manufacturing cost", "prompt": "Estimate the cost of CNC machining vs injection molding for a small plastic enclosure at 100, 1000, and 10000 unit quantities"},
    {"label": "Product description", "prompt": "Write a professional product listing for an industrial-grade waterproof enclosure IP67 rated, aluminum, for electronics"},
    {"label": "QC checklist", "prompt": "Create a quality control inspection checklist for a machined aluminum part with threaded holes and surface finish requirements"},
]

# ─── ROUTING LOGIC ───────────────────────────────────────────────────────────

def detect_agent(message: str) -> dict:
    """Score each agent by keyword matches, return best match."""
    msg_lower = message.lower()
    scores = {}
    for agent in AGENTS:
        if not agent["keywords"]:
            scores[agent["id"]] = 0
            continue
        score = sum(1 for kw in agent["keywords"] if kw in msg_lower)
        scores[agent["id"]] = score

    best_id = max(scores, key=scores.get)
    best_score = scores[best_id]

    if best_score == 0:
        return next(a for a in AGENTS if a["id"] == "general")
    return next(a for a in AGENTS if a["id"] == best_id)

def pick_model(agent: dict, available: list) -> str:
    """Pick best available model for the agent."""
    for pref in agent["model_pref"]:
        for avail in available:
            if avail.startswith(pref.split(":")[0]):
                return avail
    return available[0] if available else "llama3.1:8b"

def get_models() -> list:
    """Fetch available models from Ollama."""
    try:
        req = urllib.request.Request(f"{OLLAMA}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []

_html_cache = None
def build_html_cached():
    global _html_cache
    if _html_cache is None:
        _html_cache = build_html()
    return _html_cache

def warmup_model(model: str):
    """Pre-load a model into VRAM by sending a short request."""
    def _warmup():
        try:
            payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False, "options": {"num_predict": 1}}).encode()
            req = urllib.request.Request(f"{OLLAMA}/api/chat", data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30)
        except Exception:
            pass
    threading.Thread(target=_warmup, daemon=True).start()

def ollama_chat(model: str, system: str, history: list, message: str) -> str:
    """Send chat request to Ollama, return response text."""
    messages = [{"role": "system", "content": system}]
    for h in history[-6:]:  # last 6 exchanges for context
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7, "num_ctx": 4096}
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
            return data.get("message", {}).get("content", "No response")
    except urllib.error.URLError as e:
        return f"[Error connecting to Ollama: {e}. Make sure Ollama is running.]"
    except Exception as e:
        return f"[Error: {e}]"

def find_blender() -> str | None:
    """Find Blender executable on this system."""
    candidates = []
    # Check standard install paths
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    for base in [pf, pf86]:
        bf = os.path.join(base, "Blender Foundation")
        if os.path.isdir(bf):
            for entry in sorted(os.listdir(bf), reverse=True):
                exe = os.path.join(bf, entry, "blender.exe")
                if os.path.exists(exe):
                    candidates.append(exe)
    # Also check Steam and manual installs
    extra = [
        r"C:\blender\blender.exe",
        r"C:\tools\blender\blender.exe",
        os.path.expanduser(r"~\blender\blender.exe"),
    ]
    for p in extra:
        if os.path.exists(p):
            candidates.append(p)
    return candidates[0] if candidates else None

def open_blender() -> dict:
    """Launch Blender UI."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "msg": "Blender not found. Is it installed in Program Files?"}
    try:
        subprocess.Popen([blender], creationflags=subprocess.DETACHED_PROCESS)
        return {"ok": True, "msg": f"Blender launched: {os.path.basename(os.path.dirname(blender))}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def ollama_stream(model: str, system: str, history: list, message: str):
    """Generator: yield token strings from Ollama streaming API."""
    messages = [{"role": "system", "content": system}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})
    payload = json.dumps({
        "model": model, "messages": messages, "stream": True,
        "options": {"temperature": 0.7, "num_ctx": 4096}
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            for raw_line in r:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if chunk.get("done"):
                        break
                except Exception:
                    pass
    except Exception as e:
        yield f"[Stream error: {e}]"

def run_blender_script(script: str, open_ui: bool = True) -> dict:
    """Save script to temp file and open Blender with it. open_ui=True opens the GUI."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "msg": "Blender not found. Is it installed in Program Files?"}

    script_path = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "studio_blender_script.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    try:
        if open_ui:
            # Open Blender UI and run the script
            subprocess.Popen(
                [blender, "--python", script_path],
                creationflags=subprocess.DETACHED_PROCESS
            )
            return {"ok": True, "msg": "Blender opened and script is running", "script_path": script_path}
        else:
            result = subprocess.run(
                [blender, "--background", "--python", script_path],
                capture_output=True, text=True, timeout=60
            )
            out = result.stdout[-800:] if result.stdout else result.stderr[-800:]
            return {"ok": True, "msg": out, "script_path": script_path}
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Script timed out after 60s"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ─── ACTION DETECTION ─────────────────────────────────────────────────────────

ACTION_PATTERNS = {
    "open_blender": [
        "open blender", "launch blender", "start blender", "run blender",
        "open up blender", "start up blender", "open the blender"
    ],
    "check_models": [
        "what models", "which models", "list models", "show models",
        "models available", "models do i have", "models do you have"
    ],
    "system_status": [
        "system status", "what's running", "whats running", "status check",
        "is ollama running", "is blender installed"
    ]
}

def detect_action(message: str) -> str | None:
    """Return action name if message matches a direct action, else None."""
    msg = message.lower().strip()
    for action, patterns in ACTION_PATTERNS.items():
        for p in patterns:
            if p in msg:
                return action
    return None

def handle_action(action: str) -> dict:
    """Execute a direct action and return result for the UI."""
    if action == "open_blender":
        result = open_blender()
        if result["ok"]:
            return {
                "response": f"Blender is opening now.\n\nOnce it's open, you can:\n- Ask me to **generate a 3D script** and I'll write it for you\n- Click **Run in Blender** on any script I generate to execute it automatically\n- Ask me about materials, dimensions, or manufacturing for your parts",
                "action": "open_blender",
                "action_ok": True,
                "agent_id": "blender",
                "agent_name": "3D Scripting",
                "agent_color": "#f97316"
            }
        else:
            return {
                "response": f"Could not open Blender: {result['msg']}\n\nMake sure Blender is installed in Program Files.",
                "action": "open_blender",
                "action_ok": False,
                "agent_id": "blender",
                "agent_name": "3D Scripting",
                "agent_color": "#f97316"
            }

    elif action == "check_models":
        models = get_models()
        if models:
            lines = "\n".join(f"- {m}" for m in models)
            return {
                "response": f"Models currently loaded in Ollama:\n\n{lines}\n\nTo download more: ask me to pull qwen2.5-coder, mistral, or qwen3.5.",
                "action": action, "action_ok": True,
                "agent_id": "general", "agent_name": "Assistant", "agent_color": "#6b7280"
            }
        else:
            return {
                "response": "Ollama is offline or no models installed. Run LAUNCH.bat first.",
                "action": action, "action_ok": False,
                "agent_id": "general", "agent_name": "Assistant", "agent_color": "#6b7280"
            }

    elif action == "system_status":
        models = get_models()
        blender = find_blender()
        lines = []
        lines.append(f"Ollama: {'online, ' + str(len(models)) + ' models loaded' if models else 'OFFLINE'}")
        if models:
            for m in models:
                lines.append(f"  - {m}")
        lines.append(f"Blender: {'found at ' + os.path.basename(os.path.dirname(blender)) if blender else 'NOT FOUND'}")
        return {
            "response": "\n".join(lines),
            "action": action, "action_ok": True,
            "agent_id": "general", "agent_name": "Assistant", "agent_color": "#6b7280"
        }

# ─── HTML UI ─────────────────────────────────────────────────────────────────

def build_html():
    agents_json = json.dumps([
        {"id": a["id"], "name": a["name"], "color": a["color"]} for a in AGENTS
    ])
    quick_json = json.dumps(QUICK_ACTIONS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Industrial AI Studio</title>
<style>
  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #21242f;
    --border: #2d3148;
    --text: #e2e8f0;
    --muted: #64748b;
    --accent: #6366f1;
    --accent2: #818cf8;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; overflow: hidden; }}

  /* Sidebar */
  .sidebar {{ width: 260px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; }}
  .logo {{ padding: 20px; border-bottom: 1px solid var(--border); }}
  .logo h1 {{ font-size: 15px; font-weight: 700; color: var(--text); letter-spacing: 0.5px; }}
  .logo p {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}

  .section-title {{ font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; padding: 16px 16px 8px; }}

  .agent-card {{ display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-radius: 6px; margin: 2px 8px; cursor: default; transition: background 0.15s; }}
  .agent-card:hover {{ background: var(--surface2); }}
  .agent-card.active {{ background: var(--surface2); border: 1px solid var(--border); }}
  .agent-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
  .agent-name {{ font-size: 13px; }}
  .agent-status {{ font-size: 10px; color: var(--muted); margin-left: auto; }}

  .quick-section {{ padding: 8px; margin-top: auto; border-top: 1px solid var(--border); }}
  .quick-section .section-title {{ padding: 10px 8px 6px; }}
  .quick-btn {{ display: block; width: 100%; text-align: left; padding: 7px 10px; font-size: 12px; color: var(--muted); background: none; border: 1px solid var(--border); border-radius: 5px; cursor: pointer; margin-bottom: 4px; transition: all 0.15s; }}
  .quick-btn:hover {{ background: var(--surface2); color: var(--text); border-color: var(--accent); }}

  /* Main */
  .main {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}

  .topbar {{ height: 52px; background: var(--surface); border-bottom: 1px solid var(--border); display: flex; align-items: center; padding: 0 20px; gap: 12px; flex-shrink: 0; }}
  .status-pill {{ display: flex; align-items: center; gap: 6px; background: var(--surface2); border: 1px solid var(--border); border-radius: 20px; padding: 4px 12px; font-size: 12px; }}
  .status-dot {{ width: 6px; height: 6px; border-radius: 50%; background: #22c55e; }}
  .status-dot.off {{ background: #ef4444; }}
  .model-pill {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 20px; padding: 4px 12px; font-size: 12px; color: var(--accent2); }}
  .agent-pill {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 20px; padding: 4px 12px; font-size: 12px; }}

  /* Chat */
  .chat-area {{ flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; }}
  .chat-area::-webkit-scrollbar {{ width: 4px; }}
  .chat-area::-webkit-scrollbar-track {{ background: transparent; }}
  .chat-area::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}

  .msg-row {{ display: flex; gap: 12px; max-width: 820px; }}
  .msg-row.user {{ flex-direction: row-reverse; margin-left: auto; }}

  .avatar {{ width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }}
  .avatar.ai {{ background: var(--accent); }}
  .avatar.user {{ background: var(--surface2); border: 1px solid var(--border); }}

  .bubble-wrap {{ display: flex; flex-direction: column; gap: 4px; }}
  .bubble-meta {{ font-size: 10px; color: var(--muted); padding: 0 4px; display: flex; align-items: center; gap: 6px; }}
  .msg-row.user .bubble-meta {{ justify-content: flex-end; }}
  .agent-badge {{ font-size: 10px; padding: 1px 6px; border-radius: 10px; color: white; }}

  .bubble {{ padding: 12px 16px; border-radius: 10px; font-size: 14px; line-height: 1.6; max-width: 700px; word-break: break-word; }}
  .bubble.ai {{ background: var(--surface); border: 1px solid var(--border); border-top-left-radius: 2px; }}
  .bubble.user {{ background: var(--accent); color: white; border-top-right-radius: 2px; }}
  .code-wrap {{ position: relative; margin: 8px 0; }}
  .bubble pre {{ background: #0d0f14; border: 1px solid var(--border); border-radius: 6px 6px 0 0; padding: 12px; overflow-x: auto; margin: 0; }}
  .run-blender-btn {{ display: block; width: 100%; padding: 7px 12px; background: #f97316; border: none; border-radius: 0 0 6px 6px; color: white; font-size: 12px; font-weight: 600; cursor: pointer; text-align: left; letter-spacing: 0.3px; transition: opacity 0.15s; }}
  .run-blender-btn:hover {{ opacity: 0.85; }}
  .run-blender-btn:disabled {{ opacity: 0.6; cursor: not-allowed; }}
  .bubble code {{ font-family: 'Consolas', monospace; font-size: 12px; }}
  .bubble p {{ margin-bottom: 8px; }}
  .bubble p:last-child {{ margin-bottom: 0; }}
  .bubble ul, .bubble ol {{ padding-left: 20px; margin-bottom: 8px; }}
  .bubble li {{ margin-bottom: 4px; }}
  .bubble strong {{ color: #a5b4fc; }}
  .bubble h3 {{ color: var(--accent2); margin: 10px 0 6px; font-size: 13px; }}

  .thinking {{ display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }}
  .dots {{ display: flex; gap: 3px; }}
  .dots span {{ width: 4px; height: 4px; background: var(--muted); border-radius: 50%; animation: bounce 1.4s infinite; }}
  .dots span:nth-child(2) {{ animation-delay: 0.2s; }}
  .dots span:nth-child(3) {{ animation-delay: 0.4s; }}
  @keyframes bounce {{ 0%,80%,100% {{ transform: scale(0.6); opacity:0.4 }} 40% {{ transform: scale(1); opacity:1 }} }}

  /* Input */
  .input-bar {{ background: var(--surface); border-top: 1px solid var(--border); padding: 16px 20px; flex-shrink: 0; }}
  .input-wrap {{ display: flex; gap: 10px; align-items: flex-end; background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; transition: border-color 0.15s; }}
  .input-wrap:focus-within {{ border-color: var(--accent); }}
  .input-wrap textarea {{ flex: 1; background: none; border: none; color: var(--text); font-size: 14px; resize: none; outline: none; max-height: 120px; min-height: 24px; line-height: 1.5; font-family: inherit; }}
  .input-wrap textarea::placeholder {{ color: var(--muted); }}
  .send-btn {{ width: 34px; height: 34px; background: var(--accent); border: none; border-radius: 7px; cursor: pointer; color: white; font-size: 16px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: opacity 0.15s; }}
  .send-btn:hover {{ opacity: 0.85; }}
  .send-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  .input-hint {{ font-size: 11px; color: var(--muted); margin-top: 8px; text-align: center; }}

  /* Welcome */
  .welcome {{ max-width: 560px; margin: auto; text-align: center; padding: 40px 20px; }}
  .welcome h2 {{ font-size: 22px; font-weight: 700; margin-bottom: 10px; }}
  .welcome p {{ color: var(--muted); font-size: 14px; line-height: 1.6; margin-bottom: 24px; }}
  .welcome-chips {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }}
  .chip {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 20px; padding: 6px 14px; font-size: 12px; color: var(--muted); cursor: pointer; transition: all 0.15s; }}
  .chip:hover {{ border-color: var(--accent); color: var(--text); background: var(--surface); }}

  /* Rating */
  .rating-bar {{ display: flex; gap: 6px; margin-top: 6px; align-items: center; }}
  .rate-btn {{ background: none; border: 1px solid var(--border); border-radius: 5px; color: var(--muted); font-size: 13px; padding: 2px 8px; cursor: pointer; transition: all 0.15s; }}
  .rate-btn:hover {{ border-color: var(--accent); color: var(--text); }}
  .rate-btn.good {{ background: #14532d; border-color: #16a34a; color: #4ade80; }}
  .rate-btn.bad  {{ background: #450a0a; border-color: #b91c1c; color: #f87171; }}
  .copy-btn {{ background: none; border: 1px solid var(--border); border-radius: 5px; color: var(--muted); font-size: 11px; padding: 2px 8px; cursor: pointer; transition: all 0.15s; margin-left: 4px; }}
  .copy-btn:hover {{ background: var(--surface2); color: var(--text); }}
  .rate-saved {{ font-size: 11px; color: var(--muted); }}

  /* Mobile responsive */
  @media (max-width: 768px) {{
    .sidebar {{ display: none; }}
    .topbar {{ padding: 0 12px; gap: 6px; overflow-x: auto; }}
    .model-pill, .agent-pill {{ display: none; }}
    .chat-area {{ padding: 12px; }}
    .bubble {{ max-width: 100%; font-size: 13px; }}
    .input-bar {{ padding: 10px 12px; }}
    .input-wrap textarea {{ font-size: 16px; }} /* prevent iOS zoom */
    .agent-ex-grid {{ grid-template-columns: 1fr; }}
    .approval-btns {{ flex-direction: column; }}
    .mode-toggle {{ margin-left: 0; }}
  }}

  /* Training stats */
  .train-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin: 8px; }}
  .train-title {{ font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }}
  .train-row {{ display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }}
  .train-val {{ color: var(--accent2); font-weight: 600; }}
  .train-bar-wrap {{ background: var(--surface2); border-radius: 4px; height: 4px; margin-top: 8px; }}
  .train-bar-fill {{ background: var(--accent); border-radius: 4px; height: 4px; transition: width 0.4s; }}
  .train-next {{ font-size: 10px; color: var(--muted); margin-top: 4px; }}

  /* Agent Mode */
  .mode-toggle {{ display: flex; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 3px; margin-left: auto; }}
  .mode-btn {{ padding: 4px 14px; border-radius: 5px; border: none; background: none; color: var(--muted); font-size: 12px; cursor: pointer; transition: all 0.15s; font-family: inherit; }}
  .mode-btn.active {{ background: var(--accent); color: white; }}

  #agentPanel {{ flex: 1; overflow-y: auto; padding: 24px; display: none; flex-direction: column; gap: 14px; }}
  #agentPanel.visible {{ display: flex; }}
  #agentPanel::-webkit-scrollbar {{ width: 4px; }}
  #agentPanel::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}

  .agent-header {{ padding: 4px 0 8px; }}
  .agent-header-title {{ font-size: 18px; font-weight: 700; margin-bottom: 6px; }}
  .agent-header-sub {{ font-size: 13px; color: var(--muted); line-height: 1.5; }}

  .agent-task-bar {{ background: var(--surface); border: 2px solid var(--border); border-radius: 10px; padding: 14px 16px; transition: border-color 0.15s; }}
  .agent-task-bar:focus-within {{ border-color: #8b5cf6; }}
  .agent-task-bar textarea {{ width: 100%; background: none; border: none; color: var(--text); font-size: 14px; resize: none; outline: none; font-family: inherit; line-height: 1.6; }}
  .agent-task-bar textarea::placeholder {{ color: var(--muted); }}

  .agent-controls {{ display: flex; align-items: center; gap: 10px; }}
  .run-agent-btn {{ padding: 9px 20px; background: #8b5cf6; border: none; border-radius: 7px; color: white; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }}
  .run-agent-btn:hover {{ opacity: 0.85; }}
  .run-agent-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  .stop-agent-btn {{ padding: 9px 14px; background: #374151; border: 1px solid var(--border); border-radius: 7px; color: var(--muted); font-size: 13px; font-weight: 600; cursor: not-allowed; opacity: 0.4; }}
  .stop-agent-btn.visible {{ background: #ef4444; border-color: #ef4444; color: white; cursor: pointer; opacity: 1; }}
  .agent-hint {{ font-size: 11px; color: var(--muted); margin-left: 4px; }}
  .auto-toggle {{ display: flex; align-items: center; gap: 5px; cursor: pointer; padding: 5px 10px; background: var(--surface2); border: 1px solid var(--border); border-radius: 7px; user-select: none; }}
  .auto-toggle input {{ accent-color: #f97316; cursor: pointer; }}
  .auto-label {{ font-size: 12px; color: var(--muted); white-space: nowrap; }}
  .auto-toggle:has(input:checked) {{ border-color: #f97316; background: #1c1008; }}
  .auto-toggle:has(input:checked) .auto-label {{ color: #fb923c; }}

  .agent-examples {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
  .agent-ex-title {{ font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }}
  .agent-ex-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .agent-ex-btn {{ text-align: left; padding: 9px 12px; background: var(--surface2); border: 1px solid var(--border); border-radius: 7px; color: var(--muted); font-size: 12px; cursor: pointer; transition: all 0.15s; font-family: inherit; line-height: 1.4; }}
  .agent-ex-btn:hover {{ border-color: #8b5cf6; color: var(--text); background: var(--surface); }}

  .step-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  .step-header {{ display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--border); }}
  .step-num {{ font-size: 10px; color: var(--muted); background: var(--surface2); border-radius: 4px; padding: 1px 6px; }}
  .step-tool {{ font-size: 12px; font-weight: 600; color: var(--text); }}
  .level-tag {{ font-size: 10px; padding: 2px 7px; border-radius: 10px; margin-left: auto; }}
  .level-auto {{ background: #14532d; color: #4ade80; }}
  .level-ask {{ background: #7c2d12; color: #fb923c; }}
  .step-thought {{ padding: 10px 14px; font-size: 12px; color: var(--muted); line-height: 1.5; border-bottom: 1px solid var(--border); }}
  .step-args {{ padding: 8px 14px; font-size: 11px; font-family: monospace; color: #a5b4fc; background: #0d0f14; border-bottom: 1px solid var(--border); overflow-x: auto; }}
  .step-result {{ padding: 10px 14px; font-size: 12px; color: var(--text); font-family: monospace; white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; }}
  .step-done {{ background: #14532d; border-color: #166534; }}
  .step-done .step-header {{ border-bottom: none; }}
  .step-done-text {{ padding: 12px 14px; font-size: 13px; color: #4ade80; }}

  .approval-card {{ background: #1c1117; border: 2px solid #f97316; border-radius: 10px; padding: 16px; }}
  .approval-title {{ font-size: 13px; font-weight: 700; color: #fb923c; margin-bottom: 8px; }}
  .approval-desc {{ font-size: 12px; color: var(--muted); margin-bottom: 12px; line-height: 1.5; }}
  .approval-args {{ background: #0d0f14; border-radius: 6px; padding: 8px 12px; font-size: 11px; font-family: monospace; color: #a5b4fc; margin-bottom: 12px; white-space: pre-wrap; }}
  .approval-btns {{ display: flex; gap: 8px; }}
  .btn-approve {{ flex: 1; padding: 8px; background: #16a34a; border: none; border-radius: 6px; color: white; font-size: 13px; font-weight: 600; cursor: pointer; }}
  .btn-deny {{ flex: 1; padding: 8px; background: #dc2626; border: none; border-radius: 6px; color: white; font-size: 13px; font-weight: 600; cursor: pointer; }}
</style>
</head>
<body>

<div class="sidebar">
  <div class="logo">
    <h1>Industrial AI Studio</h1>
    <p>Auto-routing AI orchestrator</p>
  </div>

  <div class="section-title">Active Agents</div>
  <div id="agentList"></div>

  <div class="train-card">
    <div class="train-title">Training Data</div>
    <div class="train-row"><span>Conversations</span><span class="train-val" id="statTotal">-</span></div>
    <div class="train-row"><span>Good examples</span><span class="train-val" id="statGood">-</span></div>
    <div class="train-row"><span>Training set</span><span class="train-val" id="statTraining">-</span></div>
    <div class="train-bar-wrap"><div class="train-bar-fill" id="trainBar" style="width:0%"></div></div>
    <div class="train-next" id="trainNext">Rate responses with 👍 to build training data</div>
  </div>

  <div class="quick-section">
    <div class="section-title">Quick Start</div>
    <div id="quickBtns"></div>
  </div>
</div>

<div class="main">
  <div class="topbar">
    <div class="status-pill">
      <div class="status-dot" id="ollamaStatus"></div>
      <span id="ollamaLabel">Checking...</span>
    </div>
    <div class="model-pill" id="modelPill">No model</div>
    <div class="agent-pill" id="agentPill">Ready</div>
    <button onclick="clearChat()" style="background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);font-size:11px;padding:4px 10px;cursor:pointer;margin-left:auto;">clear</button>
    <div class="mode-toggle">
      <button class="mode-btn active" id="modeChatBtn" onclick="setMode('chat')">Chat</button>
      <button class="mode-btn" id="modeAgentBtn" onclick="setMode('agent')">Agent</button>
    </div>
  </div>

  <div class="chat-area" id="chatArea">
    <div class="welcome" id="welcome">
      <h2>What do you need today?</h2>
      <p>Describe your task in plain language. I'll automatically select the right AI model and tools — you don't need to know anything technical.</p>
      <div class="welcome-chips" id="welcomeChips"></div>
    </div>
  </div>

  <div id="agentPanel">
    <div class="agent-header">
      <div class="agent-header-title">Autonomous Agent</div>
      <div class="agent-header-sub">Describe a task in plain language — the agent figures out the steps, runs tools, and asks before anything sensitive</div>
    </div>
    <div class="agent-task-bar">
      <textarea id="agentTask" placeholder="e.g. Check which Ollama models I have, pull any missing ones, and create a custom industrial-design model..." rows="3"></textarea>
    </div>
    <div class="agent-controls">
      <button class="run-agent-btn" id="runAgentBtn" onclick="startAgent()">&#9654;&nbsp; Run Agent</button>
      <button class="stop-agent-btn" id="stopAgentBtn" onclick="stopAgent()">&#9632;&nbsp; Stop Agent</button>
      <label class="auto-toggle" title="Auto-approve all steps — use when you trust the task and want to go AFK">
        <input type="checkbox" id="autoApprove"> <span class="auto-label">Auto mode</span>
      </label>
      <span class="agent-hint" id="agentHintText">Orange ASK steps need your approval</span>
    </div>
    <div class="agent-examples">
      <div class="agent-ex-title">Example tasks</div>
      <div class="agent-ex-grid" id="agentExamples"></div>
    </div>
    <div id="agentSteps"></div>
  </div>

  <div class="input-bar" id="chatInputBar">
    <div class="input-wrap">
      <textarea id="input" placeholder="Describe what you need... (e.g. 'I need a bracket for a motor mount, 80x60mm, 4mm steel')" rows="1"></textarea>
      <button class="send-btn" id="sendBtn" onclick="send()">&#9654;</button>
    </div>
    <div class="input-hint">Enter to send &nbsp;|&nbsp; Shift+Enter for new line &nbsp;|&nbsp; Auto-selects best model for your task</div>
  </div>
</div>

<script>
const AGENTS = {agents_json};
const QUICK = {quick_json};
let history = [];
let currentModel = '';
let currentAgent = '';
let ollamaOnline = false;

// ── Sidebar ──────────────────────────────────────────────────────────────
function buildSidebar() {{
  const list = document.getElementById('agentList');
  AGENTS.forEach(a => {{
    const el = document.createElement('div');
    el.className = 'agent-card';
    el.id = 'agent-' + a.id;
    el.innerHTML = `
      <div class="agent-dot" style="background:${{a.color}}"></div>
      <span class="agent-name">${{a.name}}</span>
      <span class="agent-status" id="astatus-${{a.id}}">idle</span>
    `;
    list.appendChild(el);
  }});

  const qc = document.getElementById('quickBtns');
  QUICK.slice(0, 4).forEach(q => {{
    const btn = document.createElement('button');
    btn.className = 'quick-btn';
    btn.textContent = q.label;
    btn.onclick = () => {{
      document.getElementById('input').value = q.prompt;
      send();
    }};
    qc.appendChild(btn);
  }});

  const chips = document.getElementById('welcomeChips');
  QUICK.forEach(q => {{
    const c = document.createElement('div');
    c.className = 'chip';
    c.textContent = q.label;
    c.onclick = () => {{
      document.getElementById('input').value = q.prompt;
      send();
    }};
    chips.appendChild(c);
  }});
}}

// ── Status ───────────────────────────────────────────────────────────────
async function checkStatus() {{
  try {{
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 3000);
    const r = await fetch('/api/status', {{signal: ctrl.signal, cache: 'no-store'}});
    clearTimeout(t);
    const d = await r.json();
    ollamaOnline = d.ollama;
    document.getElementById('ollamaStatus').className = 'status-dot' + (ollamaOnline ? '' : ' off');
    document.getElementById('ollamaLabel').textContent = ollamaOnline ? 'Ollama online' : 'Ollama offline';
    if (d.models && d.models.length > 0) {{
      if (!currentModel) currentModel = d.models[0];
      document.getElementById('modelPill').textContent = currentModel;
    }}
    if (d.stats) updateTrainingStats(d.stats);
  }} catch(e) {{
    document.getElementById('ollamaStatus').className = 'status-dot off';
    document.getElementById('ollamaLabel').textContent = 'Ollama offline';
  }}
}}

// ── Message rendering ─────────────────────────────────────────────────
function formatText(text) {{
  // Code blocks — extract and replace with placeholder to avoid double-processing
  const codeBlocks = [];
  text = text.replace(/```(\\w+)?\\n?([\\s\\S]*?)```/g, (_, lang, code) => {{
    const idx = codeBlocks.length;
    const isPython = (lang || '').toLowerCase() === 'python' || code.includes('import bpy');
    const runBtn = isPython
      ? `<button class="run-blender-btn" onclick="runInBlender(this)" data-code="${{encodeURIComponent(code.trim())}}">&#9654; Run in Blender</button>`
      : '';
    codeBlocks.push(`<div class="code-wrap"><pre><code>${{code.trim()}}</code></pre>${{runBtn}}</div>`);
    return `__CODE_${{idx}}__`;
  }});
  // Inline code
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  text = text.replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>');
  // Headers
  text = text.replace(/^###\\s+(.+)$/gm, '<h3>$1</h3>');
  // Bullets
  text = text.replace(/^[-*]\\s+(.+)$/gm, '<li>$1</li>');
  text = text.replace(/(<li>[\\s\\S]*?<\\/li>)+/g, m => '<ul>' + m + '</ul>');
  // Numbered lists
  text = text.replace(/^\\d+\\.\\s+(.+)$/gm, '<li>$1</li>');
  // Line breaks to paragraphs
  const paras = text.split('\\n\\n').filter(p => p.trim());
  text = paras.map(p => p.startsWith('<') ? p : `<p>${{p.replace(/\\n/g, '<br>')}}</p>`).join('');
  // Restore code blocks
  codeBlocks.forEach((block, i) => {{ text = text.replace(`__CODE_${{i}}__`, block); }});
  return text;
}}

async function runInBlender(btn) {{
  const code = decodeURIComponent(btn.getAttribute('data-code'));
  btn.textContent = 'Opening Blender...';
  btn.disabled = true;
  try {{
    const r = await fetch('/api/run_blender', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{script: code, open_ui: true}})
    }});
    const d = await r.json();
    if (d.ok) {{
      btn.textContent = 'Blender opened!';
      btn.style.background = '#22c55e';
    }} else {{
      btn.textContent = 'Error: ' + d.msg.slice(0, 40);
      btn.style.background = '#ef4444';
      btn.disabled = false;
    }}
  }} catch(e) {{
    btn.textContent = 'Failed: ' + e.message;
    btn.style.background = '#ef4444';
    btn.disabled = false;
  }}
}}

function updateTrainingStats(stats) {{
  if (!stats) return;
  document.getElementById('statTotal').textContent = stats.total;
  document.getElementById('statGood').textContent = stats.good;
  document.getElementById('statTraining').textContent = stats.training_examples;
  document.getElementById('trainBar').style.width = stats.progress_pct + '%';
  document.getElementById('trainNext').textContent =
    stats.training_examples > 0
      ? `${{stats.training_examples % 50 || 50}} more 👍 until next training milestone (${{stats.next_milestone}} total)`
      : 'Rate responses with 👍 to build training data';
}}

async function rateResponse(entryId, rating, goodBtn, badBtn) {{
  goodBtn.disabled = badBtn.disabled = true;
  goodBtn.classList.toggle('good', rating === 1);
  badBtn.classList.toggle('bad', rating === -1);
  try {{
    const r = await fetch('/api/rate', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id: entryId, rating}})
    }});
    const d = await r.json();
    if (d.stats) updateTrainingStats(d.stats);
    const saved = goodBtn.closest('.rating-bar').querySelector('.rate-saved');
    if (saved) saved.textContent = rating === 1 ? 'Added to training data' : 'Noted';
  }} catch(e) {{}}
}}

function addMessage(role, content, agentName, agentColor, model, entryId) {{
  const welcome = document.getElementById('welcome');
  if (welcome) welcome.remove();

  const chat = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.className = 'msg-row ' + role;

  if (role === 'user') {{
    row.innerHTML = `
      <div class="avatar user">U</div>
      <div class="bubble-wrap">
        <div class="bubble user">${{content.replace(/</g,'&lt;').replace(/>/g,'&gt;')}}</div>
      </div>`;
  }} else {{
    const badge = agentName ? `<span class="agent-badge" style="background:${{agentColor}}">${{agentName}}</span>` : '';
    const modelTag = model && model !== 'direct-action' ? `<span>${{model}}</span>` : '';
    const ratingHtml = entryId ? `
      <div class="rating-bar">
        <button class="rate-btn" id="good-${{entryId}}" onclick="rateResponse('${{entryId}}',1,this,document.getElementById('bad-${{entryId}}'))">👍</button>
        <button class="rate-btn" id="bad-${{entryId}}"  onclick="rateResponse('${{entryId}}',-1,document.getElementById('good-${{entryId}}'),this)">👎</button>
        <span class="rate-saved"></span>
      </div>` : '';
    row.innerHTML = `
      <div class="avatar ai">AI</div>
      <div class="bubble-wrap">
        <div class="bubble-meta">${{badge}}${{modelTag}}</div>
        <div class="bubble ai">${{formatText(content)}}</div>
        ${{ratingHtml}}
      </div>`;
  }}
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
  return row;
}}

function addThinking(agentName, agentColor) {{
  const welcome = document.getElementById('welcome');
  if (welcome) welcome.remove();
  const chat = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.className = 'msg-row';
  row.id = 'thinking';
  row.innerHTML = `
    <div class="avatar ai">AI</div>
    <div class="bubble-wrap">
      <div class="bubble-meta"><span class="agent-badge" style="background:${{agentColor}}">${{agentName}}</span><span>working...</span></div>
      <div class="thinking"><div class="dots"><span></span><span></span><span></span></div> Thinking...</div>
    </div>`;
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
  return row;
}}

// ── Send ─────────────────────────────────────────────────────────────────
function setAgentActive(agentId, agentName) {{
  AGENTS.forEach(a => {{
    const el = document.getElementById('agent-' + a.id);
    if (el) el.classList.remove('active');
    const st = document.getElementById('astatus-' + a.id);
    if (st) st.textContent = 'idle';
  }});
  const el = document.getElementById('agent-' + agentId);
  if (el) el.classList.add('active');
  const st = document.getElementById('astatus-' + agentId);
  if (st) st.textContent = 'active';
  document.getElementById('agentPill').textContent = agentName || 'Ready';
}}

async function send() {{
  const input = document.getElementById('input');
  const msg = input.value.trim();
  if (!msg) return;
  if (!ollamaOnline) {{ alert('Ollama is offline. Please start Ollama first.'); return; }}

  input.value = '';
  input.style.height = 'auto';
  document.getElementById('sendBtn').disabled = true;

  addMessage('user', msg);
  history.push({{role: 'user', content: msg}});
  saveHistory();

  const thinking = addThinking('Routing...', '#6366f1');

  try {{
    const resp = await fetch('/api/chat', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{message: msg, history: history.slice(-12)}})
    }});

    const ct = resp.headers.get('content-type') || '';

    if (ct.includes('text/event-stream')) {{
      // ── STREAMING MODE ──────────────────────────────────
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '', fullText = '', entryId = '';
      let bubble = null, agentName = '', agentColor = '#6366f1', model = '';
      let thinkingRemoved = false;

      while (true) {{
        const {{done, value}} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {{stream: true}});
        const lines = buf.split('\n');
        buf = lines.pop();

        for (const line of lines) {{
          if (!line.startsWith('data: ')) continue;
          let ev;
          try {{ ev = JSON.parse(line.slice(6)); }} catch(e) {{ continue; }}

          if (ev.type === 'start') {{
            if (!thinkingRemoved) {{ thinking.remove(); thinkingRemoved = true; }}
            agentName = ev.agent_name; agentColor = ev.agent_color; model = ev.model;
            currentModel = model;
            document.getElementById('modelPill').textContent = model;
            setAgentActive(ev.agent_id, agentName);
            const row = addMessage('assistant', '', agentName, agentColor, model, null);
            bubble = row.querySelector('.bubble.ai');

          }} else if (ev.type === 'token') {{
            fullText += ev.token;
            if (bubble) bubble.innerHTML = formatText(fullText);
            // Scroll to bottom
            const chat = document.getElementById('chatArea');
            if (chat) chat.scrollTop = chat.scrollHeight;

          }} else if (ev.type === 'done') {{
            entryId = ev.entry_id;
            // Add rating buttons
            if (bubble && entryId) {{
              const wrap = bubble.closest('.bubble-wrap');
              if (wrap && !wrap.querySelector('.rating-bar')) {{
                const rDiv = document.createElement('div');
                rDiv.className = 'rating-bar';
                rDiv.innerHTML = `
                  <button class="rate-btn" id="good-${{entryId}}" onclick="rateResponse('${{entryId}}',1,this,document.getElementById('bad-${{entryId}}'))">👍</button>
                  <button class="rate-btn" id="bad-${{entryId}}"  onclick="rateResponse('${{entryId}}',-1,document.getElementById('good-${{entryId}}'),this)">👎</button>
                  <button class="copy-btn" onclick="copyMsg(this)">copy</button>
                  <span class="rate-saved"></span>`;
                wrap.appendChild(rDiv);
              }}
            }}
            history.push({{role: 'assistant', content: fullText}});
            saveHistory();

          }} else if (ev.type === 'error') {{
            if (!thinkingRemoved) {{ thinking.remove(); thinkingRemoved = true; }}
            addMessage('assistant', 'Error: ' + ev.msg, 'Error', '#ef4444', model, null);
          }}
        }}
      }}
      if (!thinkingRemoved) thinking.remove();

    }} else {{
      // ── NON-STREAMING (direct actions) ───────────────────
      const data = await resp.json();
      thinking.remove();
      currentModel = data.model || currentModel;
      document.getElementById('modelPill').textContent = currentModel;
      setAgentActive(data.agent_id, data.agent_name);
      addMessage('assistant', data.response, data.agent_name, data.agent_color, data.model, data.entry_id);
      history.push({{role: 'assistant', content: data.response}});
      saveHistory();
    }}

  }} catch(e) {{
    thinking.remove();
    addMessage('assistant', 'Connection error: ' + e.message, 'Error', '#ef4444', '', null);
  }}

  document.getElementById('sendBtn').disabled = false;
  input.focus();
}}

function copyMsg(btn) {{
  const bubble = btn.closest('.bubble-wrap').querySelector('.bubble.ai');
  if (bubble) navigator.clipboard.writeText(bubble.textContent).then(() => {{
    btn.textContent = 'copied!';
    setTimeout(() => btn.textContent = 'copy', 2000);
  }});
}}

// ── Input auto-resize ────────────────────────────────────────────────────
document.getElementById('input').addEventListener('input', function() {{
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
}});

document.getElementById('input').addEventListener('keydown', function(e) {{
  if (e.key === 'Enter' && !e.shiftKey) {{
    e.preventDefault();
    send();
  }}
}});

// ── Mode toggle ──────────────────────────────────────────────────────────
function setMode(mode) {{
  const isAgent = mode === 'agent';
  document.getElementById('modeChatBtn').classList.toggle('active', !isAgent);
  document.getElementById('modeAgentBtn').classList.toggle('active', isAgent);
  document.getElementById('chatArea').style.display = isAgent ? 'none' : 'flex';
  document.getElementById('chatInputBar').style.display = isAgent ? 'none' : 'block';
  document.getElementById('agentPanel').classList.toggle('visible', isAgent);
  if (isAgent) document.getElementById('agentTask').focus();
}}

// ── Agent Mode ───────────────────────────────────────────────────────────
let agentSessionId = null;
let agentPollTimer = null;
let agentStepCount = 0;

async function startAgent() {{
  const task = document.getElementById('agentTask').value.trim();
  if (!task) return;
  if (!ollamaOnline) {{ alert('Ollama is offline. Start it first.'); return; }}

  agentStepCount = 0;
  document.getElementById('agentSteps').innerHTML = '';
  document.getElementById('runAgentBtn').disabled = true;
  document.getElementById('stopAgentBtn').classList.add('visible');

  try {{
    const autoApprove = document.getElementById('autoApprove').checked;
    if (autoApprove) {{
      document.getElementById('agentHintText').textContent = 'AUTO MODE — all steps execute without asking';
      document.getElementById('agentHintText').style.color = '#fb923c';
    }}
    const r = await fetch('/api/agent/start', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{task, model: currentModel || 'llama3.1:8b', auto_approve: autoApprove}})
    }});
    const d = await r.json();
    if (d.error) {{ alert(d.error); return; }}
    agentSessionId = d.session_id;
    agentPollTimer = setInterval(pollAgent, 1500);
  }} catch(e) {{
    alert('Failed to start agent: ' + e.message);
    resetAgentUI();
  }}
}}

async function pollAgent() {{
  if (!agentSessionId) {{ clearInterval(agentPollTimer); return; }}
  try {{
    const r = await fetch('/api/agent/poll?sid=' + agentSessionId);
    if (!r.ok) return;
    const d = await r.json();
    if (d.error) return;
    renderNewSteps(d.steps || []);
    if (d.status === 'done' || d.status === 'error' || d.status === 'stopped') {{
      clearInterval(agentPollTimer);
      agentSessionId = null;
      resetAgentUI();
    }}
  }} catch(e) {{
    // ignore transient fetch errors during polling
  }}
}}

function renderNewSteps(steps) {{
  if (!steps || !steps.length) return;
  const container = document.getElementById('agentSteps');
  for (let i = agentStepCount; i < steps.length; i++) {{
    const card = buildStepCard(steps[i]);
    if (card && card.tagName) container.appendChild(card);
    agentStepCount = i + 1;
  }}
  container.scrollTop = container.scrollHeight;
}}

function buildStepCard(s) {{
  const div = document.createElement('div');

  if (s.type === 'done') {{
    div.className = 'step-card step-done';
    div.innerHTML = `
      <div class="step-header"><span style="color:#4ade80;font-weight:700">Task Complete</span></div>
      <div class="step-done-text">${{s.summary || s.thought || 'Done'}}</div>`;
    return div;
  }}

  if (s.type === 'error' || s.type === 'stopped') {{
    div.className = 'step-card';
    div.style.borderColor = '#ef4444';
    div.innerHTML = `<div class="step-header"><span style="color:#f87171">${{s.type === 'stopped' ? 'Stopped' : 'Error'}}: ${{s.msg || ''}}</span></div>`;
    return div;
  }}

  if (s.type === 'result') {{
    const existing = document.getElementById('step-card-' + s.n);
    if (existing) {{
      const res = document.createElement('div');
      res.className = 'step-result';
      res.textContent = s.result || '';
      existing.appendChild(res);
      const spinner = existing.querySelector('.spinner');
      if (spinner) spinner.remove();
    }}
    return document.createElement('span');  // empty, already updated above
  }}

  if (s.type === 'step') {{
    div.className = 'step-card';
    div.id = 'step-card-' + s.n;
    const levelClass = s.level === 'auto' ? 'level-auto' : 'level-ask';
    const levelLabel = s.level === 'auto' ? 'AUTO' : 'NEEDS APPROVAL';
    const argsStr = Object.keys(s.args || {{}}).length
      ? JSON.stringify(s.args, null, 2)
      : '(no args)';

    if (s.waiting) {{
      // Approval card
      div.className = 'approval-card';
      div.id = 'approval-' + s.n;
      div.innerHTML = `
        <div class="approval-title">Agent wants to: ${{s.tool}}</div>
        <div class="approval-desc">${{s.thought}}</div>
        <div class="approval-args">${{argsStr}}</div>
        <div class="approval-btns">
          <button class="btn-approve" onclick="approveStep(true, ${{s.n}})">Allow</button>
          <button class="btn-deny" onclick="approveStep(false, ${{s.n}})">Deny</button>
        </div>`;
    }} else {{
      div.innerHTML = `
        <div class="step-header">
          <span class="step-num">Step ${{s.n}}</span>
          <span class="step-tool">${{s.tool}}</span>
          <span class="level-tag ${{levelClass}}">${{levelLabel}}</span>
        </div>
        <div class="step-thought">${{s.thought}}</div>
        <div class="step-args">${{argsStr}}</div>
        <div class="spinner" style="padding:8px 14px;font-size:11px;color:var(--muted)">running...</div>`;
    }}
    return div;
  }}

  return div;
}}

async function approveStep(approved, stepNum) {{
  const card = document.getElementById('approval-' + stepNum);
  if (card) {{
    card.style.opacity = '0.5';
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    const label = approved ? 'Approved' : 'Denied';
    const color = approved ? '#4ade80' : '#f87171';
    card.querySelector('.approval-title').innerHTML = `<span style="color:${{color}}">${{label}}</span>`;
  }}
  await fetch('/api/agent/approve', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{session_id: agentSessionId, approved}})
  }});
}}

async function stopAgent() {{
  if (agentSessionId) {{
    await fetch('/api/agent/stop', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{session_id: agentSessionId}})
    }});
  }}
  clearInterval(agentPollTimer);
  resetAgentUI();
}}

function resetAgentUI() {{
  document.getElementById('runAgentBtn').disabled = false;
  document.getElementById('stopAgentBtn').classList.remove('visible');
}}

// ── Agent examples ────────────────────────────────────────────────────────
const AGENT_EXAMPLES = [
  "Check which Ollama models I have, list them with their sizes, and recommend the best one for Blender scripting",
  "Create a custom industrial-design Ollama model from llama3.1:8b — write a Modelfile with expert system prompt and build it",
  "Search the web for the latest Ollama release notes and check if my version is up to date",
  "Check my system specs (CPU, RAM, GPU) and tell me which models will run best on my hardware",
  "List all files in local-ai-production, read the README, and give me a summary of what each file does",
  "Calculate the stress on a steel bracket: 10kg load, 50mm arm, 5x20mm cross section. Show the formula and result",
  "Search for best practices for aluminum 6061 CNC machining tolerances and save a summary to my workspace",
  "Debug the AI studio: check Ollama is running, test a chat request, check disk space, and report any issues",
  "Read my training data in ai-workspace and create an improved Modelfile based on the most common topics",
  "Check if Python, pip, ollama are installed and their versions. Install any that are missing.",
];

function buildAgentExamples() {{
  const grid = document.getElementById('agentExamples');
  if (!grid) return;
  AGENT_EXAMPLES.forEach(ex => {{
    const btn = document.createElement('button');
    btn.className = 'agent-ex-btn';
    btn.textContent = ex;
    btn.onclick = () => {{
      document.getElementById('agentTask').value = ex;
      document.getElementById('agentTask').focus();
    }};
    grid.appendChild(btn);
  }});
}}

// ── Conversation persistence ─────────────────────────────────────────────
function saveHistory() {{
  try {{ localStorage.setItem('chat_history', JSON.stringify(history.slice(-40))); }} catch(e) {{}}
  try {{ localStorage.setItem('chat_messages', document.getElementById('chatArea').innerHTML); }} catch(e) {{}}
}}

function restoreHistory() {{
  try {{
    const h = localStorage.getItem('chat_history');
    if (h) history = JSON.parse(h);
    const msgs = localStorage.getItem('chat_messages');
    if (msgs && msgs.includes('msg-row')) {{
      document.getElementById('chatArea').innerHTML = msgs;
      const welcome = document.getElementById('welcome');
      if (welcome) welcome.remove();
      // Re-bind copy/rate buttons (they still work, event handlers are inline)
    }}
  }} catch(e) {{}}
}}

function clearChat() {{
  history = [];
  try {{ localStorage.removeItem('chat_history'); localStorage.removeItem('chat_messages'); }} catch(e) {{}}
  const chat = document.getElementById('chatArea');
  chat.innerHTML = `<div class="welcome" id="welcome">
    <h2>Chat cleared</h2>
    <p>Start a new conversation below.</p>
    <div class="welcome-chips" id="welcomeChips2"></div>
  </div>`;
  // Re-add chips
  QUICK.forEach(q => {{
    const c = document.createElement('div'); c.className = 'chip';
    c.textContent = q.label;
    c.onclick = () => {{ document.getElementById('input').value = q.prompt; send(); }};
    document.getElementById('welcomeChips2')?.appendChild(c);
  }});
}}

// ── Init ─────────────────────────────────────────────────────────────────
buildSidebar();
buildAgentExamples();
restoreHistory();
window.addEventListener('load', () => {{
  setTimeout(checkStatus, 1500);
  setInterval(checkStatus, 30000);
}});
</script>
</body>
</html>"""

# ─── HTTP HANDLER ─────────────────────────────────────────────────────────────

class StudioHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress access logs

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            html = build_html_cached().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self.end_headers()
            self.wfile.write(html)

        elif self.path == "/api/status":
            models = get_models()
            online = len(models) > 0
            self.send_json({"ollama": online, "models": models, "stats": get_training_stats()})

        elif self.path.startswith("/api/agent/poll"):
            sid = self.path.split("sid=")[-1] if "sid=" in self.path else ""
            session = agent_engine.get_session(sid)
            if not session:
                self.send_json({"error": "Session not found"}, 404)
                return
            self.send_json({
                "status": session.status,
                "steps": session.steps,
                "pending": session.pending
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if self.path == "/api/chat":
            message = body.get("message", "")
            history = body.get("history", [])

            # Check for direct actions first (no LLM needed)
            action = detect_action(message)
            if action:
                result = handle_action(action)
                result["model"] = "direct-action"
                self.send_json(result)
                return

            # Route to best agent
            agent = detect_agent(message)
            available = get_models()
            if not available:
                self.send_json({"error": "No models available. Start Ollama first."}, 503)
                return
            model = pick_model(agent, available)

            # Stream response as SSE
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def sse(data: dict):
                line = "data: " + json.dumps(data) + "\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()

            sse({"type": "start", "model": model, "agent_id": agent["id"],
                 "agent_name": agent["name"], "agent_color": agent["color"]})

            tokens = []
            try:
                for token in ollama_stream(model, agent["system"], history, message):
                    tokens.append(token)
                    sse({"type": "token", "token": token})
            except Exception as e:
                sse({"type": "error", "msg": str(e)})
                return

            full_response = "".join(tokens)
            entry_id = log_conversation(message, full_response, agent["id"], model)
            sse({"type": "done", "entry_id": entry_id})

        elif self.path == "/api/run_blender":
            script = body.get("script", "")
            open_ui = body.get("open_ui", True)
            result = run_blender_script(script, open_ui=open_ui)
            self.send_json(result)

        elif self.path == "/api/blender":
            script = body.get("script", "")
            result = run_blender_script(script, open_ui=True)
            self.send_json(result)

        elif self.path == "/api/agent/start":
            task = body.get("task", "")
            model = body.get("model", "llama3.1:8b")
            auto_approve = body.get("auto_approve", False)
            if not task:
                self.send_json({"error": "No task provided"}, 400)
                return
            sid = agent_engine.start_session(task, model, auto_approve=auto_approve)
            self.send_json({"session_id": sid})

        elif self.path == "/api/agent/approve":
            sid = body.get("session_id", "")
            approved = body.get("approved", False)
            session = agent_engine.get_session(sid)
            if not session:
                self.send_json({"error": "Session not found"}, 404)
                return
            session.approve(approved)
            self.send_json({"ok": True})

        elif self.path == "/api/rate":
            entry_id = body.get("id", "")
            rating = body.get("rating", 0)
            ok = rate_conversation(entry_id, rating)
            stats = get_training_stats()
            # Auto-improve model in background if enough good examples
            threading.Thread(target=maybe_auto_improve_model, daemon=True).start()
            self.send_json({"ok": ok, "stats": stats})

        elif self.path == "/api/agent/stop":
            sid = body.get("session_id", "")
            session = agent_engine.get_session(sid)
            if session:
                session.stop()
            self.send_json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"  Industrial AI Studio")
    print(f"  Starting on http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop\n")

    # Invalidate HTML cache on fresh start
    _html_cache = None

    # Pre-load default model into VRAM
    warmup_model("llama3.1:8b")
    print(f"  Warming up llama3.1:8b in VRAM...")

    with http.server.ThreadingHTTPServer(("", PORT), StudioHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")
