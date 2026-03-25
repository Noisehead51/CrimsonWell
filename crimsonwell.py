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
    from core.skill_manager import (
        scan_local_skills, download_skill, load_skill, run_skill,
        list_skills, enable_skill, disable_skill, get_community_skills
    )
    from core.auto_benchmark import (
        start_scheduler, stop_scheduler, get_scheduler_status, set_scheduler_config
    )
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

    # Check for [SEARCH: ...] or [FETCH: ...] in response
    full_text = "".join(full_response)

    # Look for [SEARCH: query]
    search_match = re.search(r'\[SEARCH:\s*([^\]]+)\]', full_text)
    if search_match:
        query = search_match.group(1).strip()
        try:
            result = execute_tool("web_search", {"query": query})
            yield f'data: \n\n[Web Search for "{query}"]\n{result}\n\n'
        except:
            pass

    # Look for [FETCH: url]
    fetch_match = re.search(r'\[FETCH:\s*([^\]]+)\]', full_text)
    if fetch_match:
        url = fetch_match.group(1).strip()
        try:
            result = execute_tool("fetch_url", {"url": url})
            yield f'data: \n\n[Fetched: {url}]\n{result[:1000]}\n\n'
        except:
            pass

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

        elif self.path == "/api/skills":
            # List all skills
            skills = list_skills()
            self._json(skills)

        elif self.path == "/api/skills/scan":
            # Scan and register local skills
            found = scan_local_skills()
            self._json({"found": len(found), "skills": found})

        elif self.path == "/api/skills/community":
            # List recommended community skills
            community = get_community_skills()
            self._json({"skills": community})

        elif self.path == "/api/skills/download":
            # Download and install a community skill
            url = body.get("url", "").strip()
            name = body.get("name", "").strip()
            if not url or not name:
                self._json({"error": "url and name required"}, 400)
                return
            result = download_skill(url, name)
            self._json(result)

        elif self.path == "/api/skills/enable":
            # Enable a skill
            name = body.get("skill", "").strip()
            if not name:
                self._json({"error": "skill name required"}, 400)
                return
            try:
                enable_skill(name)
                self._json({"ok": True, "message": f"Enabled {name}"})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif self.path == "/api/skills/disable":
            # Disable a skill
            name = body.get("skill", "").strip()
            if not name:
                self._json({"error": "skill name required"}, 400)
                return
            try:
                disable_skill(name)
                self._json({"ok": True, "message": f"Disabled {name}"})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif self.path == "/api/scheduler/start":
            # Start auto-benchmark scheduler
            result = start_scheduler()
            self._json(result)

        elif self.path == "/api/scheduler/stop":
            # Stop scheduler
            result = stop_scheduler()
            self._json(result)

        elif self.path == "/api/scheduler/status":
            # Get scheduler status
            status = get_scheduler_status()
            self._json(status)

        elif self.path == "/api/scheduler/config":
            # Update scheduler config
            config = body.get("config", {})
            result = set_scheduler_config(config)
            self._json(result)

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

        elif self.path == "/api/list-files":
            # List files in a directory (for file tree)
            path = body.get("path", ".").strip()
            try:
                resolved = os.path.abspath(os.path.expanduser(path))
                if not os.path.isdir(resolved):
                    self._json({"error": "not a directory"}, 400)
                    return
                items = []
                for name in sorted(os.listdir(resolved))[:100]:
                    if name.startswith('.'):
                        continue
                    full = os.path.join(resolved, name)
                    try:
                        items.append({
                            "name": name,
                            "type": "dir" if os.path.isdir(full) else "file",
                            "path": full,
                            "size": os.path.getsize(full) if os.path.isfile(full) else None,
                        })
                    except:
                        pass
                self._json({"items": items, "path": resolved})
            except Exception as e:
                self._json({"error": str(e)}, 400)

        elif self.path == "/api/read-file":
            # Read file content (for editor)
            path = body.get("path", "").strip()
            try:
                resolved = os.path.abspath(os.path.expanduser(path))
                if not os.path.isfile(resolved):
                    self._json({"error": "not a file"}, 400)
                    return
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                self._json({"content": content, "path": resolved})
            except Exception as e:
                self._json({"error": str(e)}, 400)

        elif self.path == "/api/write-file":
            # Write file content (from editor)
            path = body.get("path", "").strip()
            content = body.get("content", "")
            try:
                resolved = os.path.abspath(os.path.expanduser(path))
                os.makedirs(os.path.dirname(resolved), exist_ok=True)
                with open(resolved, "w", encoding="utf-8") as f:
                    f.write(content)
                self._json({"ok": True, "path": resolved})
            except Exception as e:
                self._json({"error": str(e)}, 400)

        elif self.path == "/api/exec-command":
            # Execute shell command (for /run slash command)
            cmd = body.get("cmd", "").strip()
            cwd = body.get("cwd", ".").strip()
            try:
                resolved_cwd = os.path.abspath(os.path.expanduser(cwd))
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=resolved_cwd,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                self._json({
                    "ok": True,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                })
            except subprocess.TimeoutExpired:
                self._json({"error": "command timeout (30s)"}, 400)
            except Exception as e:
                self._json({"error": str(e)}, 400)

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
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
:root{--c:#dc2626;--cd:#991b1b;--bg:#0d0d0d;--s1:#161616;--s2:#1f1f1f;--br:#2a2a2a;--tx:#e5e5e5;--mu:#6b7280;--gr:#22c55e;--yl:#eab308}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;width:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);font-size:13px}
body{display:flex;flex-direction:column;overflow:hidden}

/* HEADER */
.header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--s1);border-bottom:1px solid var(--br);height:50px;flex-shrink:0}
.logo{font-weight:700;font-size:15px;color:var(--c);letter-spacing:-.5px}
.gpu-badge{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--mu);background:var(--s2);padding:4px 10px;border-radius:4px;border:1px solid var(--br)}
.vbar{width:80px;height:3px;background:var(--s2);border-radius:2px;overflow:hidden}
.vfill{height:100%;background:var(--c);border-radius:2px;transition:width .3s}
.spacer{flex:1}
.agent-badge{display:flex;align-items:center;gap:6px;padding:4px 10px;background:rgba(220,38,38,.1);border:1px solid var(--c);border-radius:4px;font-size:11px;color:var(--c)}

/* MAIN LAYOUT */
.main{display:flex;flex:1;overflow:hidden}

/* LEFT SIDEBAR - FILE TREE */
.sidebar{width:220px;background:var(--s1);border-right:1px solid var(--br);display:flex;flex-direction:column;overflow:hidden}
.sidebar-title{padding:8px 12px;font-size:11px;font-weight:600;color:var(--mu);text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid var(--br)}
.file-tree{flex:1;overflow-y:auto;padding:8px 0}
.file-item{padding:4px 8px;cursor:pointer;color:var(--mu);font-size:12px;user-select:none;transition:all .1s}
.file-item:hover{background:var(--s2);color:var(--tx)}
.file-item.selected{background:rgba(220,38,38,.15);color:var(--c);border-left:2px solid var(--c)}
.file-item.dir::before{content:'📁 ';margin-right:2px}
.file-item.file::before{content:'📄 ';margin-right:2px}
.file-tree-empty{padding:12px;color:var(--mu);font-size:11px;text-align:center}

/* CENTER - EDITOR + CHAT */
.center{flex:1;display:flex;flex-direction:column;overflow:hidden;border-right:1px solid var(--br)}
.panel-tabs{display:flex;gap:0;padding:0;background:var(--s2);border-bottom:1px solid var(--br);height:40px;flex-shrink:0}
.tab{padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;color:var(--mu);font-size:12px;transition:all .15s;user-select:none;white-space:nowrap}
.tab:hover{color:var(--tx);background:var(--br)}
.tab.active{color:var(--c);border-bottom-color:var(--c)}

.tab-content{display:none;flex:1;overflow:hidden}
.tab-content.active{display:flex}

.editor-panel{display:flex;flex-direction:column;overflow:hidden}
.editor-header{padding:8px 12px;background:var(--s2);border-bottom:1px solid var(--br);font-size:11px;color:var(--mu);display:flex;align-items:center;gap:8px}
.editor-path{flex:1;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.editor-btn{background:none;border:none;color:var(--mu);cursor:pointer;padding:2px 6px;font-size:11px;transition:color .1s}
.editor-btn:hover{color:var(--tx)}
.editor-content{flex:1;overflow:hidden;display:flex;flex-direction:column}
.code-editor{flex:1;overflow-y:auto;padding:12px;font-family:'Consolas','Courier New',monospace;font-size:12px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;background:var(--bg);color:var(--tx)}

.chat-panel{display:flex;flex-direction:column;overflow:hidden}
.messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.msg{display:flex;gap:8px;max-width:85%;animation:fadein .15s ease}
@keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1}}
.msg.user{flex-direction:row-reverse;margin-left:auto;max-width:90%}
.msg-avatar{width:24px;height:24px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;margin-top:2px}
.msg.user .msg-avatar{background:var(--c);color:#fff}
.msg.ai .msg-avatar{background:var(--s2);border:1px solid var(--br);color:var(--mu)}
.msg-content{display:flex;flex-direction:column;gap:4px}
.msg-bubble{background:var(--s1);border:1px solid var(--br);border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.5;word-break:break-word}
.msg.user .msg-bubble{background:rgba(220,38,38,.15);border-color:var(--c)}
.msg-bubble code{font-family:monospace;background:#000;padding:2px 4px;border-radius:2px;font-size:11px}
.msg-bubble pre{background:#000;padding:8px;border-radius:4px;overflow-x:auto;font-size:11px;margin:6px 0}

.input-area{padding:12px;background:var(--s2);border-top:1px solid var(--br);flex-shrink:0}
.input-row{display:flex;gap:8px;align-items:flex-end}
#inp{flex:1;background:var(--s1);border:1px solid var(--br);color:var(--tx);border-radius:5px;padding:10px;font-family:inherit;font-size:12px;resize:none;min-height:36px;max-height:100px;outline:none}
#inp:focus{border-color:var(--c)}
.send-btn{background:var(--c);color:#fff;border:none;border-radius:5px;padding:10px 20px;font-weight:600;cursor:pointer;font-size:12px;transition:background .1s}
.send-btn:hover{background:var(--cd)}
.send-btn:disabled{opacity:.3;cursor:not-allowed}

/* RIGHT SIDEBAR - TERMINAL */
.terminal{width:280px;background:var(--s1);border-left:1px solid var(--br);display:flex;flex-direction:column;overflow:hidden}
.terminal-title{padding:8px 12px;font-size:11px;font-weight:600;color:var(--mu);text-transform:uppercase;border-bottom:1px solid var(--br);display:flex;justify-content:space-between;align-items:center}
.terminal-clear{background:none;border:none;color:var(--mu);cursor:pointer;font-size:10px;padding:2px 6px}
.terminal-clear:hover{color:var(--tx)}
.terminal-output{flex:1;overflow-y:auto;padding:10px;font-family:'Consolas','Courier New',monospace;font-size:11px;color:var(--mu);white-space:pre-wrap;word-wrap:break-word;line-height:1.4}
.terminal-line{margin:2px 0}
.terminal-line.ok{color:var(--gr)}
.terminal-line.err{color:#ef4444}
.terminal-line.tool{color:var(--c)}

/* UTILITIES */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--s2)}
::-webkit-scrollbar-thumb{background:var(--br);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#555}

.hidden{display:none !important}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="logo">CrimsonWell</div>
  <div class="gpu-badge">
    <span id="gpu-name">GPU</span>
    <div class="vbar"><div class="vfill" id="vfill" style="width:0%"></div></div>
  </div>
  <div class="spacer"></div>
  <div class="agent-badge">
    <span id="agent-icon">AI</span>
    <span id="agent-name">Chat</span>
  </div>
</div>

<!-- MAIN -->
<div class="main">
  <!-- LEFT: FILE TREE -->
  <div class="sidebar">
    <div class="sidebar-title">Project Files</div>
    <div class="file-tree" id="file-tree">
      <div class="file-tree-empty">No files</div>
    </div>
  </div>

  <!-- CENTER: EDITOR + CHAT -->
  <div class="center">
    <!-- TABS -->
    <div class="panel-tabs">
      <div class="tab active" onclick="switchTab(0)">💬 Chat</div>
      <div class="tab" onclick="switchTab(1)">📝 Editor</div>
      <div class="tab" onclick="switchTab(2)">🤖 Plans</div>
    </div>

    <!-- TAB 0: CHAT -->
    <div class="tab-content active">
      <div class="chat-panel">
        <div class="messages" id="messages"></div>
        <div class="input-area">
          <div class="input-row">
            <textarea id="inp" placeholder="Type a message or /help for commands..." rows="1"></textarea>
            <button class="send-btn" id="send-btn" onclick="sendMessage()">Send</button>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 1: EDITOR -->
    <div class="tab-content">
      <div class="editor-panel">
        <div class="editor-header">
          <span class="editor-path" id="editor-path">No file selected</span>
          <button class="editor-btn" id="save-btn" onclick="saveFile()" title="Save">Save</button>
          <button class="editor-btn" onclick="closeFile()" title="Close">Close</button>
        </div>
        <div class="editor-content">
          <div class="code-editor" id="code-editor" contenteditable="true"></div>
        </div>
      </div>
    </div>

    <!-- TAB 2: PLANS -->
    <div class="tab-content">
      <div style="flex:1;overflow-y:auto;padding:14px;color:var(--mu)">
        <div id="plans-view">No plan yet. Agent will show reasoning here.</div>
      </div>
    </div>
  </div>

  <!-- RIGHT: TERMINAL -->
  <div class="terminal">
    <div class="terminal-title">
      Terminal
      <button class="terminal-clear" onclick="clearTerminal()">Clear</button>
    </div>
    <div class="terminal-output" id="terminal"></div>
  </div>
</div>

<script>
// STATE
let history = [], currentModel = '', currentIntent = 'chat';
let streaming = false;
let selectedFile = null;
let currentTab = 0;

// TAB SWITCHING
function switchTab(n) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', i===n));
  document.querySelectorAll('.tab-content').forEach((c,i) => c.classList.toggle('active', i===n));
  currentTab = n;
  if(n===1 && selectedFile) loadFileInEditor(selectedFile);
}

// STATUS
async function updateStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('gpu-name').textContent = s.gpu_name || 'CPU';
    if(s.vram_total > 0) {
      const pct = Math.round(s.vram_used / s.vram_total * 100);
      document.getElementById('vfill').style.width = pct + '%';
    }
  } catch(e) {}
}

// FILE TREE
async function loadFileTree(path = '.') {
  try {
    const res = await fetch('/api/list-files', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path})
    });
    const data = await res.json();
    const tree = document.getElementById('file-tree');
    tree.innerHTML = '';
    if(data.items) {
      data.items.forEach(item => {
        const div = document.createElement('div');
        div.className = 'file-item ' + item.type;
        div.textContent = '  '.repeat(0) + (item.type==='dir' ? '📁 ' : '📄 ') + item.name;
        div.onclick = function() { selectFile(this, item); };
        tree.appendChild(div);
      });
    }
  } catch(e) {
    addTerminalLine('Error loading files: ' + e.message, 'err');
  }
}

function selectFile(el, item) {
  document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
  el.classList.add('selected');
  selectedFile = item.path;
  document.getElementById('editor-path').textContent = item.name;
  if(item.type === 'file' && currentTab === 1) {
    loadFileInEditor(selectedFile);
  } else if(item.type === 'dir') {
    loadFileTree(selectedFile);
  }
}

// EDITOR
async function loadFileInEditor(filepath) {
  const editor = document.getElementById('code-editor');
  editor.textContent = 'Loading...';
  try {
    const res = await fetch('/api/read-file', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: filepath})
    });
    const data = await res.json();
    if(data.error) {
      editor.textContent = 'Error: ' + data.error;
    } else {
      editor.textContent = data.content;
    }
  } catch(e) {
    editor.textContent = 'Error loading file: ' + e.message;
  }
}

async function saveFile() {
  if(!selectedFile) {
    addTerminalLine('No file selected', 'err');
    return;
  }
  const content = document.getElementById('code-editor').textContent;
  try {
    const res = await fetch('/api/write-file', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: selectedFile, content: content})
    });
    const data = await res.json();
    if(data.error) {
      addTerminalLine('Save error: ' + data.error, 'err');
    } else {
      addTerminalLine('Saved ' + selectedFile, 'ok');
    }
  } catch(e) {
    addTerminalLine('Save failed: ' + e.message, 'err');
  }
}

function closeFile() {
  selectedFile = null;
  document.getElementById('code-editor').textContent = '';
  document.getElementById('editor-path').textContent = 'No file selected';
}

async function executeCommand(cmd) {
  try {
    const res = await fetch('/api/exec-command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cmd: cmd, cwd: '.'})
    });
    const data = await res.json();
    if(data.error) {
      addTerminalLine('Error: ' + data.error, 'err');
    } else {
      if(data.stdout) {
        data.stdout.split('\n').forEach(line => {
          if(line) addTerminalLine(line, data.returncode === 0 ? 'ok' : 'err');
        });
      }
      if(data.stderr) {
        data.stderr.split('\n').forEach(line => {
          if(line) addTerminalLine(line, 'err');
        });
      }
      if(data.returncode !== 0) {
        addTerminalLine(`Process exited with code ${data.returncode}`, 'err');
      }
    }
  } catch(e) {
    addTerminalLine('Command failed: ' + e.message, 'err');
  }
}

// MESSAGES
function addMessage(role, text) {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = `
    <div class="msg-avatar">${role==='user' ? 'U' : 'AI'}</div>
    <div class="msg-content">
      <div class="msg-bubble">${text}</div>
    </div>
  `;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function addPlan(step) {
  const plans = document.getElementById('plans-view');
  if(!plans.textContent.includes('Agent will show')) {
    plans.innerHTML += `<div style="padding:8px;background:var(--s2);border-radius:4px;margin:4px 0;font-size:11px">${step}</div>`;
  } else {
    plans.innerHTML = `<div style="padding:8px;background:var(--s2);border-radius:4px;margin:4px 0;font-size:11px">${step}</div>`;
  }
}

function addTerminalLine(text, type='') {
  const term = document.getElementById('terminal');
  const line = document.createElement('div');
  line.className = 'terminal-line ' + type;
  line.textContent = text;
  term.appendChild(line);
  term.scrollTop = term.scrollHeight;
}

function clearTerminal() {
  document.getElementById('terminal').innerHTML = '';
}

// CHAT
async function sendMessage() {
  const inp = document.getElementById('inp');
  const text = inp.value.trim();
  if(!text) return;

  // Handle slash commands
  if(text.startsWith('/')) {
    handleSlashCommand(text);
    inp.value = '';
    return;
  }

  streaming = true;
  document.getElementById('send-btn').disabled = true;
  addMessage('user', text);
  inp.value = '';
  inp.style.height = 'auto';

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message:text,history:history.slice(-10),intent:currentIntent,model:currentModel||null,files:[]})
    });

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '', full = '';

    while(true) {
      const {done, value} = await reader.read();
      if(done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n');
      buf = lines.pop();

      let streamDone = false;
      for(const line of lines) {
        if(!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if(data === '[DONE]') { streamDone = true; break; }
        if(data.startsWith('{')) {
          try {
            const ev = JSON.parse(data);
            if(ev.type === 'meta') {
              currentIntent = ev.intent || 'chat';
              currentModel = ev.model || currentModel;
              document.getElementById('agent-name').textContent = ev.agent || 'Chat';
              continue;
            }
            if(ev.type === 'tool_call') {
              addTerminalLine('→ ' + ev.tool + '(...)', 'tool');
              continue;
            }
            if(ev.type === 'tool_result') {
              addTerminalLine('✓ ' + ev.tool, ev.ok ? 'ok' : 'err');
              continue;
            }
          } catch(e) {}
        }
        const token = data.replace(/\\n/g, '\n');
        full += token;
      }
      if(streamDone) break;
    }

    if(full) addMessage('ai', full.replace(/</g,'&lt;').replace(/>/g,'&gt;'));
    history.push({role:'user',content:text}, {role:'assistant',content:full});
  } catch(e) {
    addMessage('ai', 'Error: ' + e.message);
  }

  streaming = false;
  document.getElementById('send-btn').disabled = false;
}

function handleSlashCommand(cmd) {
  const parts = cmd.split(' ');
  const c = parts[0].slice(1);
  switch(c) {
    case 'new':
      selectedFile = parts[1] || 'untitled.py';
      switchTab(1);
      document.getElementById('editor-path').textContent = selectedFile;
      document.getElementById('code-editor').textContent = '# ' + selectedFile + '\n\n';
      addTerminalLine('Created ' + selectedFile);
      break;
    case 'save':
      saveFile();
      break;
    case 'run':
      const cmd_str = parts.slice(1).join(' ') || 'python main.py';
      addTerminalLine('$ ' + cmd_str, '');
      executeCommand(cmd_str);
      break;
    case 'clear':
      clearTerminal();
      break;
    case 'help':
      addTerminalLine('/new <file>  - Create new file');
      addTerminalLine('/save        - Save current file');
      addTerminalLine('/run <cmd>   - Run command');
      addTerminalLine('/clear       - Clear terminal');
      break;
    default:
      addTerminalLine('Unknown command: ' + c);
  }
}

// INIT
document.getElementById('inp').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 100) + 'px';
});
document.getElementById('inp').addEventListener('keydown', function(e) {
  if(e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

updateStatus();
loadFileTree();
setInterval(updateStatus, 5000);

// Quick message for demo
addTerminalLine('[CrimsonWell] Ready. Type /help for commands.', 'ok');
</script>
</body>
</html>

"""

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

    # Start background scheduler for model benchmarking
    try:
        start_scheduler()
        print("  ✓ Auto-benchmark scheduler started")
    except Exception as e:
        print(f"  [!] Scheduler failed to start: {e}")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  CrimsonWell stopped.")
            try:
                stop_scheduler()
            except:
                pass


if __name__ == "__main__":
    start()
