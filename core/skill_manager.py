"""
Skill Manager
Auto-discovers, downloads, validates, and registers community skills.
"""

import os
import json
import urllib.request
import subprocess
from datetime import datetime

_HOME = os.path.expanduser("~")
_SKILLS_DIR = os.path.join(_HOME, "CrimsonWell", "skills")
_REGISTRY_FILE = os.path.join(_HOME, ".crimsonwell", "skill_registry.json")

os.makedirs(_SKILLS_DIR, exist_ok=True)


def load_registry():
    """Load skill registry (installed skills metadata)."""
    if os.path.exists(_REGISTRY_FILE):
        try:
            with open(_REGISTRY_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"installed": {}, "disabled": [], "last_updated": None}


def save_registry(registry):
    """Save skill registry."""
    os.makedirs(os.path.dirname(_REGISTRY_FILE), exist_ok=True)
    with open(_REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def scan_local_skills():
    """Scan skills/ folder and auto-register Python files."""
    registry = load_registry()
    installed = {}

    for filename in os.listdir(_SKILLS_DIR):
        if filename.endswith(".py") and not filename.startswith("_"):
            skill_name = filename[:-3]
            path = os.path.join(_SKILLS_DIR, filename)

            installed[skill_name] = {
                "source": "local",
                "path": path,
                "installed": True,
                "enabled": skill_name not in registry.get("disabled", []),
                "timestamp": datetime.now().isoformat(),
            }

    registry["installed"] = installed
    registry["last_updated"] = datetime.now().isoformat()
    save_registry(registry)

    return installed


def download_skill(url: str, skill_name: str) -> dict:
    """
    Download a skill from GitHub (or any URL).
    url should point to raw .py file (e.g., raw.githubusercontent.com/...)
    """
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "Invalid URL"}

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CrimsonWell/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.read().decode("utf-8")

        # Validate it's Python
        try:
            compile(code, skill_name, "exec")
        except SyntaxError as e:
            return {"ok": False, "error": f"Syntax error: {e}"}

        # Save to skills folder
        path = os.path.join(_SKILLS_DIR, f"{skill_name}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)

        # Register
        registry = load_registry()
        registry["installed"][skill_name] = {
            "source": "github",
            "url": url,
            "path": path,
            "installed": True,
            "enabled": True,
            "timestamp": datetime.now().isoformat(),
        }
        save_registry(registry)

        return {
            "ok": True,
            "message": f"Installed skill: {skill_name}",
            "path": path,
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def validate_skill(path: str) -> dict:
    """Validate a skill file."""
    if not os.path.exists(path):
        return {"ok": False, "error": "File not found"}

    try:
        with open(path) as f:
            code = f.read()

        compile(code, path, "exec")

        has_run = "def run(" in code or "def main(" in code
        has_docstring = '"""' in code or "'''" in code

        return {
            "ok": True,
            "valid_python": True,
            "has_function": has_run,
            "has_docstring": has_docstring,
            "size_bytes": len(code),
        }
    except SyntaxError as e:
        return {"ok": False, "error": f"Syntax: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def load_skill(skill_name: str):
    """
    Dynamically load a skill module.
    Returns (module, error) tuple.
    """
    registry = load_registry()
    if skill_name not in registry["installed"]:
        return None, f"Skill not found: {skill_name}"

    skill_info = registry["installed"][skill_name]
    if not skill_info.get("enabled", True):
        return None, f"Skill disabled: {skill_name}"

    path = skill_info["path"]
    if not os.path.exists(path):
        return None, f"Skill file missing: {path}"

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(skill_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, None
    except Exception as e:
        return None, f"Load error: {str(e)}"


def run_skill(skill_name: str, *args, **kwargs) -> str:
    """Run a skill's main function."""
    module, error = load_skill(skill_name)
    if error:
        return f"[ERROR] {error}"

    try:
        if hasattr(module, "run"):
            result = module.run(*args, **kwargs)
        elif hasattr(module, "main"):
            result = module.main(*args, **kwargs)
        else:
            return "[ERROR] Skill has no run() or main() function"

        return str(result)
    except Exception as e:
        return f"[ERROR] {str(e)}"


def list_skills() -> dict:
    """List all registered skills with metadata."""
    registry = load_registry()
    return {
        "installed": registry["installed"],
        "disabled": registry["disabled"],
        "count": len(registry["installed"]),
        "enabled_count": len([
            s for s in registry["installed"].values()
            if s.get("enabled", True)
        ]),
    }


def enable_skill(skill_name: str):
    """Enable a skill."""
    registry = load_registry()
    if skill_name in registry.get("disabled", []):
        registry["disabled"].remove(skill_name)
    save_registry(registry)


def disable_skill(skill_name: str):
    """Disable a skill."""
    registry = load_registry()
    if skill_name not in registry.get("disabled", []):
        registry["disabled"].append(skill_name)
    save_registry(registry)


# Known community skills (example)
COMMUNITY_SKILLS = [
    {
        "name": "git_wrapper",
        "url": "https://raw.githubusercontent.com/Noisehead51/CrimsonWell-skills/main/git_wrapper.py",
        "description": "Git command wrapper with status/diff parsing",
    },
    {
        "name": "image_analyzer",
        "url": "https://raw.githubusercontent.com/Noisehead51/CrimsonWell-skills/main/image_analyzer.py",
        "description": "Analyze images with Ollama vision models",
    },
    {
        "name": "file_formatter",
        "url": "https://raw.githubusercontent.com/Noisehead51/CrimsonWell-skills/main/file_formatter.py",
        "description": "Auto-format code files (Python, JS, JSON, YAML)",
    },
]


def get_community_skills() -> list:
    """Get list of recommended community skills."""
    return COMMUNITY_SKILLS
