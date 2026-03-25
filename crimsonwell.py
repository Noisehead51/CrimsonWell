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
    from core.update_engine import (
        discover_models, benchmark_model, compare_models, safe_swap_model,
        discover_skills, validate_skill, get_update_status
    )
    from core.model_selector import select_model, set_speed_preference
    _CORE_OK = True
except ImportError as e:
    print(f"[warn] Core module import failed: {e} — using built-in fallbacks")
    _CORE_OK = False
    def discover_models(limit=20): return []
    def benchmark_model(name, prompt=None): return {}
    def compare_models(old, new, prompt=None): return {}
    def safe_swap_model(old, new, intent): return {"ok": False, "error": "update_engine not loaded"}
    def discover_skills(): return []
    def validate_skill(path): return {"ok": False}
    def get_update_status(): return {}

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

# ─── SESSION STATE ───────────────────────────────────────────────────────────

_session = {
    "cwd": os.path.expanduser("~/Desktop"),
}

# ─── TOOL EXECUTOR ───────────────────────────────────────────────────────────

_BLOCKED_CMD = [
    "rm -rf /", "rm -rf ~", "del /f /s /q c:\\", "format c:",
    "shutdown /s", "shutdown /r", "rd /s /q c:\\", "reg delete hklm",
    "bcdedit", "diskpart", "mkfs", "dd if=/dev/zero",
    ":(){:|:&};:", "fork bomb"
]

def _resolve(path: str) -> str:
    """Resolve a path relative to session cwd."""
    if not os.path.isabs(path):
        path = os.path.join(_session["cwd"], path)
    return os.path.abspath(os.path.expanduser(path))

def execute_tool(tool_name: str, args: dict) -> str:
    """Execute an agent tool. Returns result string."""
    try:
        cwd = _session["cwd"]

        if tool_name == "get_cwd":
            return cwd

        elif tool_name == "set_cwd":
            path = _resolve(args.get("path", ""))
            if not os.path.isdir(path):
                return f"[ERROR] Not a directory: {path}"
            _session["cwd"] = path
            return f"[OK] cwd = {path}"

        elif tool_name == "list_dir":
            path = _resolve(args.get("path", "."))
            if not os.path.isdir(path):
                return f"[ERROR] Not a directory: {path}"
            items = sorted(os.listdir(path))
            lines = []
            for item in items[:80]:
                full = os.path.join(path, item)
                if os.path.isdir(full):
                    lines.append(f"[dir]  {item}/")
                else:
                    size = os.path.getsize(full)
                    lines.append(f"[file] {item}  ({size:,} bytes)")
            return "\n".join(lines) or "(empty)"

        elif tool_name == "glob":
            pattern = args.get("pattern", "**/*")
            base = _resolve(args.get("path", "."))
            skip = {".git", "__pycache__", "node_modules", ".venv", "venv"}
            results = []
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in skip]
                for f in files:
                    fp = os.path.join(root, f)
                    rel = os.path.relpath(fp, base)
                    import fnmatch
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f, pattern):
                        results.append(rel)
                if len(results) >= 60:
                    break
            return "\n".join(results[:60]) if results else "(no matches)"

        elif tool_name == "grep":
            pattern = args.get("pattern", "")
            base = _resolve(args.get("path", "."))
            file_glob = args.get("include", "*.py *.js *.ts *.html *.css *.json *.md *.txt")
            import fnmatch
            exts = set(g.lstrip("*") for g in file_glob.split())
            results = []
            skip = {".git", "__pycache__", "node_modules", ".venv", "venv"}
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in skip]
                for fname in files:
                    if any(fname.endswith(e) for e in exts) or not exts:
                        fp = os.path.join(root, fname)
                        try:
                            with open(fp, "r", errors="replace") as fh:
                                for i, line in enumerate(fh, 1):
                                    if pattern.lower() in line.lower():
                                        rel = os.path.relpath(fp, base)
                                        results.append(f"{rel}:{i}:  {line.rstrip()}")
                                        if len(results) >= 40:
                                            break
                        except Exception:
                            pass
                if len(results) >= 40:
                    break
            return "\n".join(results) if results else f"(no matches for '{pattern}')"

        elif tool_name == "read_file":
            path = _resolve(args.get("path", ""))
            if not os.path.isfile(path):
                return f"[ERROR] File not found: {path}"
            with open(path, "r", errors="replace") as fp:
                content = fp.read()
            # show line numbers
            lines = content.splitlines()
            numbered = "\n".join(f"{i+1:4}: {l}" for i, l in enumerate(lines[:300]))
            suffix = f"\n... ({len(lines)-300} more lines)" if len(lines) > 300 else ""
            return numbered + suffix

        elif tool_name == "write_file":
            path = _resolve(args.get("path", ""))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(args.get("content", ""))
            return f"[OK] Written {path} ({len(args.get('content',''))} chars)"

        elif tool_name == "edit_file":
            path = _resolve(args.get("path", ""))
            if not os.path.isfile(path):
                return f"[ERROR] File not found: {path}"
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            if not old_str:
                return "[ERROR] old_str is required"
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                content = fp.read()
            count = content.count(old_str)
            if count == 0:
                # give helpful snippet for debugging
                preview = content[:200].replace("\n", "\\n")
                return f"[ERROR] old_str not found in file. File starts with: {preview}"
            if count > 1:
                return f"[ERROR] old_str matches {count} locations — add more context to make it unique"
            new_content = content.replace(old_str, new_str, 1)
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(new_content)
            return f"[OK] Edited {path}"

        elif tool_name == "run_cmd":
            cmd = args.get("cmd", "").strip()
            low = cmd.lower()
            for pat in _BLOCKED_CMD:
                if pat in low:
                    return f"[BLOCKED] Unsafe pattern: {pat}"
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=60, cwd=cwd, errors="replace"
            )
            out = (r.stdout + r.stderr).strip()
            rc = r.returncode
            prefix = f"[exit {rc}]\n" if rc != 0 else ""
            return (prefix + out)[:4000] if out else f"[exit {rc}] (no output)"

        elif tool_name == "run_python":
            code = args.get("code", "").strip()
            script = os.path.join(WORKSPACE, "_agent_run.py")
            with open(script, "w", encoding="utf-8") as fp:
                fp.write(code)
            r = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=60,
                cwd=cwd, errors="replace"
            )
            out = (r.stdout + r.stderr).strip()
            return out[:4000] if out else "[OK] (no output)"

        elif tool_name == "open_url":
            url = args.get("url", "")
            if url.startswith(("http://", "https://")):
                import webbrowser
                webbrowser.open(url)
                return f"[OK] Opened {url}"
            return "[ERROR] Only http/https URLs"

        elif tool_name == "fetch_url":
            url = args.get("url", "")
            if not url.startswith(("http://", "https://")):
                return "[ERROR] Only http/https URLs"
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "CrimsonWell/1.0 (local AI assistant)"}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    content = r.read(100000).decode("utf-8", errors="replace")
                return content[:8000]
            except urllib.error.HTTPError as e:
                return f"[HTTP {e.code}] {e.reason}"
            except Exception as e:
                return f"[ERROR] {str(e)[:200]}"

        elif tool_name == "web_search":
            query = args.get("query", "").strip()
            if not query:
                return "[ERROR] query is required"
            try:
                # Use DuckDuckGo's simple search (no API key needed)
                search_url = f"https://duckduckgo.com/html/?q={urllib.parse.quote(query)}"
                req = urllib.request.Request(
                    search_url,
                    headers={"User-Agent": "CrimsonWell/1.0 (local AI assistant)"}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    html = r.read(50000).decode("utf-8", errors="replace")
                # Extract title tags (DDG result titles)
                import re
                titles = re.findall(r'<a[^>]*href="([^"]*)"[^>]*title="([^"]*)"', html)
                results = []
                for url, title in titles[:5]:
                    if url and title and not url.startswith('javascript:'):
                        results.append(f"• {title}\n  {url}")
                return "\n".join(results) if results else "(no results found)"
            except Exception as e:
                return f"[ERROR] {str(e)[:200]}"

        elif tool_name == "get_system_info":
            try:
                import platform, psutil
                info = []
                info.append(f"OS: {platform.system()} {platform.release()}")
                info.append(f"Python: {platform.python_version()}")
                info.append(f"CPU: {psutil.cpu_percent(interval=1)}% · {psutil.cpu_count()} cores")
                info.append(f"RAM: {psutil.virtual_memory().percent}% used")
                info.append(f"Disk: {psutil.disk_usage('/').percent}% used")
                return "\n".join(info)
            except ImportError:
                return "[WARN] psutil not installed. Run: pip install psutil"
            except Exception as e:
                return f"[ERROR] {str(e)[:200]}"

        elif tool_name == "get_processes":
            try:
                import psutil
                top = sorted(psutil.process_iter(['pid','name','memory_percent']),
                           key=lambda p: p.info['memory_percent'], reverse=True)[:10]
                lines = ["PID    NAME                          MEM%"]
                for p in top:
                    lines.append(f"{p.info['pid']:5} {p.info['name'][:28]:28} {p.info['memory_percent']:5.1f}%")
                return "\n".join(lines)
            except ImportError:
                return "[WARN] psutil not installed. Run: pip install psutil"
            except Exception as e:
                return f"[ERROR] {str(e)[:200]}"

        elif tool_name == "check_disk":
            try:
                import psutil
                parts = psutil.disk_partitions()
                lines = []
                for p in parts:
                    usage = psutil.disk_usage(p.mountpoint)
                    pct = usage.percent
                    status = "🔴 FULL" if pct > 90 else "🟡 HIGH" if pct > 75 else "🟢 OK"
                    lines.append(f"{p.device:10} {p.mountpoint:20} {pct:5.1f}% {status}")
                return "\n".join(lines) if lines else "(no partitions found)"
            except ImportError:
                return "[WARN] psutil not installed. Run: pip install psutil"
            except Exception as e:
                return f"[ERROR] {str(e)[:200]}"

        elif tool_name == "install_package":
            pkg = args.get("package", "").strip()
            mgr = args.get("manager", "pip").lower()  # pip, npm, choco, apt
            if not pkg:
                return "[ERROR] package name required"
            if mgr not in ["pip", "npm", "choco", "apt"]:
                return f"[ERROR] unsupported package manager: {mgr}"
            cmd_map = {
                "pip": f"pip install {pkg}",
                "npm": f"npm install -g {pkg}",
                "choco": f"choco install -y {pkg}",
                "apt": f"apt-get install -y {pkg}",
            }
            return execute_tool("run_cmd", {"cmd": cmd_map[mgr]})

        else:
            return f"[ERROR] Unknown tool: {tool_name}"

    except subprocess.TimeoutExpired:
        return "[ERROR] Timed out (>60s)"
    except Exception as e:
        return f"[ERROR] {e}"

# ─── AGENT LOOP ───────────────────────────────────────────────────────────────

_AGENT_SYSTEM = """\
You are CrimsonWell Agent — a local AI assistant that works like Claude Code.
You have tools to read/write/edit files, run commands, search code, access the internet, and more.

Working directory: {cwd}

TOOL FORMAT — use EXACTLY this format, one tool at a time:
<tool>{{"name":"TOOL_NAME","args":{{...}}}}</tool>

AVAILABLE TOOLS:
get_cwd       | {{}}                                          | Show working directory
set_cwd       | {{"path":"path/to/project"}}                  | Change working directory
list_dir      | {{"path":"."}}                                | List files & folders
glob          | {{"pattern":"**/*.py","path":"."}}            | Find files by pattern
grep          | {{"pattern":"def main","path":".","include":"*.py"}} | Search file contents
read_file     | {{"path":"main.py"}}                          | Read file with line numbers
edit_file     | {{"path":"file.py","old_str":"exact text","new_str":"replacement"}} | Targeted edit
write_file    | {{"path":"file.py","content":"..."}}          | Create or overwrite file
run_cmd       | {{"cmd":"python main.py"}}                    | Run shell command in cwd
run_python    | {{"code":"print('hello')"}}                   | Execute Python snippet
fetch_url     | {{"url":"https://example.com"}}               | Fetch URL content
web_search    | {{"query":"what is python"}}                  | Search the web (DuckDuckGo)
open_url      | {{"url":"https://..."}}                       | Open URL in browser
get_system_info | {{}}                                         | CPU, RAM, disk, OS info
get_processes | {{}}                                          | Top processes by memory
check_disk    | {{}}                                          | Disk usage per partition
install_package | {{"package":"numpy","manager":"pip"}}      | Install via pip/npm/choco/apt

WORKFLOW (follow this every time):
1. EXPLORE: list_dir / glob / grep to understand the project structure
2. READ: read_file on relevant files before editing
3. EDIT: use edit_file for changes (targeted, safe). Use write_file only for new files.
4. VERIFY: run_cmd to test / check syntax after changes
5. REPORT: summarize exactly what you did and what files changed

RULES:
- One tool per response — wait for the result before the next step
- edit_file old_str must be unique in the file (add surrounding lines if needed)
- Relative paths resolve against cwd: {cwd}
- Explain your reasoning before each tool call
- If something fails, diagnose and try a different approach
- Use web_search / fetch_url when you need current info or to verify facts
"""

_TOOL_RE = re.compile(r'<tool>(.*?)</tool>', re.DOTALL)

def _agent_loop(task: str, history: list, model: str, images: list = None):
    """Claude Code-style ReAct agent loop. Yields SSE chunks."""
    system = _AGENT_SYSTEM.format(cwd=_session["cwd"])
    messages = [{"role": "system", "content": system}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    user_msg: dict = {"role": "user", "content": task}
    if images:
        user_msg["images"] = images
    messages.append(user_msg)

    for step in range(15):
        payload = json.dumps({
            "model": model, "messages": messages, "stream": True,
            "options": {"temperature": 0.2, "num_ctx": 8192}
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
                            safe = token.replace("\n", "\\n")
                            yield f"data: {safe}\n\n"
                        if chunk.get("done"):
                            break
                    except Exception:
                        pass
        except Exception as e:
            err = {"type": "tool_result", "tool": "error", "ok": False, "result": str(e)}
            yield f"data: {json.dumps(err)}\n\n"
            break

        full_text = "".join(full_response)
        messages.append({"role": "assistant", "content": full_text})

        # Parse tool calls
        calls = _TOOL_RE.findall(full_text)
        if not calls:
            break  # Done — no more tool calls

        tool_results = []
        for raw in calls:
            try:
                call = json.loads(raw.strip())
                t_name = call.get("name", call.get("tool", ""))
                t_args = call.get("args", {})
            except Exception as e:
                tool_results.append(f"[parse error: {e}] raw={raw[:100]}")
                continue

            # Emit tool_call event so UI can render it
            tc_evt = {"type": "tool_call", "tool": t_name, "args": t_args}
            yield f"data: {json.dumps(tc_evt)}\n\n"

            result = execute_tool(t_name, t_args)
            ok = not result.startswith(("[ERROR]", "[BLOCKED]"))
            preview = result[:600]

            # Emit tool_result event
            tr_evt = {"type": "tool_result", "tool": t_name, "ok": ok, "result": preview}
            yield f"data: {json.dumps(tr_evt)}\n\n"

            tool_results.append(
                f"<tool_result name='{t_name}' ok='{ok}'>\n{result[:3000]}\n</tool_result>"
            )

        messages.append({"role": "user", "content": "\n".join(tool_results)})

    record_usage("agent", model)
    yield f"data: {json.dumps({'type':'cwd','path':_session['cwd']})}\n\n"
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
        "cwd":         _session["cwd"],
    }

# ─── STREAMING CHAT ───────────────────────────────────────────────────────────

def stream_chat(message: str, history: list, model_override: str | None, files: list = None, intent_override: str = None):
    """
    Generator that yields SSE lines.
    First yields a meta JSON (intent, model, agent info).
    Then streams tokens from Ollama (or runs agent loop for agent intent).
    """
    ol = ollama_models()
    gpu = cached_gpu()

    # Route intent (use override if provided, else infer from message)
    if intent_override and intent_override in AGENTS:
        intent = intent_override
        route = AGENTS.get(intent, route_intent(message))
        # For non-chat intents, ensure we have a system prompt
        if "system" not in route:
            fallback = route_intent(message)
            route = {**fallback, **route}
    else:
        route = route_intent(message)
        intent = route["intent"]

    # Pick model (use smart selector by default, override if specified)
    if model_override and model_override in ol["models"]:
        model = model_override
    elif ol["models"]:
        # Smart model selection based on intent + VRAM + benchmarks
        try:
            model = select_model(ol["models"], intent, gpu["vram_mb"])
        except:
            # Fallback to old method if smart selector fails
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
            intent_ov = body.get("intent") or None
            files    = body.get("files") or []

            if not message and not files:
                self._json({"error": "empty message"}, 400)
                return
            if not message and files:
                message = "Please analyze the attached file(s)."

            self._sse_headers()
            try:
                for chunk in stream_chat(message, history, model_ov, files, intent_ov):
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif self.path == "/api/cwd":
            path = body.get("path", "").strip()
            if path:
                path = os.path.abspath(os.path.expanduser(path))
                if os.path.isdir(path):
                    _session["cwd"] = path
                    self._json({"ok": True, "cwd": path})
                else:
                    self._json({"ok": False, "error": f"Not a directory: {path}"})
            else:
                self._json({"ok": True, "cwd": _session["cwd"]})

        elif self.path == "/api/pull":
            model = body.get("model", "").strip()
            if not model:
                self._json({"ok": False, "error": "no model specified"}, 400)
                return
            ollama_pull_bg(model)
            self._json({"ok": True, "msg": f"Downloading {model} in background..."})

        elif self.path == "/api/discover-models":
            # Discover new models from Ollama library
            limit = body.get("limit", 20)
            models = discover_models(limit)
            self._json({"models": models})

        elif self.path == "/api/benchmark":
            # Benchmark a model
            model = body.get("model", "").strip()
            if not model:
                self._json({"error": "model required"}, 400)
                return
            result = benchmark_model(model)
            self._json(result)

        elif self.path == "/api/compare-models":
            # Compare two models
            old_model = body.get("old_model", "").strip()
            new_model = body.get("new_model", "").strip()
            if not old_model or not new_model:
                self._json({"error": "both models required"}, 400)
                return
            result = compare_models(old_model, new_model)
            self._json(result)

        elif self.path == "/api/swap-model":
            # Safely swap a model
            old = body.get("old_model", "").strip()
            new = body.get("new_model", "").strip()
            intent = body.get("intent", "chat").strip()
            if not old or not new:
                self._json({"error": "both models required"}, 400)
                return
            result = safe_swap_model(old, new, intent)
            self._json(result)

        elif self.path == "/api/discover-skills":
            # Discover available skills
            skills = discover_skills()
            self._json({"skills": skills})

        elif self.path == "/api/validate-skill":
            # Validate a skill file
            path = body.get("path", "").strip()
            if not path:
                self._json({"error": "path required"}, 400)
                return
            result = validate_skill(path)
            self._json(result)

        elif self.path == "/api/update-status":
            # Get current update status
            status = get_update_status()
            self._json(status)

        elif self.path == "/api/set-speed-preference":
            # Set speed vs quality preference (0=fast, 1=quality)
            pref = body.get("preference")
            if pref is None or not (0 <= pref <= 1):
                self._json({"error": "preference must be 0-1"}, 400)
                return
            try:
                set_speed_preference(pref)
                self._json({"ok": True, "preference": pref})
            except Exception as e:
                self._json({"error": str(e)}, 500)

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
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>CrimsonWell</title>
<style>
:root{--c:#dc2626;--cd:#991b1b;--cg:rgba(220,38,38,.12);--bg:#0d0d0d;--s1:#161616;--s2:#1f1f1f;--br:#2a2a2a;--tx:#e5e5e5;--mu:#6b7280;--gr:#22c55e;--yl:#eab308}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);height:100vh;overflow:hidden;display:flex;flex-direction:column;font-size:14px}
.hdr{display:flex;align-items:center;gap:10px;padding:7px 14px;background:var(--s1);border-bottom:1px solid var(--br);flex-shrink:0;min-height:42px}
.logo{font-weight:700;font-size:16px;color:var(--c);letter-spacing:-.5px;white-space:nowrap}
.logo sub{font-size:10px;color:var(--mu);font-weight:400;vertical-align:middle}
.badge{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--mu);background:var(--s2);padding:3px 8px;border-radius:5px;border:1px solid var(--br);white-space:nowrap}
.dot{width:6px;height:6px;border-radius:50%;background:var(--br);flex-shrink:0}
.vbar-wrap{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--mu)}
.vbar{width:60px;height:4px;background:var(--s2);border-radius:3px;overflow:hidden;border:1px solid var(--br)}
.vfill{height:100%;background:var(--c);border-radius:3px;transition:width .5s,background .5s}
.agent-badge{margin-left:auto;display:flex;align-items:center;gap:5px;font-size:11px;padding:3px 9px;border-radius:5px;border:1px solid var(--c);color:var(--c);background:var(--cg);transition:all .3s;white-space:nowrap}
.model-tag{font-size:10px;color:var(--mu);border-left:1px solid var(--br);padding-left:6px;margin-left:2px}
.cwd-bar{display:flex;align-items:center;gap:8px;padding:4px 14px;background:#111;border-bottom:1px solid var(--br);font-size:11px;color:var(--mu);flex-shrink:0}
.cwd-bar span{color:var(--mu)}
#cwd-path{color:var(--tx);font-family:'Consolas','Courier New',monospace;font-size:11px;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cwd-btn{background:none;border:1px solid var(--br);border-radius:3px;color:var(--mu);padding:1px 6px;font-size:10px;cursor:pointer;transition:all .15s}
.cwd-btn:hover{border-color:var(--c);color:var(--c)}
.main{display:flex;flex:1;overflow:hidden}
.sb{width:185px;flex-shrink:0;background:var(--s1);border-right:1px solid var(--br);overflow-y:auto;padding:10px 0}
.sb-sec{padding:0 10px 10px}
.sb-title{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--mu);margin-bottom:5px}
.m-item{display:flex;align-items:center;gap:5px;padding:4px 5px;border-radius:4px;cursor:pointer;font-size:11px;color:var(--mu);transition:all .15s;user-select:none}
.m-item:hover{background:var(--s2);color:var(--tx)}
.m-item.active{color:var(--c);background:var(--cg)}
.m-item.loaded .dot{background:var(--gr)}
.m-vram{font-size:9px;margin-left:auto;color:var(--mu)}
.pull-btn{font-size:9px;padding:1px 5px;background:var(--s2);border:1px solid var(--br);border-radius:3px;color:var(--mu);cursor:pointer;margin-left:auto;transition:all .15s}
.pull-btn:hover{border-color:var(--c);color:var(--c)}
.sk-item{display:flex;align-items:center;gap:5px;padding:3px 5px;font-size:11px;color:var(--mu);border-radius:4px}
.sk-item.active-sk{color:var(--tx)}
.chat{flex:1;display:flex;flex-direction:column;overflow:hidden}
#alerts{flex-shrink:0}
.alert{margin:6px 12px;padding:6px 10px;border-radius:5px;font-size:11px;border:1px solid var(--br);display:flex;align-items:center;gap:6px}
.alert.warn{border-color:var(--yl);color:var(--yl);background:rgba(234,179,8,.08)}
.alert.info{border-color:var(--c);color:var(--mu);background:var(--cg)}
.msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.msg{display:flex;gap:9px;max-width:900px;animation:fadein .15s ease}
@keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.msg.user{flex-direction:row-reverse;margin-left:auto}
.avatar{width:24px;height:24px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;margin-top:3px;font-weight:700}
.msg.user .avatar{background:var(--c);color:#fff}
.msg.ai .avatar{background:var(--s2);border:1px solid var(--br);color:var(--mu)}
.cwrap{display:flex;flex-direction:column;gap:4px;max-width:720px}
.bubble{background:var(--s1);border:1px solid var(--br);border-radius:8px;padding:9px 13px;font-size:13px;line-height:1.65;word-break:break-word}
.msg.user .bubble{background:var(--cg);border-color:var(--c)}
.meta-line{font-size:9px;color:var(--mu);padding:0 2px}
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
.tool-step{border:1px solid var(--br);border-radius:5px;overflow:hidden;font-size:11px;font-family:'Consolas','Courier New',monospace}
.tool-step.ok>.tool-hdr{border-left:3px solid var(--gr)}
.tool-step.err>.tool-hdr{border-left:3px solid #ef4444}
.tool-step.pending>.tool-hdr{border-left:3px solid var(--yl)}
.tool-hdr{display:flex;align-items:center;gap:7px;padding:5px 9px;background:var(--s2);cursor:pointer;user-select:none;min-height:28px}
.tool-hdr:hover{background:var(--br)}
.tool-ico{color:var(--c);font-weight:900;font-size:10px}
.tool-nm{color:var(--tx);font-weight:700}
.tool-ag{color:var(--mu);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-st{font-size:9px;padding:1px 5px;border-radius:3px;white-space:nowrap}
.tool-step.pending .tool-st{background:rgba(234,179,8,.15);color:var(--yl)}
.tool-step.ok .tool-st{background:rgba(34,197,94,.15);color:var(--gr)}
.tool-step.err .tool-st{background:rgba(239,68,68,.15);color:#ef4444}
.tool-body{display:none;padding:7px 9px;background:#0a0a0a;border-top:1px solid var(--br);max-height:220px;overflow-y:auto}
.tool-step.open .tool-body,.tool-step.err.open .tool-body{display:block}
.tool-body pre{font-size:10px;color:var(--mu);white-space:pre-wrap;word-break:break-all;margin:0;line-height:1.5}
.qa{padding:5px 12px;display:flex;gap:5px;flex-wrap:wrap;border-top:1px solid var(--br);flex-shrink:0}
.q-btn{font-size:11px;padding:3px 9px;background:var(--s2);border:1px solid var(--br);border-radius:10px;color:var(--mu);cursor:pointer;transition:all .15s;white-space:nowrap}
.q-btn:hover{border-color:var(--c);color:var(--c);background:var(--cg)}
.inp-area{display:flex;flex-direction:column;gap:5px;padding:8px 12px 10px;background:var(--s1);border-top:1px solid var(--br);flex-shrink:0}
.inp-row{display:flex;gap:7px;align-items:flex-end}
.inp-wrap{flex:1}
#inp{width:100%;background:var(--s2);border:1px solid var(--br);border-radius:7px;color:var(--tx);font-size:13px;padding:9px 12px;outline:none;resize:none;min-height:40px;max-height:120px;font-family:inherit;transition:border-color .15s;line-height:1.5}
#inp:focus{border-color:var(--c)}
#inp::placeholder{color:var(--mu)}
.send{background:var(--c);color:#fff;border:none;border-radius:7px;padding:9px 16px;cursor:pointer;font-size:13px;font-weight:600;transition:background .15s;white-space:nowrap}
.send:hover{background:var(--cd)}
.send:disabled{opacity:.35;cursor:not-allowed}
.attach-btn{background:var(--s2);color:var(--mu);border:1px solid var(--br);border-radius:7px;padding:9px 11px;cursor:pointer;font-size:12px;font-weight:600;transition:all .15s;white-space:nowrap}
.attach-btn:hover{border-color:var(--c);color:var(--c)}
.file-chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{display:flex;align-items:center;gap:4px;background:var(--s2);border:1px solid var(--br);border-radius:4px;padding:2px 7px;font-size:11px;color:var(--mu)}
.chip .rm{cursor:pointer;margin-left:2px;opacity:.6}.chip .rm:hover{color:#ef4444;opacity:1}
.chip.img{border-color:#3b82f6;color:#3b82f6}
.update-btn{width:100%;background:var(--c);color:#fff;border:none;border-radius:5px;padding:6px;font-size:11px;cursor:pointer;font-weight:600;transition:background .15s;margin-bottom:8px}
.update-btn:hover{background:var(--cd)}
.update-item{padding:6px;margin:4px 0;background:#111;border-radius:3px;border-left:2px solid var(--c);font-size:10px}
.update-item.ok{border-left-color:var(--gr)}
.update-item.warn{border-left-color:var(--yl)}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:var(--br);border-radius:2px}
@media(max-width:580px){.sb{display:none}.model-tag{display:none}.cwd-bar{display:none}}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">CrimsonWell <sub>local AI</sub></div>
  <div class="badge" id="gpu-badge"><div class="dot" id="gpu-dot"></div><span id="gpu-name">...</span></div>
  <div class="vbar-wrap"><div class="vbar"><div class="vfill" id="vfill" style="width:0%"></div></div><span id="vram-txt">-</span></div>
  <div class="agent-badge" id="agent-badge">
    <span id="agent-icon">AI</span>
    <span id="agent-name">Chat</span>
    <span class="model-tag" id="model-tag"></span>
  </div>
</div>

<div class="cwd-bar">
  <span>Project:</span>
  <span id="cwd-path">~/Desktop</span>
  <button class="cwd-btn" onclick="promptCwd()" title="Change project directory">change</button>
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
    <div class="sb-sec">
      <div class="sb-title">Updates</div>
      <button class="update-btn" onclick="showUpdatesTab()">🔄 Check Updates</button>
      <div id="update-panel" style="display:none;margin-top:10px;padding:10px;background:var(--s2);border-radius:5px;font-size:11px">
        <div id="update-status">Checking...</div>
      </div>
    </div>
  </div>

  <div class="chat">
    <div id="alerts"></div>
    <div id="msgs" class="msgs"></div>
    <div class="qa" id="qa"></div>
    <div class="inp-area">
      <div id="file-chips" class="file-chips"></div>
      <div class="inp-row">
        <label class="attach-btn" for="file-input">+ File</label>
        <input type="file" id="file-input" multiple style="display:none" onchange="handleFiles(this.files)">
        <div class="inp-wrap">
          <textarea id="inp" placeholder="Chat, write code, run agent tasks, analyze files..." rows="1"></textarea>
        </div>
        <button class="send" id="send-btn" onclick="send()">Send</button>
      </div>
    </div>
  </div>
</div>

<script>
// STATE
let history = [], currentModel = '', currentIntent = 'chat';
let streaming = false, status = {}, attachedFiles = [], currentCwd = '';

const QUICK = [
  {l:'Code', p:'Write a Python script that '},
  {l:'Blender', p:'Write a Blender Python script to create '},
  {l:'Research', p:'Research and explain: '},
  {l:'Write', p:'Write a professional '},
  {l:'Math', p:'Calculate: '},
  {l:'Agent', p:'Do this for me autonomously: '},
  {l:'Edit file', p:'Edit the file at '},
  {l:'Debug', p:'Debug this error: '},
];

// STATUS
async function updateStatus() {
  try {
    const r = await fetch('/api/status');
    status = await r.json();
    document.getElementById('gpu-name').textContent = status.gpu_name || 'CPU';
    document.getElementById('gpu-dot').style.background = status.ollama_ok ? '#22c55e' : '#dc2626';
    if (status.vram_total > 0) {
      const pct = Math.round(status.vram_used / status.vram_total * 100);
      const fill = document.getElementById('vfill');
      fill.style.width = pct + '%';
      fill.style.background = pct > 85 ? '#ef4444' : pct > 65 ? '#eab308' : '#dc2626';
      document.getElementById('vram-txt').textContent =
        (status.vram_used/1024).toFixed(1)+'/'+(status.vram_total/1024).toFixed(0)+'GB';
    } else {
      document.getElementById('vram-txt').textContent = status.gpu_vendor !== 'unknown' ? 'GPU' : 'CPU';
    }
    if (status.cwd) updateCwd(status.cwd);
    renderModels(status.models||[], status.loaded_models||[], status.recommended||[]);
    renderAgents(status.agents||[]);
    const al = document.getElementById('alerts');
    if (!status.ollama_ok) {
      al.innerHTML = '<div class="alert warn">Ollama not running — start it with LAUNCH.bat or run: ollama serve</div>';
    } else if (!(status.models||[]).length) {
      al.innerHTML = '<div class="alert info">No models installed. Click a model to download one for your GPU.</div>';
    } else { al.innerHTML = ''; }
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

// CWD
function updateCwd(path) {
  currentCwd = path;
  const el = document.getElementById('cwd-path');
  if (el) {
    // Show last 2 path segments for brevity
    const parts = path.replace(/\\/g,'/').split('/').filter(Boolean);
    el.textContent = parts.length > 2 ? '.../' + parts.slice(-2).join('/') : path;
    el.title = path;
  }
}
async function promptCwd() {
  const path = prompt('Set project directory (full path):', currentCwd);
  if (!path) return;
  const r = await fetch('/api/cwd', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path})
  });
  const d = await r.json();
  if (d.ok) { updateCwd(d.cwd); addMsg('ai', fmt(`Project directory set to:\n\`${d.cwd}\``)); }
  else addMsg('ai', `<span style="color:#ef4444">Error: ${esc(d.error)}</span>`);
}

// MESSAGES
function addMsg(role, html, meta) {
  const msgs = document.getElementById('msgs');
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const av = role === 'user' ? 'U' : (document.getElementById('agent-icon').textContent || 'AI');
  wrap.innerHTML = `<div class="avatar">${av}</div><div class="cwrap">
    <div class="bubble" id="${meta?.id||('b'+Date.now())}">${html}</div>
    <div class="tools-wrap"></div>
    ${meta?.label?`<div class="meta-line">${meta.label}</div>`:''}
  </div>`;
  msgs.appendChild(wrap);
  msgs.scrollTop = msgs.scrollHeight;
  return wrap;
}

// TOOL RENDERING
let _currentToolEl = null;

function renderToolCall(toolsWrap, info) {
  const argsPreview = Object.entries(info.args||{})
    .map(([k,v])=>typeof v==='string'?v.slice(0,60):JSON.stringify(v).slice(0,60))
    .join(' ');
  const div = document.createElement('div');
  div.className = 'tool-step pending';
  div.innerHTML = `<div class="tool-hdr" onclick="this.parentElement.classList.toggle('open')">
    <span class="tool-ico">></span>
    <span class="tool-nm">${esc(info.tool)}</span>
    <span class="tool-ag">${esc(argsPreview)}</span>
    <span class="tool-st">running</span>
  </div><div class="tool-body"><pre></pre></div>`;
  toolsWrap.appendChild(div);
  _currentToolEl = div;
  document.getElementById('msgs').scrollTop = 99999;
}

function renderToolResult(info) {
  const div = _currentToolEl;
  if (!div) return;
  div.classList.remove('pending');
  div.classList.add(info.ok ? 'ok' : 'err');
  div.querySelector('.tool-st').textContent = info.ok ? 'done' : 'error';
  div.querySelector('.tool-body pre').textContent = info.result || '';
  _currentToolEl = null;
  document.getElementById('msgs').scrollTop = 99999;
}

// FILE HANDLING
function handleFiles(fileList) {
  Array.from(fileList).slice(0, 5 - attachedFiles.length).forEach(file => {
    const reader = new FileReader();
    reader.onload = e => { attachedFiles.push({name:file.name,type:file.type,data:e.target.result}); renderChips(); };
    reader.readAsDataURL(file);
  });
  document.getElementById('file-input').value = '';
}
function renderChips() {
  const el = document.getElementById('file-chips');
  el.innerHTML = '';
  attachedFiles.forEach((f,i) => {
    const isImg = f.type.startsWith('image/');
    const c = document.createElement('div');
    c.className = 'chip' + (isImg?' img':'');
    c.innerHTML = `${isImg?'[img]':'[f]'} ${esc(f.name)} <span class="rm" onclick="removeFile(${i})">x</span>`;
    el.appendChild(c);
  });
}
function removeFile(i) { attachedFiles.splice(i,1); renderChips(); }

// DRAG & DROP anywhere
document.body.addEventListener('dragover', e => { e.preventDefault(); document.getElementById('inp').style.borderColor='var(--c)'; });
document.body.addEventListener('dragleave', e => { if(!e.relatedTarget) document.getElementById('inp').style.borderColor=''; });
document.body.addEventListener('drop', e => { e.preventDefault(); document.getElementById('inp').style.borderColor=''; if(e.dataTransfer.files.length) handleFiles(e.dataTransfer.files); });

// FORMAT
function fmt(text) {
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_,lang,code) => `<pre><code>${esc(code.trim())}</code></pre>`);
  text = text.replace(/`([^`\n]+)`/g, (_,c) => `<code>${esc(c)}</code>`);
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\*(.+?)\*/g, '<em>$1</em>');
  text = text.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  text = text.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  text = text.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  text = text.replace(/^[*\-] (.+)$/gm,'<li>$1</li>');
  text = text.replace(/(<li>.*<\/li>\n?)+/g, s=>`<ul>${s}</ul>`);
  text = text.replace(/(?<![>])\n(?!<)/g,'<br>');
  return text;
}
function esc(t){ return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// INTENT PREVIEW
function previewIntent(text) {
  if (!text.trim()) return;
  const t = text.toLowerCase();
  let intent='chat',icon='AI',name='Chat',color='#6b7280';
  if(/\b(code|script|python|javascript|function|debug|def |class |api|fix|error)\b/.test(t)) {intent='code';icon='{}';name='Coder';color='#3b82f6';}
  else if(/\b(blender|3d|mesh|bpy|render|animate|gltf|stl|geometry)\b/.test(t)) {intent='3d';icon='3D';name='3D';color='#f97316';}
  else if(/\b(research|analyze|explain|compare|summarize|what is|how does)\b/.test(t)) {intent='research';icon='R';name='Research';color='#8b5cf6';}
  else if(/\b(calculate|math|equation|formula|solve|compute)\b/.test(t)) {intent='math';icon='=';name='Math';color='#06b6d4';}
  else if(/\b(write|essay|email|article|blog|document|draft|proofread)\b/.test(t)) {intent='write';icon='W';name='Writer';color='#10b981';}
  else if(/\b(do |run |install|automate|create file|edit file|open |execute|agent)\b/.test(t)) {intent='agent';icon='>>',name='Agent';color='#ec4899';}
  currentIntent = intent;
  const badge = document.getElementById('agent-badge');
  badge.style.borderColor=color; badge.style.color=color;
  document.getElementById('agent-icon').textContent=icon;
  document.getElementById('agent-name').textContent=name;
}

// SEND
async function send() {
  if (streaming) return;
  const inp = document.getElementById('inp');
  const text = inp.value.trim();
  if (!text && !attachedFiles.length) return;

  const filesToSend = [...attachedFiles];
  attachedFiles = []; renderChips();
  inp.value = ''; resize(inp);
  streaming = true;
  document.getElementById('send-btn').disabled = true;

  let userHtml = text ? esc(text) : '';
  if (filesToSend.length) {
    const chips = filesToSend.map(f=>`<span class="chip${f.type.startsWith('image/')?' img':''}" style="display:inline-flex">[${f.type.startsWith('image/')?'img':'f'}] ${esc(f.name)}</span>`).join(' ');
    userHtml = (userHtml?userHtml+'<br>':'') + chips;
  }
  addMsg('user', userHtml||'(files)');
  history.push({role:'user', content: text||'(files attached)'});

  const aiWrap = addMsg('ai', '<div class="typing"><span></span><span></span><span></span></div>');
  const bubble = aiWrap.querySelector('.bubble');
  const toolsWrap = aiWrap.querySelector('.tools-wrap');
  _currentToolEl = null;

  let full='', metaLabel='';

  try {
    const resp = await fetch('/api/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:text||'Analyze attached files.',history:history.slice(-10),model:currentModel||null,intent:currentIntent||'chat',files:filesToSend})
    });
    if (!resp.ok) throw new Error('HTTP '+resp.status);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    outer: while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trimEnd();
        if (data === '[DONE]') break outer;
        if (data.startsWith('{')) {
          try {
            const ev = JSON.parse(data);
            if (ev.type === 'meta') {
              currentIntent=ev.intent||'chat'; currentModel=ev.model||currentModel;
              const badge=document.getElementById('agent-badge');
              badge.style.borderColor=ev.color; badge.style.color=ev.color;
              document.getElementById('agent-icon').textContent=ev.icon;
              document.getElementById('agent-name').textContent=ev.agent;
              document.getElementById('model-tag').textContent=ev.model||'';
              metaLabel=`${ev.model} · ${ev.agent}`;
              continue;
            }
            if (ev.type === 'tool_call') { renderToolCall(toolsWrap, ev); continue; }
            if (ev.type === 'tool_result') { renderToolResult(ev); continue; }
            if (ev.type === 'cwd') { updateCwd(ev.path); continue; }
          } catch(e) {}
        }
        const token = data.replace(/\\n/g,'\n');
        full += token;
        bubble.innerHTML = fmt(full)+'<span class="typing" style="display:inline"><span></span></span>';
        document.getElementById('msgs').scrollTop = 99999;
      }
    }
    bubble.innerHTML = fmt(full);
    if (metaLabel) {
      const ml = document.createElement('div');
      ml.className = 'meta-line'; ml.textContent = metaLabel;
      aiWrap.querySelector('.cwrap').appendChild(ml);
    }
    history.push({role:'assistant', content:full});
  } catch(e) {
    bubble.innerHTML = `<span style="color:#ef4444">Error: ${esc(e.message)}</span>`;
  }
  streaming = false;
  document.getElementById('send-btn').disabled = false;
  document.getElementById('msgs').scrollTop = 99999;
}

// PULL MODEL
async function pullModel(name) {
  await fetch('/api/pull',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:name})});
  addMsg('ai', fmt(`Downloading **${name}** in background... it will appear in the sidebar when ready.`));
}

// SIDEBAR RENDERS
function renderModels(models, loaded, recommended) {
  const list = document.getElementById('model-list');
  list.innerHTML = '';
  if (!models.length && recommended.length) {
    document.getElementById('rec-sec').style.display='block';
    const rl = document.getElementById('rec-list');
    rl.innerHTML = '';
    recommended.slice(0,5).forEach(m => {
      const d=document.createElement('div'); d.className='m-item';
      const gb=(m.vram/1024).toFixed(1);
      d.innerHTML=`<div class="dot"></div><span>${m.name.split(':')[0]}</span><button class="pull-btn" onclick="pullModel('${m.name}')">get ${gb}GB</button>`;
      rl.appendChild(d);
    });
  } else { document.getElementById('rec-sec').style.display='none'; }
  models.forEach(m => {
    const isLoaded=loaded.includes(m), isActive=m===currentModel;
    const d=document.createElement('div');
    d.className='m-item'+(isActive?' active':'')+(isLoaded?' loaded':'');
    const short=m.replace(/:latest$/,'');
    d.innerHTML=`<div class="dot"></div><span title="${m}">${short}</span>`;
    if(isLoaded) d.innerHTML+='<span class="m-vram" style="color:#22c55e">●</span>';
    d.onclick=()=>{ currentModel=m; renderModels(models,loaded,recommended); };
    list.appendChild(d);
  });
}
function renderAgents(agents) {
  const list = document.getElementById('agent-list');
  list.innerHTML = '';
  agents.forEach(a => {
    const active=a.id===currentIntent;
    const d=document.createElement('div');
    d.className='sk-item'+(active?' active-sk':'');
    d.style.color=active?a.color:'';
    d.innerHTML=`<div class="dot" style="background:${active?a.color:'var(--br)'}"></div>${a.icon} ${a.name}`;
    d.onclick=()=>{ currentIntent=a.id; renderAgents(agents); const inp=document.getElementById('inp'); inp.focus(); };
    list.appendChild(d);
  });
}

// QUICK ACTIONS
function renderQA() {
  const qa=document.getElementById('qa');
  QUICK.forEach(q=>{
    const b=document.createElement('button');
    b.className='q-btn'; b.textContent=q.l;
    b.onclick=()=>{ const inp=document.getElementById('inp'); inp.value=q.p; inp.focus(); resize(inp); previewIntent(q.p); };
    qa.appendChild(b);
  });
}

function resize(el){ el.style.height='auto'; el.style.height=Math.min(el.scrollHeight,120)+'px'; }

// INIT
document.getElementById('inp').addEventListener('input',function(){ resize(this); previewIntent(this.value); });
document.getElementById('inp').addEventListener('keydown',function(e){ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();} });

renderQA();
updateStatus();
setInterval(updateStatus, 5000);

// ─── UPDATES TAB ─────────────────────────────────────────────────────────
async function showUpdatesTab() {
  const panel = document.getElementById('update-panel');
  panel.style.display = 'block';
  const statusDiv = document.getElementById('update-status');
  statusDiv.innerHTML = '<div class="update-item">Checking for new models...</div>';

  try {
    // Get update status
    const res = await fetch('/api/update-status');
    const data = await res.json();

    let html = '';

    // Show discovered models
    if (data.discovered_count > 0) {
      html += `<div class="update-item ok">📦 ${data.discovered_count} new models found</div>`;
    }

    // Show recent swaps
    if (data.recent_swaps && data.recent_swaps.length > 0) {
      html += `<div class="update-item">📊 Recent upgrades:</div>`;
      data.recent_swaps.forEach(s => {
        html += `<div style="font-size:9px;margin-left:8px;color:#888">${s.old_model.split(':')[0]} → ${s.new_model.split(':')[0]}</div>`;
      });
    }

    // Show skills
    if (data.available_skills && data.available_skills.length > 0) {
      html += `<div class="update-item">⚙️ ${data.available_skills.length} skills available</div>`;
    }

    html += `<div style="margin-top:8px;font-size:9px;color:#666">Last checked: ${data.last_checked || 'Never'}</div>`;
    statusDiv.innerHTML = html || '<div class="update-item warn">No updates available</div>';
  } catch(e) {
    statusDiv.innerHTML = `<div class="update-item">Error: ${e.message}</div>`;
  }
}

async function benchmarkAndSwap(oldModel, newModel) {
  const inp = document.getElementById('inp');
  inp.value = `Compare and swap: ${oldModel} → ${newModel}`;

  addMsg('ai', 'Starting benchmark comparison... This may take a minute.');

  try {
    const res = await fetch('/api/compare-models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({old_model: oldModel, new_model: newModel})
    });
    const result = await res.json();

    if (result.recommend_swap) {
      addMsg('ai', fmt(`✅ **New model is ${result.delta_percent}% better!**\n\nOld: ${result.old_score}/100\nNew: ${result.new_score}/100\n\nRecommend swap to \`${newModel}\``));

      // Auto-swap
      const swapRes = await fetch('/api/swap-model', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({old_model: oldModel, new_model: newModel, intent: 'auto'})
      });
      const swapResult = await swapRes.json();
      if (swapResult.ok) {
        addMsg('ai', `✅ Swapped! ${swapResult.message}`);
      }
    } else {
      addMsg('ai', fmt(`Current model still better.\n\nOld: ${result.old_score}/100\nNew: ${result.new_score}/100`));
    }
  } catch(e) {
    addMsg('ai', `Error: ${e.message}`);
  }
}
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
