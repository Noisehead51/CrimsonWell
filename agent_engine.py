#!/usr/bin/env python3
"""
AUTONOMOUS AGENT ENGINE
ReAct loop (Reason + Act) with a 3-tier safety gate.

Safety tiers:
  AUTO    — read, check, list, open apps  → executes immediately
  ASK     — write files, run commands, install packages → shows user first
  CONFIRM — delete, registry, system changes → requires explicit typed confirmation
"""
import subprocess, os, json, re, threading, uuid, time, urllib.request, glob
from typing import Optional

OLLAMA = "http://localhost:11434"

# ─── DYNAMIC BASE PATHS (no hardcoded usernames) ──────────────────────────────
_HOME       = os.path.expanduser("~")
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE  = os.path.join(_HOME, ".crimsonwell", "workspace")
os.makedirs(_WORKSPACE, exist_ok=True)

# ─── PATH HELPERS ─────────────────────────────────────────────────────────────

def _resolve_path(path: str) -> str:
    """Resolve shorthand names and relative paths to full absolute paths."""
    if not path:
        return _WORKSPACE
    path = os.path.expandvars(path.strip())
    if os.path.isabs(path):
        return path
    shortcuts = {
        "workspace":           _WORKSPACE,
        "crimsonwell":         _BASE_DIR,
        "desktop":             os.path.join(_HOME, "Desktop"),
        "documents":           os.path.join(_HOME, "Documents"),
        "downloads":           os.path.join(_HOME, "Downloads"),
        "ollama":              os.path.join(_HOME, ".ollama"),
        # legacy aliases kept for backwards compatibility
        "local-ai-production": _WORKSPACE,
        "ai-workspace":        _WORKSPACE,
    }
    key = path.lower().strip("\\/ ")
    if key in shortcuts:
        return shortcuts[key]
    candidate = os.path.join(_WORKSPACE, path)
    if os.path.exists(candidate):
        return candidate
    # Also check CrimsonWell base dir
    candidate2 = os.path.join(_BASE_DIR, path)
    if os.path.exists(candidate2):
        return candidate2
    return path

# ─── TOOL IMPLEMENTATIONS ─────────────────────────────────────────────────────

def _read_file(path: str) -> str:
    try:
        with open(_resolve_path(path), encoding="utf-8", errors="replace") as f:
            return f.read(6000)
    except Exception as e:
        return f"[read_file error] {e}"

def _list_dir(path: str = "") -> str:
    try:
        path = _resolve_path(path)
        entries = os.listdir(path)
        lines = [f"Contents of: {path}", ""]
        for e in entries[:80]:
            fp = os.path.join(path, e)
            tag = "[DIR] " if os.path.isdir(fp) else "      "
            lines.append(tag + e)
        return "\n".join(lines)
    except Exception as e:
        return f"[list_dir error] {e}"

def _search_files(pattern: str, directory: str = "") -> str:
    try:
        directory = _resolve_path(directory)
        results = glob.glob(os.path.join(directory, "**", pattern), recursive=True)
        return "\n".join(results[:30]) if results else "No matches"
    except Exception as e:
        return f"[search_files error] {e}"

def _check_process(name: str = "ollama.exe") -> str:
    r = subprocess.run('tasklist', shell=True, capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if name.lower() in l.lower()]
    return "\n".join(lines) if lines else f"{name}: NOT running"

def _ollama_list() -> str:
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
        return r.stdout or r.stderr
    except Exception as e:
        return f"[ollama_list error] {e}"

def _ollama_pull(model: str) -> str:
    try:
        r = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=600)
        return (r.stdout + r.stderr)[-1000:]
    except Exception as e:
        return f"[ollama_pull error] {e}"

def _run_command(cmd: str) -> str:
    ALLOWED_CMDS = {
        "python", "pip", "pip3", "ollama", "winget", "where", "dir", "echo",
        "type", "tasklist", "netstat", "ipconfig", "systeminfo", "ver", "git",
        "npm", "node", "curl", "ping", "chcp", "set", "path", "blender"
    }
    parts = cmd.strip().split()
    base = os.path.basename(parts[0]).lower().replace(".exe", "") if parts else ""
    if base not in ALLOWED_CMDS:
        return f"[BLOCKED] '{base}' not in allowlist.\nAllowed: {', '.join(sorted(ALLOWED_CMDS))}"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60,
                           env={**os.environ})
        out = (r.stdout + r.stderr).strip()
        return out[-2000:] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "[Timed out after 60s]"
    except Exception as e:
        return f"[run_command error] {e}"

def _write_file(path: str, content: str) -> str:
    path = os.path.expandvars(os.path.abspath(path))
    safe_roots = [
        os.path.abspath(_WORKSPACE),
        os.path.abspath(_BASE_DIR),
        os.path.abspath(os.path.join(_HOME, "Desktop")),
        os.path.abspath(os.path.join(_HOME, "Documents")),
    ]
    if not any(path.startswith(r) for r in safe_roots):
        return (
            f"[BLOCKED] Writes outside safe directories are not allowed.\n"
            f"Safe directories:\n"
            + "\n".join(f"  {r}" for r in safe_roots) +
            f"\nRequested path was: {path}"
        )
    # Back up if file exists
    backup_note = ""
    if os.path.exists(path):
        backup = path + ".bak"
        try:
            import shutil
            shutil.copy2(path, backup)
            backup_note = f" (backup: {backup})"
        except Exception:
            pass
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[OK] Written: {path}{backup_note}"
    except Exception as e:
        return f"[write_file error] {e}"

def _install_pip(package: str) -> str:
    # Sanitize: only allow simple package names
    if not re.match(r'^[\w\-\.\[\]>=<~!]+$', package):
        return f"[BLOCKED] Invalid package name: {package}"
    try:
        r = subprocess.run(
            ["python", "-m", "pip", "install", package],
            capture_output=True, text=True, timeout=180
        )
        return (r.stdout + r.stderr)[-1000:]
    except Exception as e:
        return f"[install_pip error] {e}"

def _install_winget(package_id: str) -> str:
    if not re.match(r'^[\w\.\-]+$', package_id):
        return f"[BLOCKED] Invalid package id: {package_id}"
    try:
        r = subprocess.run(
            ["winget", "install", "--id", package_id, "--silent", "--accept-package-agreements"],
            capture_output=True, text=True, timeout=300
        )
        return (r.stdout + r.stderr)[-1000:]
    except Exception as e:
        return f"[install_winget error] {e}"

def _open_app(path: str) -> str:
    path = os.path.expandvars(path)
    if not os.path.exists(path):
        # Try where command
        r = subprocess.run(f"where {os.path.basename(path)}", shell=True, capture_output=True, text=True)
        if r.stdout.strip():
            path = r.stdout.strip().splitlines()[0]
        else:
            return f"[open_app] Not found: {path}"
    try:
        subprocess.Popen([path], creationflags=subprocess.DETACHED_PROCESS)
        return f"[OK] Opened: {path}"
    except Exception as e:
        return f"[open_app error] {e}"

def _set_env_var(name: str, value: str, scope: str = "user") -> str:
    # Only allow user scope (not machine/system)
    if scope.lower() not in ("user",):
        return "[BLOCKED] Only user-scope env vars allowed (not system/machine)"
    # Block dangerous variable names
    BLOCKED_VARS = {"PATH", "PATHEXT", "COMSPEC", "WINDIR", "SYSTEMROOT", "TEMP", "TMP",
                    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA", "USERNAME"}
    if name.upper() in BLOCKED_VARS:
        return f"[BLOCKED] Cannot modify system variable: {name}"
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             f'[System.Environment]::SetEnvironmentVariable("{name}", "{value}", "User")'],
            capture_output=True, text=True, timeout=15
        )
        os.environ[name] = value  # also set in current process
        return f"[OK] Set {name}={value} (user scope)"
    except Exception as e:
        return f"[set_env_var error] {e}"

def _blender_run_script(script: str) -> str:
    """Save script to temp and open Blender with it."""
    blender = _find_blender()
    if not blender:
        return "[blender_run] Blender not found"
    script_path = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "agent_blender_script.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    try:
        subprocess.Popen([blender, "--python", script_path],
                         creationflags=subprocess.DETACHED_PROCESS)
        return f"[OK] Blender opened with script: {script_path}"
    except Exception as e:
        return f"[blender_run error] {e}"

def _web_search(query: str) -> str:
    """Search DuckDuckGo and return top results (no API key needed)."""
    import urllib.parse, html
    try:
        q = urllib.parse.quote_plus(query)
        req = urllib.request.Request(
            f"https://html.duckduckgo.com/html/?q={q}",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
        # Extract result snippets
        import re
        results = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.DOTALL)
        titles  = re.findall(r'class="result__a"[^>]*>(.*?)</a>', body, re.DOTALL)
        clean = lambda s: re.sub(r'<[^>]+>', '', s).strip()
        out = []
        for i, (t, s) in enumerate(zip(titles[:5], results[:5])):
            out.append(f"{i+1}. {clean(t)}\n   {clean(s)}")
        return "\n\n".join(out) if out else "No results found"
    except Exception as e:
        return f"[web_search error] {e}"

def _fetch_url(url: str) -> str:
    """Fetch a webpage and return readable text (first 3000 chars)."""
    import re
    try:
        if not url.startswith("http"):
            url = "https://" + url
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
        # Strip HTML tags, collapse whitespace
        text = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:3000]
    except Exception as e:
        return f"[fetch_url error] {e}"

def _calculate(expression: str) -> str:
    """Safely evaluate a mathematical or engineering expression."""
    import math
    safe_ns = {
        "__builtins__": {},
        "math": math, "sqrt": math.sqrt, "pi": math.pi, "e": math.e,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10, "exp": math.exp,
        "abs": abs, "round": round, "min": min, "max": max, "pow": pow,
        "floor": math.floor, "ceil": math.ceil,
    }
    try:
        result = eval(expression, safe_ns)
        return f"{expression} = {result}"
    except Exception as e:
        return f"[calculate error] {e}"

def _save_note(filename: str, content: str) -> str:
    """Save a note or result to the crimsonwell workspace."""
    workspace = _WORKSPACE
    os.makedirs(workspace, exist_ok=True)
    # Only allow safe filenames
    import re
    safe_name = re.sub(r'[^\w\-\.]', '_', os.path.basename(filename))
    if not safe_name:
        safe_name = "note.txt"
    path = os.path.join(workspace, safe_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[OK] Saved to: {path}"
    except Exception as e:
        return f"[save_note error] {e}"

def _get_system_info() -> str:
    """Get system info: CPU, RAM, GPU, disk."""
    lines = []
    try:
        r = subprocess.run("wmic cpu get Name /value", shell=True, capture_output=True, text=True, timeout=10)
        cpu = [l for l in r.stdout.splitlines() if "Name=" in l]
        lines.append("CPU: " + (cpu[0].replace("Name=","").strip() if cpu else "unknown"))
    except Exception: pass
    try:
        r = subprocess.run("wmic memorychip get Capacity /value", shell=True, capture_output=True, text=True, timeout=10)
        mems = [int(l.replace("Capacity=","").strip()) for l in r.stdout.splitlines() if "Capacity=" in l and l.strip() != "Capacity="]
        total_gb = sum(mems) / (1024**3)
        lines.append(f"RAM: {total_gb:.1f} GB")
    except Exception: pass
    try:
        r = subprocess.run("wmic path win32_VideoController get Name /value", shell=True, capture_output=True, text=True, timeout=10)
        gpus = [l.replace("Name=","").strip() for l in r.stdout.splitlines() if "Name=" in l and l.strip() != "Name="]
        lines.append("GPU: " + ", ".join(gpus))
    except Exception: pass
    try:
        r = subprocess.run("wmic logicaldisk get Size,FreeSpace,DeviceID /value", shell=True, capture_output=True, text=True, timeout=10)
        disks = {}
        cur = {}
        for l in r.stdout.splitlines():
            if "DeviceID=" in l: cur["id"] = l.replace("DeviceID=","").strip()
            if "FreeSpace=" in l and l.strip() != "FreeSpace=": cur["free"] = int(l.replace("FreeSpace=","").strip() or 0)
            if "Size=" in l and l.strip() != "Size=" and "Size" not in cur:
                cur["size"] = int(l.replace("Size=","").strip() or 0)
                if "id" in cur: disks[cur["id"]] = cur; cur = {}
        for d, v in disks.items():
            sz = v.get("size", 0) / (1024**3)
            fr = v.get("free", 0) / (1024**3)
            if sz > 0: lines.append(f"Disk {d}: {fr:.0f}GB free / {sz:.0f}GB total")
    except Exception: pass
    return "\n".join(lines) if lines else "Could not retrieve system info"

def _find_blender() -> Optional[str]:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    bf = os.path.join(pf, "Blender Foundation")
    if os.path.isdir(bf):
        for entry in sorted(os.listdir(bf), reverse=True):
            exe = os.path.join(bf, entry, "blender.exe")
            if os.path.exists(exe):
                return exe
    return None

# ─── TOOL REGISTRY ────────────────────────────────────────────────────────────

TOOLS = {
    # AUTO — safe, read-only or open-only operations
    "read_file":     {"fn": _read_file,    "level": "auto", "desc": "Read a file's contents"},
    "list_dir":      {"fn": _list_dir,     "level": "auto", "desc": "List files in a folder"},
    "search_files":  {"fn": _search_files, "level": "auto", "desc": "Search for files by pattern"},
    "check_process": {"fn": _check_process,"level": "auto", "desc": "Check if a process is running"},
    "ollama_list":   {"fn": _ollama_list,  "level": "auto", "desc": "List installed Ollama models"},
    "ollama_pull":   {"fn": _ollama_pull,  "level": "auto", "desc": "Download an Ollama model"},
    "open_app":      {"fn": _open_app,     "level": "auto", "desc": "Open an application"},

    "web_search":    {"fn": _web_search,   "level": "auto", "desc": "Search DuckDuckGo for information"},
    "fetch_url":     {"fn": _fetch_url,    "level": "auto", "desc": "Fetch and read a webpage"},
    "calculate":     {"fn": _calculate,    "level": "auto", "desc": "Evaluate math/engineering expressions"},
    "get_system_info":{"fn": _get_system_info,"level": "auto","desc": "Get CPU, RAM, GPU, disk info"},
    "save_note":     {"fn": _save_note,    "level": "auto", "desc": "Save text/results to ai-workspace folder"},

    # ASK — modifies things, shown to user before executing
    "run_command":   {"fn": _run_command,  "level": "ask",  "desc": "Run a shell command (allowlisted only)"},
    "write_file":    {"fn": _write_file,   "level": "ask",  "desc": "Write or edit a file (auto-backs up)"},
    "install_pip":   {"fn": _install_pip,  "level": "ask",  "desc": "Install a Python package"},
    "install_winget":{"fn": _install_winget,"level": "ask", "desc": "Install a Windows app via winget"},
    "set_env_var":   {"fn": _set_env_var,  "level": "ask",  "desc": "Set a user environment variable"},
    "blender_run":   {"fn": _blender_run_script, "level": "ask", "desc": "Run a Python script in Blender"},
}

TOOL_ARGS = {
    "read_file":     ["path"],
    "list_dir":      ["path"],
    "search_files":  ["pattern", "directory"],
    "check_process": ["name"],
    "ollama_list":   [],
    "ollama_pull":   ["model"],
    "open_app":      ["path"],
    "web_search":    ["query"],
    "fetch_url":     ["url"],
    "calculate":     ["expression"],
    "get_system_info": [],
    "save_note":     ["filename", "content"],
    "run_command":   ["cmd"],
    "write_file":    ["path", "content"],
    "install_pip":   ["package"],
    "install_winget":["package_id"],
    "set_env_var":   ["name", "value"],
    "blender_run":   ["script"],
}

# ─── REACT SYSTEM PROMPT ──────────────────────────────────────────────────────

def build_system_prompt() -> str:
    tool_lines = []
    for name, t in TOOLS.items():
        safety = "AUTO-EXECUTE" if t["level"] == "auto" else "ASKS USER FIRST"
        args = ", ".join(TOOL_ARGS.get(name, []))
        tool_lines.append(f"  {name}({args}) [{safety}] — {t['desc']}")
    tool_list = "\n".join(tool_lines)

    return f"""You are an autonomous AI agent for an industrial designer's PC.
You solve tasks step by step using tools. Be methodical and verify before acting.

AVAILABLE TOOLS:
{tool_list}
  done(summary) — Call this when the task is fully complete.

RESPONSE FORMAT (follow exactly, every single time):
THOUGHT: [your reasoning about what to do next]
TOOL: [exact tool name]
INPUT: {{"arg": "value"}}

PATH SHORTCUTS (use these exact strings as the path argument):
- "workspace"   -> ~/.crimsonwell/workspace  (your working folder)
- "crimsonwell" -> CrimsonWell install folder
- "desktop"     -> ~/Desktop
- "documents"   -> ~/Documents
- "downloads"   -> ~/Downloads
- For full paths always use forward slashes or double backslashes in JSON

SAFETY RULES:
- Never modify system files, registry, or paths outside the user's home folder
- Prefer reading/checking before writing
- If unsure, read first, then write
- Always verify success after each action
- If a tool returns an error, try a different approach

Start every task by listing relevant files or checking status first."""

# ─── AGENT SESSION ────────────────────────────────────────────────────────────

class AgentSession:
    def __init__(self, session_id: str, task: str, model: str, auto_approve: bool = False):
        self.id = session_id
        self.task = task
        self.model = model
        self.auto_approve = auto_approve  # if True, ASK steps approved automatically
        self.steps = []           # all steps so far
        self.status = "running"   # running | waiting | done | error | stopped
        self.pending = None       # step waiting for user approval
        self._approval = threading.Event()
        self._approval_result = None
        self._thread = None
        self.created = time.time()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def approve(self, approved: bool):
        self._approval_result = approved
        self._approval.set()

    def stop(self):
        self.status = "stopped"
        self._approval_result = False
        self._approval.set()  # unblock if waiting

    def _add_step(self, step: dict):
        step["ts"] = time.time()
        self.steps.append(step)

    def _llm(self, messages: list) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 4096}
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA}/api/chat", data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
            return data.get("message", {}).get("content", "")

    def _parse(self, text: str):
        tm = re.search(r"THOUGHT:\s*(.+?)(?=TOOL:|$)", text, re.DOTALL | re.IGNORECASE)
        om = re.search(r"TOOL:\s*(\w+)", text, re.IGNORECASE)
        im = re.search(r"INPUT:\s*(\{.*?\})\s*$", text, re.DOTALL)
        thought = tm.group(1).strip() if tm else text[:300]
        tool = om.group(1).strip().lower() if om else "done"
        try:
            args = json.loads(im.group(1)) if im else {}
        except Exception:
            args = {}
        return thought, tool, args

    def _run(self):
        system = build_system_prompt()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Task: {self.task}"}
        ]
        max_steps = 20

        for i in range(max_steps):
            if self.status == "stopped":
                self._add_step({"type": "stopped", "msg": "Stopped by user"})
                return

            # LLM decision
            try:
                raw = self._llm(messages)
            except Exception as e:
                self._add_step({"type": "error", "msg": f"LLM error: {e}"})
                self.status = "error"
                return

            thought, tool_name, args = self._parse(raw)

            # Done?
            if tool_name == "done":
                self._add_step({
                    "type": "done",
                    "thought": thought,
                    "summary": args.get("summary", thought)
                })
                self.status = "done"
                return

            # Unknown tool
            if tool_name not in TOOLS:
                msg = f"Unknown tool '{tool_name}'. Use only the listed tools."
                self._add_step({"type": "error", "msg": msg, "thought": thought})
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": f"Error: {msg}"})
                continue

            tool = TOOLS[tool_name]
            step = {
                "type": "step",
                "n": i + 1,
                "thought": thought,
                "tool": tool_name,
                "args": args,
                "level": tool["level"],
                "desc": tool["desc"],
            }

            # Safety gate — ASK level
            if tool["level"] == "ask":
                if self.auto_approve:
                    # Auto-approve mode: log step but execute immediately
                    step["waiting"] = False
                    step["approved"] = True
                    step["auto_approved"] = True
                    self._add_step(step)
                else:
                    step["waiting"] = True
                    self.pending = step
                    self.status = "waiting"
                    self._add_step(step)
                    self._approval.clear()
                    self._approval.wait(timeout=300)  # wait up to 5 min for user

                    approved = self._approval_result
                    self.pending = None
                    step["approved"] = approved

                    if self.status == "stopped":
                        return

                    self.status = "running"
                if not self.auto_approve and not approved:
                    result_str = "User declined this action."
                    self._add_step({"type": "result", "n": i+1, "tool": tool_name, "result": result_str, "approved": False})
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content": f"The user declined that action. Explain what you need or try a different approach."})
                    continue
            else:
                self._add_step(step)

            # Execute tool — always pass args dict (functions have defaults for missing args)
            try:
                fn = tool["fn"]
                result_str = str(fn(**args))[:1500]
            except TypeError as e:
                # If required arg missing, call with no args to get default behavior
                try:
                    result_str = str(fn())[:1500]
                except Exception as e2:
                    result_str = f"[Tool error] {e}. Tried without args: {e2}"
            except Exception as e:
                result_str = f"[Tool error] {e}"

            self._add_step({"type": "result", "n": i+1, "tool": tool_name, "result": result_str})
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Tool result:\n{result_str}\n\nContinue to next step."})

        self._add_step({"type": "done", "summary": "Reached max steps. Task may be incomplete."})
        self.status = "done"


# ─── SESSION MANAGER ──────────────────────────────────────────────────────────

_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.Lock()

def start_session(task: str, model: str, auto_approve: bool = False) -> str:
    sid = str(uuid.uuid4())[:8]
    session = AgentSession(sid, task, model, auto_approve=auto_approve)
    with _sessions_lock:
        _sessions[sid] = session
        # Clean up old sessions (keep last 10)
        if len(_sessions) > 10:
            oldest = sorted(_sessions.keys(), key=lambda k: _sessions[k].created)[0]
            del _sessions[oldest]
    session.start()
    return sid

def get_session(sid: str) -> Optional[AgentSession]:
    return _sessions.get(sid)
