# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
CrimsonWell - Local AI for Everyone
Auto-routing orchestrator with VRAM-aware model switching.

Works on:  AMD (Vulkan)  |  NVIDIA (CUDA)  |  Intel Arc  |  CPU
Requires:  Python 3.9+  |  Ollama (https://ollama.com)

Run:  python crimsonwell.py
      then open  http://localhost:3000
"""

import http.server, json, urllib.request, urllib.error, urllib.parse
import threading, os, subprocess, sys, re, time, glob, socketserver, base64
from datetime import datetime

# Ensure stdout/stderr use UTF-8 on Windows to avoid cp1252 crashes
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── SETUP PATHS ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(os.path.expanduser("~"), ".crimsonwell")
LOGS_DIR  = os.path.join(WORKSPACE, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
sys.path.insert(0, BASE_DIR)

# ─── IMPORT CORE MODULES ──────────────────────────────────────────────────────
try:
    from core.gpu_detect   import get_gpu_info, get_ollama_loaded_vram
    from core.vram_planner import pick_model, get_recommendations, MODEL_CATALOG, model_fits
    from core.intent_router import route_intent, AGENTS, needs_clarification
    from core.usage_learner import record as record_usage, get_last_model, prewarm_top
    _CORE_OK = True
except ImportError as e:
    print(f"[warn] Core module import failed: {e} — using built-in fallbacks")
    _CORE_OK = False

PORT   = 3000
OLLAMA = "http://localhost:11434"

# ─── FALLBACKS IF CORE NOT LOADED ─────────────────────────────────────────────
if not _CORE_OK:
    def get_gpu_info():
        return {"name": "Unknown GPU", "vendor": "unknown", "vram_mb": 0,
                "backend": "cpu", "ollama_env": {}}
    def get_ollama_loaded_vram(): return 0
    def pick_model(models, intent, vram): return models[0] if models else ""
    def get_recommendations(vram, limit=6): return []
    def model_fits(name, vram): return True
    def route_intent(msg):
        return {"intent": "chat", "name": "Chat", "icon": "💬",
                "color": "#6b7280", "confidence": 0.5,
                "system": "You are a helpful AI assistant."}
    def needs_clarification(msg, conf): return False
    def record_usage(intent, model): pass
    def get_last_model(): return ""
    def prewarm_top(): pass
    MODEL_CATALOG = {}
    AGENTS = {"chat": {"name": "Chat", "icon": "💬", "color": "#6b7280"}}

# ─── GPU CACHE (refreshed every 30s) ─────────────────────────────────────────
_gpu_cache      = None
_gpu_cache_time = 0
_gpu_lock       = threading.Lock()

def cached_gpu() -> dict:
    global _gpu_cache, _gpu_cache_time
    with _gpu_lock:
        if time.time() - _gpu_cache_time > 30:
            _gpu_cache = get_gpu_info()
            _gpu_cache_time = time.time()
        return _gpu_cache

# ─── OLLAMA HELPERS ───────────────────────────────────────────────────────────

def ollama_models() -> dict:
    """Returns {'ok': bool, 'models': [...], 'loaded': [...]}"""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            data = json.loads(r.read())
            models = [m["name"] for m in data.get("models", [])]
        try:
            with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=3) as r2:
                ps = json.loads(r2.read())
                loaded = [m["name"] for m in ps.get("models", [])]
        except Exception:
            loaded = []
        return {"ok": True, "models": models, "loaded": loaded}
    except Exception:
        return {"ok": False, "models": [], "loaded": []}

def ollama_pull_bg(model: str):
    """Pull a model in a background thread."""
    def _pull():
        try:
            subprocess.run(["ollama", "pull", model],
                           capture_output=True, timeout=3600)
        except Exception:
            pass
    threading.Thread(target=_pull, daemon=True).start()

# ─── VISION MODEL DETECTION ──────────────────────────────────────────────────

_VISION_KEYS = ["gemma3", "llava", "bakllava", "moondream", "minicpm-v", "cogvlm", "llama3.2-vision"]

def is_vision_capable(model: str) -> bool:
    return any(v in model.lower() for v in _VISION_KEYS)

# ─── FILE CONTEXT BUILDER ────────────────────────────────────────────────────

def build_file_context(files: list, model: str) -> tuple:
    """Returns (text_context: str, image_b64_list: list)."""
    text_ctx, images = [], []
    for f in (files or []):
        name  = f.get("name", "file")
        ftype = f.get("type", "")
        data  = f.get("data", "")
        if "," in data:
            data = data.split(",", 1)[1]
        try:
            raw = base64.b64decode(data)
        except Exception:
            continue
        if ftype.startswith("image/"):
            if is_vision_capable(model):
                images.append(data)
            else:
                text_ctx.append(f"[Image attached: {name} — switch to a vision model like gemma3 to analyze]")
        else:
            try:
                text = raw.decode("utf-8", errors="replace")[:6000]
                ext = name.rsplit(".", 1)[-1] if "." in name else "txt"
                text_ctx.append(f"[File: {name}]\n```{ext}\n{text}\n```")
            except Exception:
                text_ctx.append(f"[Binary file: {name}]")
    return "\n\n".join(text_ctx), images

# ─── SAFE TOOL EXECUTOR ──────────────────────────────────────────────────────

_BLOCKED_CMD_PATTERNS = [
    "rm -rf", "del /f /s", "format c:", "shutdown /", "reboot",
    "rd /s /q", "reg delete", "bcdedit", "diskpart", "mkfs",
    "dd if=", ":(){:|:&};:", "wget -O /dev/", "curl -o /dev/"
]

def execute_tool(tool_name: str, args: dict) -> str:
    """Execute a sandboxed agent tool. Returns result string."""
    try:
        if tool_name == "read_file":
            path = os.path.abspath(os.path.expanduser(args.get("path", "")))
            safe_roots = [WORKSPACE, BASE_DIR, os.path.expanduser("~/Desktop"),
                          os.path.expanduser("~/Documents")]
            if not any(path.startswith(r) for r in safe_roots):
                return f"[BLOCKED] Path outside safe zone: {path}"
            if not os.path.isfile(path):
                return f"[ERROR] File not found: {path}"
            with open(path, "r", errors="replace") as fp:
                return fp.read(8000)

        elif tool_name == "write_file":
            path = args.get("path", "")
            if not os.path.isabs(path):
                path = os.path.join(WORKSPACE, path)
            path = os.path.abspath(path)
            if not path.startswith(WORKSPACE):
                return f"[BLOCKED] Can only write inside workspace: {WORKSPACE}"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(args.get("content", ""))
            return f"[OK] Written: {path}"

        elif tool_name == "list_dir":
            path = os.path.abspath(os.path.expanduser(args.get("path", WORKSPACE)))
            if not os.path.isdir(path):
                return f"[ERROR] Not a directory: {path}"
            items = os.listdir(path)[:60]
            lines = []
            for item in items:
                full = os.path.join(path, item)
                tag = "[D]" if os.path.isdir(full) else "[F]"
                lines.append(f"{tag} {item}")
            return "\n".join(lines) or "(empty)"

        elif tool_name == "run_python":
            code = args.get("code", "").strip()
            script = os.path.join(WORKSPACE, "_agent_run.py")
            with open(script, "w", encoding="utf-8") as fp:
                fp.write(code)
            r = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=30, cwd=WORKSPACE
            )
            out = (r.stdout + r.stderr).strip()
            return out[:3000] if out else "[OK] Script completed (no output)"

        elif tool_name == "run_cmd":
            cmd = args.get("cmd", "").strip()
            low = cmd.lower()
            for pat in _BLOCKED_CMD_PATTERNS:
                if pat in low:
                    return f"[BLOCKED] Dangerous pattern detected: {pat}"
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=WORKSPACE
            )
            out = (r.stdout + r.stderr).strip()
            return out[:3000] if out else "[OK]"

        elif tool_name == "open_url":
            url = args.get("url", "")
            if url.startswith(("http://", "https://")):
                import webbrowser
                webbrowser.open(url)
                return f"[OK] Opened: {url}"
            return "[ERROR] Only http/https URLs allowed"

        elif tool_name == "search_file":
            pattern = args.get("pattern", "")
            path = os.path.abspath(os.path.expanduser(args.get("path", WORKSPACE)))
            results = []
            for f in glob.glob(os.path.join(path, "**", pattern), recursive=True)[:20]:
                results.append(f)
            return "\n".join(results) or "(no matches)"

        else:
            return f"[ERROR] Unknown tool: {tool_name}"

    except subprocess.TimeoutExpired:
        return "[ERROR] Tool timed out"
    except Exception as e:
        return f"[ERROR] {e}"

# ─── AGENT LOOP ───────────────────────────────────────────────────────────────

_AGENT_SYSTEM = """You are an autonomous AI agent running on the user's PC. You complete tasks step-by-step using tools.

To use a tool, output EXACTLY this format (nothing else on those lines):
<use_tool>
{{"tool": "TOOL_NAME", "args": {{...}}}}
</use_tool>

Available tools:
- read_file: {{"path": "..."}} — Read file (safe paths only)
- write_file: {{"path": "filename.py", "content": "..."}} — Write file to workspace
- list_dir: {{"path": "..."}} — List directory contents
- run_python: {{"code": "import os\\nprint(os.getcwd())"}} — Execute Python code
- run_cmd: {{"cmd": "dir"}} — Run shell command (safe commands only)
- open_url: {{"url": "https://..."}} — Open URL in browser
- search_file: {{"path": "...", "pattern": "*.py"}} — Find files by pattern

Workspace: {workspace}

Rules:
1. Think step by step before acting
2. Use tools to gather info before writing code
3. Always verify file writes with read_file
4. Report what you accomplished when done
5. If a command fails, try an alternative approach"""

_TOOL_RE = re.compile(r'<use_tool>\s*(.*?)\s*</use_tool>', re.DOTALL)

def _agent_loop(task: str, history: list, model: str, images: list = None):
    """Autonomous ReAct loop — yields SSE chunks."""
    system = _AGENT_SYSTEM.format(workspace=WORKSPACE)
    messages = [{"role": "system", "content": system}]
    for h in history[-4:]:
        messages.append({"role": h["role"], "content": h["content"]})
    user_msg: dict = {"role": "user", "content": task}
    if images:
        user_msg["images"] = images
    messages.append(user_msg)

    for step in range(10):
        payload = json.dumps({
            "model": model, "messages": messages, "stream": True,
            "options": {"temperature": 0.3, "num_ctx": 8192}
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA}/api/chat", data=payload,
            headers={"Content-Type": "application/json"}
        )
        full_response = []
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                for raw_line in r:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        chunk = json.loads(raw_line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            full_response.append(token)
                            safe = token.replace("\n", "\\n")
                            yield f"data: {safe}\n\n"
                        if chunk.get("done"):
                            break
                    except Exception:
                        pass
        except Exception as e:
            yield f"data: \\n\\n**[Agent error: {e}]**\\n\n\n"
            break

        full_text = "".join(full_response)
        messages.append({"role": "assistant", "content": full_text})

        # Check for tool calls
        tool_calls = _TOOL_RE.findall(full_text)
        if not tool_calls:
            break  # No tools = agent is done

        tool_results = []
        for call_json in tool_calls:
            try:
                call = json.loads(call_json.strip())
                t_name = call.get("tool", "")
                t_args = call.get("args", {})
            except Exception as parse_err:
                tool_results.append(f"[parse error: {parse_err}]")
                continue

            result = execute_tool(t_name, t_args)
            short = result[:120].replace("\n", " ")
            yield f"data: \\n\\n> **[{t_name}]** {short}{'...' if len(result)>120 else ''}\\n\n\n"
            tool_results.append(f"<result tool='{t_name}'>\n{result[:2000]}\n</result>")

        messages.append({"role": "user", "content": "\n".join(tool_results)})

    record_usage("agent", model)
    yield "data: [DONE]\n\n"

# ─── SKILL SCANNER ───────────────────────────────────────────────────────────

def scan_skills() -> list:
    """Auto-discover .py files in the skills/ folder."""
    skills_dir = os.path.join(BASE_DIR, "skills")
    found = []
    if os.path.isdir(skills_dir):
        for f in glob.glob(os.path.join(skills_dir, "*.py")):
            name = os.path.splitext(os.path.basename(f))[0]
            if name.startswith("_"):
                continue
            found.append(name)
    return found

# ─── STATUS API ───────────────────────────────────────────────────────────────

def build_status() -> dict:
    gpu   = cached_gpu()
    ol    = ollama_models()
    vused = get_ollama_loaded_vram()
    rec   = get_recommendations(gpu["vram_mb"]) if ol["ok"] else []

    return {
        "gpu_name":    gpu["name"],
        "gpu_vendor":  gpu["vendor"],
        "vram_total":  gpu["vram_mb"],
        "vram_used":   vused,
        "backend":     gpu["backend"],
        "ollama_ok":   ol["ok"],
        "models":      ol["models"],
        "loaded_models": ol["loaded"],
        "recommended": [{"name": r["name"], "vram": r["vram_mb"], "desc": r["desc"]}
                        for r in rec],
        "skills":      scan_skills(),
        "agents":      [{"id": k, "name": v["name"], "icon": v["icon"], "color": v["color"]}
                        for k, v in AGENTS.items()],
    }

# ─── STREAMING CHAT ───────────────────────────────────────────────────────────

def stream_chat(message: str, history: list, model_override: str | None, files: list = None):
    """
    Generator that yields SSE lines.
    First yields a meta JSON (intent, model, agent info).
    Then streams tokens from Ollama (or runs agent loop for agent intent).
    """
    ol = ollama_models()
    gpu = cached_gpu()

    # Route intent
    route = route_intent(message)
    intent = route["intent"]

    # Pick model
    if model_override and model_override in ol["models"]:
        model = model_override
    elif ol["models"]:
        model = pick_model(ol["models"], intent, gpu["vram_mb"])
    else:
        yield 'data: {"type":"error","msg":"No models installed. Run SETUP.bat or: ollama pull llama3.2:3b"}\n\n'
        yield "data: [DONE]\n\n"
        return

    # Check VRAM fit
    if not model_fits(model, gpu["vram_mb"]):
        yield f'data: {{"type":"warn","msg":"Model {model} may be too large for your GPU VRAM."}}\n\n'

    # Send meta so the UI can update the agent badge immediately
    meta = {
        "type": "meta",
        "intent": intent,
        "model": model,
        "agent": route["name"],
        "icon":  route["icon"],
        "color": route["color"],
    }
    yield f"data: {json.dumps(meta)}\n\n"

    # Build file context
    file_text, image_data = build_file_context(files or [], model)
    full_message = (file_text + "\n\n" + message).strip() if file_text else message

    # Agent intent → run autonomous loop
    if intent == "agent":
        yield from _agent_loop(full_message, history, model, image_data or None)
        return

    # Build messages
    system_prompt = route.get("system", "You are a helpful assistant.")
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-8:]:  # last 8 exchanges for context
        messages.append({"role": h["role"], "content": h["content"]})

    user_msg: dict = {"role": "user", "content": full_message}
    if image_data:
        user_msg["images"] = image_data
    messages.append(user_msg)

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.7, "num_ctx": 4096},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}
    )

    full_response = []
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw_line in r:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full_response.append(token)
                        # Escape for SSE: replace newlines in token
                        safe = token.replace("\n", "\\n")
                        yield f"data: {safe}\n\n"
                    if chunk.get("done"):
                        break
                except Exception:
                    pass
    except urllib.error.URLError as e:
        yield f'data: [Error: Cannot reach Ollama — {e}]\n\n'
    except Exception as e:
        yield f'data: [Error: {e}]\n\n'

    # Log usage
    record_usage(intent, model)
    yield "data: [DONE]\n\n"

# ─── HTTP SERVER ──────────────────────────────────────────────────────────────

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default access logs

    def handle_error(self):
        pass  # swallow connection-reset noise printed by socketserver

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            pass

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif self.path == "/api/status":
                self._json(build_status())

            elif self.path == "/api/models":
                self._json(ollama_models())

            else:
                self.send_response(404)
                self.end_headers()
        except _CLIENT_GONE:
            pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(length)
        try:
            body = json.loads(body_raw) if body_raw else {}
        except Exception:
            body = {}

        if self.path == "/api/chat":
            message  = body.get("message", "").strip()
            history  = body.get("history", [])
            model_ov = body.get("model") or None
            files    = body.get("files") or []

            if not message and not files:
                self._json({"error": "empty message"}, 400)
                return
            if not message and files:
                message = "Please analyze the attached file(s)."

            self._sse_headers()
            try:
                for chunk in stream_chat(message, history, model_ov, files):
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif self.path == "/api/pull":
            model = body.get("model", "").strip()
            if not model:
                self._json({"ok": False, "error": "no model specified"}, 400)
                return
            ollama_pull_bg(model)
            self._json({"ok": True, "msg": f"Downloading {model} in background..."})

        elif self.path == "/api/agent":
            # Kick off an autonomous agent task (uses agent_engine if available)
            task  = body.get("task", "").strip()
            model = body.get("model", "llama3.1:8b")
            try:
                import agent_engine
                sid = agent_engine.start_session(task, model)
                self._json({"ok": True, "session_id": sid})
            except ImportError:
                self._json({"ok": False, "error": "agent_engine not available"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()

# ─── HTML UI (embedded) ───────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CrimsonWell</title>
<style>
:root {
  --c:    #dc2626;
  --cd:   #991b1b;
  --cg:   rgba(220,38,38,.12);
  --bg:   #0d0d0d;
  --s1:   #161616;
  --s2:   #1f1f1f;
  --br:   #2a2a2a;
  --tx:   #e5e5e5;
  --mu:   #6b7280;
  --gr:   #22c55e;
  --yl:   #eab308;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);height:100vh;overflow:hidden;display:flex;flex-direction:column;font-size:14px}

/* HEADER */
.hdr{display:flex;align-items:center;gap:12px;padding:8px 14px;background:var(--s1);border-bottom:1px solid var(--br);flex-shrink:0;min-height:44px}
.logo{font-weight:700;font-size:17px;color:var(--c);letter-spacing:-.5px;white-space:nowrap}
.logo sub{font-size:10px;color:var(--mu);font-weight:400;vertical-align:middle}
.badge{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--mu);background:var(--s2);padding:3px 8px;border-radius:5px;border:1px solid var(--br);white-space:nowrap}
.dot{width:6px;height:6px;border-radius:50%;background:var(--br);flex-shrink:0}
.vbar-wrap{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--mu)}
.vbar{width:70px;height:5px;background:var(--s2);border-radius:3px;overflow:hidden;border:1px solid var(--br)}
.vfill{height:100%;background:var(--c);border-radius:3px;transition:width .5s,background .5s}
.agent-badge{margin-left:auto;display:flex;align-items:center;gap:5px;font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid var(--c);color:var(--c);background:var(--cg);transition:all .3s;white-space:nowrap}
.model-tag{font-size:10px;color:var(--mu);border-left:1px solid var(--br);padding-left:6px;margin-left:2px}

/* LAYOUT */
.main{display:flex;flex:1;overflow:hidden}

/* SIDEBAR */
.sb{width:190px;flex-shrink:0;background:var(--s1);border-right:1px solid var(--br);overflow-y:auto;padding:10px 0}
.sb-sec{padding:0 10px 10px}
.sb-title{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--mu);margin-bottom:5px}
.m-item{display:flex;align-items:center;gap:5px;padding:4px 5px;border-radius:4px;cursor:pointer;font-size:11px;color:var(--mu);transition:all .15s;user-select:none}
.m-item:hover{background:var(--s2);color:var(--tx)}
.m-item.active{color:var(--c);background:var(--cg)}
.m-item.loaded .dot{background:var(--gr)}
.m-vram{font-size:9px;margin-left:auto;color:var(--mu)}
.pull-btn{font-size:9px;padding:1px 5px;background:var(--s2);border:1px solid var(--br);border-radius:3px;color:var(--mu);cursor:pointer;margin-left:auto;transition:all .15s}
.pull-btn:hover{border-color:var(--c);color:var(--c)}
.sk-item{display:flex;align-items:center;gap:5px;padding:3px 5px;font-size:11px;color:var(--mu);border-radius:4px;transition:color .15s}
.sk-item.active-sk{color:var(--tx)}

/* CHAT */
.chat{flex:1;display:flex;flex-direction:column;overflow:hidden}
#alerts{flex-shrink:0}
.alert{margin:6px 12px;padding:7px 10px;border-radius:5px;font-size:11px;border:1px solid var(--br);display:flex;align-items:center;gap:6px}
.alert.warn{border-color:var(--yl);color:var(--yl);background:rgba(234,179,8,.08)}
.alert.info{border-color:var(--c);color:var(--mu);background:var(--cg)}
.msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:14px}
.msg{display:flex;gap:9px;max-width:860px;animation:fadein .2s ease}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.msg.user{flex-direction:row-reverse;margin-left:auto}
.avatar{width:26px;height:26px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0;margin-top:2px}
.msg.user .avatar{background:var(--c)}
.msg.ai .avatar{background:var(--s2);border:1px solid var(--br)}
.bubble{background:var(--s1);border:1px solid var(--br);border-radius:8px;padding:9px 13px;font-size:13px;line-height:1.65;max-width:680px;word-break:break-word}
.msg.user .bubble{background:var(--cg);border-color:var(--c)}
.meta-line{font-size:9px;color:var(--mu);margin-top:3px;padding:0 2px}
.bubble pre{background:#090909;border:1px solid var(--br);border-radius:5px;padding:9px;overflow-x:auto;margin:7px 0;font-size:11px;font-family:'Consolas','Courier New',monospace}
.bubble code{font-family:'Consolas','Courier New',monospace;font-size:11px;background:#111;padding:1px 4px;border-radius:3px}
.bubble pre code{background:none;padding:0}
.bubble strong{color:var(--tx)}
.bubble h1,.bubble h2,.bubble h3{font-size:13px;margin:8px 0 4px;color:var(--tx)}
.bubble ul,.bubble ol{padding-left:18px;margin:4px 0}
.bubble li{margin:2px 0}
.bubble a{color:var(--c);text-decoration:none}
.bubble a:hover{text-decoration:underline}
.typing span{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--c);margin:0 2px;animation:bounce 1s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-5px)}}

/* QUICK ACTIONS */
.qa{padding:6px 12px;display:flex;gap:5px;flex-wrap:wrap;border-top:1px solid var(--br);flex-shrink:0}
.q-btn{font-size:11px;padding:3px 9px;background:var(--s2);border:1px solid var(--br);border-radius:10px;color:var(--mu);cursor:pointer;transition:all .15s;white-space:nowrap}
.q-btn:hover{border-color:var(--c);color:var(--c);background:var(--cg)}

/* INPUT */
.inp-area{display:flex;flex-direction:column;gap:5px;padding:8px 12px 10px;background:var(--s1);border-top:1px solid var(--br);flex-shrink:0}
.inp-row{display:flex;gap:7px;align-items:flex-end}
.inp-wrap{flex:1;position:relative}
#inp{width:100%;background:var(--s2);border:1px solid var(--br);border-radius:7px;color:var(--tx);font-size:13px;padding:9px 12px;outline:none;resize:none;min-height:40px;max-height:110px;font-family:inherit;transition:border-color .15s;line-height:1.5}
#inp:focus{border-color:var(--c)}
#inp::placeholder{color:var(--mu)}
.send{background:var(--c);color:#fff;border:none;border-radius:7px;padding:9px 15px;cursor:pointer;font-size:13px;font-weight:600;transition:background .15s;white-space:nowrap}
.send:hover{background:var(--cd)}
.send:disabled{opacity:.35;cursor:not-allowed}
.attach-btn{background:var(--s2);color:var(--mu);border:1px solid var(--br);border-radius:7px;padding:9px 11px;cursor:pointer;font-size:14px;transition:all .15s;white-space:nowrap}
.attach-btn:hover{border-color:var(--c);color:var(--c)}
.file-chips{display:flex;flex-wrap:wrap;gap:5px;padding:0 2px}
.chip{display:flex;align-items:center;gap:4px;background:var(--s2);border:1px solid var(--br);border-radius:4px;padding:2px 7px;font-size:11px;color:var(--mu)}
.chip .rm{cursor:pointer;color:var(--mu);margin-left:2px;opacity:.7}
.chip .rm:hover{color:#ef4444;opacity:1}
.chip.img{border-color:#3b82f6;color:#3b82f6}

/* SCROLLBAR */
::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-thumb{background:var(--br);border-radius:2px}

@media(max-width:580px){.sb{display:none}.model-tag{display:none}}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">CrimsonWell <sub>local AI</sub></div>
  <div class="badge" id="gpu-badge">
    <div class="dot" id="gpu-dot"></div>
    <span id="gpu-name">Detecting...</span>
  </div>
  <div class="vbar-wrap">
    <div class="vbar"><div class="vfill" id="vfill" style="width:0%"></div></div>
    <span id="vram-txt">–</span>
  </div>
  <div class="agent-badge" id="agent-badge">
    <span id="agent-icon">💬</span>
    <span id="agent-name">Chat</span>
    <span class="model-tag" id="model-tag"></span>
  </div>
</div>

<div class="main">
  <div class="sb">
    <div class="sb-sec">
      <div class="sb-title">Models</div>
      <div id="model-list"></div>
    </div>
    <div class="sb-sec" id="rec-sec" style="display:none">
      <div class="sb-title">Fits Your GPU</div>
      <div id="rec-list"></div>
    </div>
    <div class="sb-sec">
      <div class="sb-title">Agents</div>
      <div id="agent-list"></div>
    </div>
  </div>

  <div class="chat">
    <div id="alerts"></div>
    <div id="msgs" class="msgs"></div>
    <div class="qa" id="qa"></div>
    <div class="inp-area">
      <div id="file-chips" class="file-chips"></div>
      <div class="inp-row">
        <button class="attach-btn" title="Attach file" onclick="document.getElementById('file-input').click()">+ File</button>
        <input type="file" id="file-input" multiple style="display:none" onchange="handleFiles(this.files)">
        <div class="inp-wrap">
          <textarea id="inp" placeholder="Ask anything — attach files, run agent tasks, code, design..." rows="1"></textarea>
        </div>
        <button class="send" id="send-btn" onclick="send()">Send ▶</button>
      </div>
    </div>
  </div>
</div>

<script>
// ─── STATE ───────────────────────────────────────────────────────────────────
let history = [];
let currentModel = '';
let currentIntent = 'chat';
let streaming = false;
let status = {};
let attachedFiles = [];  // [{name, type, data (base64 dataURL)}]

const QUICK = [
  {l:'💻 Code', p:'Write a Python script that '},
  {l:'🧊 Blender', p:'Write a Blender Python script to create '},
  {l:'🔬 Research', p:'Research and summarize: '},
  {l:'✍️ Write', p:'Write a professional '},
  {l:'🧮 Math', p:'Calculate: '},
  {l:'🤖 Agent task', p:'Do this for me: '},
];

// ─── STATUS ──────────────────────────────────────────────────────────────────
async function updateStatus() {
  try {
    const r = await fetch('/api/status');
    status = await r.json();

    // GPU badge
    document.getElementById('gpu-name').textContent = status.gpu_name || 'CPU mode';
    document.getElementById('gpu-dot').style.background = status.ollama_ok ? '#22c55e' : '#dc2626';

    // VRAM bar
    if (status.vram_total > 0) {
      const pct = Math.round(status.vram_used / status.vram_total * 100);
      const fill = document.getElementById('vfill');
      fill.style.width = pct + '%';
      fill.style.background = pct > 85 ? '#ef4444' : pct > 65 ? '#eab308' : '#dc2626';
      document.getElementById('vram-txt').textContent =
        (status.vram_used/1024).toFixed(1) + '/' + (status.vram_total/1024).toFixed(0) + 'GB';
    } else {
      document.getElementById('vram-txt').textContent = status.gpu_vendor !== 'unknown' ? 'VRAM' : 'CPU';
    }

    renderModels(status.models||[], status.loaded_models||[], status.recommended||[]);
    renderAgents(status.agents||[]);

    // Alerts
    const al = document.getElementById('alerts');
    if (!status.ollama_ok) {
      al.innerHTML = '<div class="alert warn">⚠️ Ollama is not running. Start it with LAUNCH.bat or run: <code>ollama serve</code></div>';
    } else if ((status.models||[]).length === 0) {
      al.innerHTML = '<div class="alert info">👋 No models installed yet. Click a model below to download one for your GPU.</div>';
    } else {
      al.innerHTML = '';
    }
  } catch(e) {}
}

// ─── RENDER MODELS ───────────────────────────────────────────────────────────
function renderModels(models, loaded, recommended) {
  const list = document.getElementById('model-list');
  list.innerHTML = '';

  if (models.length === 0 && recommended.length > 0) {
    const rs = document.getElementById('rec-sec');
    rs.style.display = 'block';
    const rl = document.getElementById('rec-list');
    rl.innerHTML = '';
    recommended.slice(0, 5).forEach(m => {
      const d = document.createElement('div');
      d.className = 'm-item';
      const gb = (m.vram/1024).toFixed(1);
      d.innerHTML = `<div class="dot"></div><span>${m.name.split(':')[0]}</span>
        <button class="pull-btn" onclick="pullModel('${m.name}')">↓${gb}GB</button>`;
      rl.appendChild(d);
    });
  } else {
    document.getElementById('rec-sec').style.display = 'none';
  }

  models.forEach(m => {
    const isLoaded = loaded.includes(m);
    const isActive = m === currentModel;
    const d = document.createElement('div');
    d.className = 'm-item' + (isActive?' active':'') + (isLoaded?' loaded':'');
    const short = m.replace(/:latest$/,'');
    d.innerHTML = `<div class="dot"></div><span>${short}</span>`;
    if (isLoaded) d.innerHTML += '<span class="m-vram" style="color:#22c55e">●</span>';
    d.onclick = () => {
      currentModel = m;
      renderModels(models, loaded, recommended);
    };
    list.appendChild(d);
  });
}

// ─── RENDER AGENTS ───────────────────────────────────────────────────────────
function renderAgents(agents) {
  const list = document.getElementById('agent-list');
  list.innerHTML = '';
  agents.forEach(a => {
    const d = document.createElement('div');
    const active = a.id === currentIntent;
    d.className = 'sk-item' + (active?' active-sk':'');
    d.style.color = active ? a.color : '';
    d.innerHTML = `<div class="dot" style="background:${active?a.color:'var(--br)'}"></div>${a.icon} ${a.name}`;
    list.appendChild(d);
  });
}

// ─── INTENT PREVIEW (live as user types) ────────────────────────────────────
function previewIntent(text) {
  if (!text.trim()) return;
  const t = text.toLowerCase();
  let intent = 'chat', icon = '💬', name = 'Chat', color = '#6b7280';
  if (/\b(code|script|python|javascript|function|debug|import|def |class |api|program)\b/.test(t))
    { intent='code'; icon='💻'; name='Coder'; color='#3b82f6'; }
  else if (/\b(blender|3d|mesh|bpy|geometry|render|animate|low.?poly|gltf|stl)\b/.test(t))
    { intent='3d'; icon='🧊'; name='3D'; color='#f97316'; }
  else if (/\b(research|analyze|explain|compare|summarize|what is|how does)\b/.test(t))
    { intent='research'; icon='🔬'; name='Research'; color='#8b5cf6'; }
  else if (/\b(calculate|math|equation|formula|solve|compute|derivative|integral)\b/.test(t))
    { intent='math'; icon='🧮'; name='Math'; color='#06b6d4'; }
  else if (/\b(write|essay|email|article|blog|document|report|draft|proofread)\b/.test(t))
    { intent='write'; icon='✍️'; name='Writer'; color='#10b981'; }
  else if (/\b(do |execute|run |install|download|automate|create file|open )\b/.test(t))
    { intent='agent'; icon='🤖'; name='Agent'; color='#ec4899'; }

  currentIntent = intent;
  const badge = document.getElementById('agent-badge');
  badge.style.borderColor = color; badge.style.color = color;
  badge.style.background = color.replace(')', ',0.12)').replace('rgb', 'rgba');
  document.getElementById('agent-icon').textContent = icon;
  document.getElementById('agent-name').textContent = name;
}

// ─── FORMAT OUTPUT ───────────────────────────────────────────────────────────
function fmt(text) {
  // Code blocks first
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    `<pre><code>${esc(code.trim())}</code></pre>`);
  // Inline code
  text = text.replace(/`([^`\n]+)`/g, (_, c) => `<code>${esc(c)}</code>`);
  // Bold/italic
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Headings
  text = text.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  text = text.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  text = text.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // Lists
  text = text.replace(/^[*\-] (.+)$/gm, '<li>$1</li>');
  text = text.replace(/(<li>.*<\/li>\n?)+/g, s => `<ul>${s}</ul>`);
  // Newlines → br (but not inside pre)
  text = text.replace(/(?<![>])\n(?!<)/g, '<br>');
  return text;
}
function esc(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ─── MESSAGES ────────────────────────────────────────────────────────────────
function addMsg(role, html, meta) {
  const msgs = document.getElementById('msgs');
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const avatar = role === 'user' ? '👤' : (document.getElementById('agent-icon').textContent || '🤖');
  wrap.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div>
      <div class="bubble" id="b${meta?.id||Date.now()}">${html}</div>
      ${meta?.label ? `<div class="meta-line">${meta.label}</div>` : ''}
    </div>`;
  msgs.appendChild(wrap);
  msgs.scrollTop = msgs.scrollHeight;
  return wrap;
}

// ─── FILE HANDLING ────────────────────────────────────────────────────────────
function handleFiles(fileList) {
  const remaining = 5 - attachedFiles.length;
  const toAdd = Array.from(fileList).slice(0, remaining);
  toAdd.forEach(file => {
    const reader = new FileReader();
    reader.onload = e => {
      attachedFiles.push({name: file.name, type: file.type, data: e.target.result});
      renderChips();
    };
    reader.readAsDataURL(file);
  });
  document.getElementById('file-input').value = '';
}

function renderChips() {
  const el = document.getElementById('file-chips');
  el.innerHTML = '';
  attachedFiles.forEach((f, i) => {
    const isImg = f.type.startsWith('image/');
    const chip = document.createElement('div');
    chip.className = 'chip' + (isImg ? ' img' : '');
    chip.innerHTML = `${isImg ? '🖼' : '📄'} ${esc(f.name)} <span class="rm" onclick="removeFile(${i})">✕</span>`;
    el.appendChild(chip);
  });
}

function removeFile(idx) {
  attachedFiles.splice(idx, 1);
  renderChips();
}

// drag & drop — works anywhere on the page
document.body.addEventListener('dragover', e => {
  e.preventDefault();
  document.getElementById('inp').style.borderColor = 'var(--c)';
});
document.body.addEventListener('dragleave', e => {
  if (!e.relatedTarget) document.getElementById('inp').style.borderColor = '';
});
document.body.addEventListener('drop', e => {
  e.preventDefault();
  document.getElementById('inp').style.borderColor = '';
  if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
});

// ─── SEND ─────────────────────────────────────────────────────────────────────
async function send() {
  if (streaming) return;
  const inp = document.getElementById('inp');
  const text = inp.value.trim();
  if (!text && attachedFiles.length === 0) return;

  const filesToSend = [...attachedFiles];
  attachedFiles = [];
  renderChips();

  inp.value = '';
  resize(inp);
  streaming = true;
  document.getElementById('send-btn').disabled = true;

  // Show user message (with file indicators)
  let userHtml = text ? esc(text) : '';
  if (filesToSend.length) {
    const fnames = filesToSend.map(f => {
      const isImg = f.type.startsWith('image/');
      return `<span class="chip ${isImg?'img':''}" style="display:inline-flex">${isImg?'🖼':'📄'} ${esc(f.name)}</span>`;
    }).join(' ');
    userHtml = (userHtml ? userHtml + '<br>' : '') + fnames;
  }
  addMsg('user', userHtml || '(files attached)');
  history.push({role:'user', content: text || '(files attached)'});

  const mid = 'b' + Date.now();
  const aiWrap = addMsg('ai', '<div class="typing"><span></span><span></span><span></span></div>', {id: mid});
  const bubble = aiWrap.querySelector('.bubble');

  let full = '';
  let metaLabel = '';

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        message: text || 'Please analyze the attached file(s).',
        history: history.slice(-10),
        model: currentModel||null,
        files: filesToSend
      })
    });

    if (!resp.ok) throw new Error('Server error ' + resp.status);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    outer: while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n');
      buf = lines.pop(); // keep incomplete last line
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trimEnd();
        if (data === '[DONE]') break outer;
        if (data.startsWith('{')) {
          try {
            const info = JSON.parse(data);
            if (info.type === 'meta') {
              currentIntent = info.intent||'chat';
              currentModel  = info.model||currentModel;
              const badge = document.getElementById('agent-badge');
              badge.style.borderColor = info.color; badge.style.color = info.color;
              document.getElementById('agent-icon').textContent = info.icon;
              document.getElementById('agent-name').textContent = info.agent;
              document.getElementById('model-tag').textContent = info.model||'';
              metaLabel = `${info.model} · ${info.agent}`;
              continue;
            }
          } catch(e) {}
        }
        // Unescape newlines
        const token = data.replace(/\\n/g, '\n');
        full += token;
        bubble.innerHTML = fmt(full) + '<span class="typing" style="display:inline"><span></span></span>';
        document.getElementById('msgs').scrollTop = 99999;
      }
    }

    bubble.innerHTML = fmt(full);
    // Add meta line
    if (metaLabel) {
      const ml = aiWrap.querySelector('.meta-line') || document.createElement('div');
      ml.className = 'meta-line';
      ml.textContent = metaLabel;
      if (!aiWrap.querySelector('.meta-line')) aiWrap.children[1].appendChild(ml);
    }
    history.push({role:'assistant', content:full});

  } catch(e) {
    bubble.innerHTML = `<span style="color:#ef4444">⚠ ${esc(e.message)}</span>`;
  }

  streaming = false;
  document.getElementById('send-btn').disabled = false;
  document.getElementById('msgs').scrollTop = 99999;
}

// ─── PULL MODEL ───────────────────────────────────────────────────────────────
async function pullModel(name) {
  const r = await fetch('/api/pull', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model:name})
  });
  const d = await r.json();
  addMsg('ai', fmt(`Downloading **${name}**...  \nThis runs in the background and may take a few minutes depending on your internet speed. The model will appear in the sidebar when ready.`));
}

// ─── QUICK ACTIONS ────────────────────────────────────────────────────────────
function renderQA() {
  const qa = document.getElementById('qa');
  QUICK.forEach(q => {
    const b = document.createElement('button');
    b.className = 'q-btn'; b.textContent = q.l;
    b.onclick = () => {
      const inp = document.getElementById('inp');
      inp.value = q.p; inp.focus(); resize(inp);
      previewIntent(q.p);
    };
    qa.appendChild(b);
  });
}

// ─── INPUT AUTO-RESIZE ────────────────────────────────────────────────────────
function resize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 110) + 'px';
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
document.getElementById('inp').addEventListener('input', function() {
  resize(this); previewIntent(this.value);
});
document.getElementById('inp').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

renderQA();
updateStatus();
setInterval(updateStatus, 5000);
</script>
</body>
</html>"""

# ─── STARTUP ──────────────────────────────────────────────────────────────────

def start():
    # Pre-warm the most used model in background
    try:
        prewarm_top()
    except Exception:
        pass

    # Apply GPU-specific Ollama env hints
    try:
        gpu = cached_gpu()
        for k, v in gpu.get("ollama_env", {}).items():
            os.environ.setdefault(k, v)
    except Exception:
        pass

    # Use ASCII-safe banner (avoids cp1252 encoding issues on Windows)
    sep = "  " + "=" * 39
    print(sep)
    print("  CrimsonWell - Local AI Stack")
    print(sep)

    try:
        gpu = cached_gpu()
        print(f"  GPU   : {gpu['name'][:40]}")
        vram_str = f"{gpu['vram_mb']//1024}GB" if gpu['vram_mb'] else "unknown"
        print(f"  VRAM  : {vram_str} ({gpu['backend']})")
    except Exception:
        print("  GPU   : detection failed")

    ol = ollama_models()
    model_count = len(ol['models'])
    status_str = f"{model_count} models" if ol['ok'] else "NOT RUNNING"
    print(f"  Ollama: {status_str}")
    print(sep)
    print(f"  http://localhost:{PORT}")
    print(sep)
    print()

    if not ol['ok']:
        print("  [!] Ollama not detected. Run LAUNCH.bat or: ollama serve")
        print()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  CrimsonWell stopped.")


if __name__ == "__main__":
    start()
