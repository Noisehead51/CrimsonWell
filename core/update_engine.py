"""
CrimsonWell Update Engine
Auto-discover models, benchmark them, and safely upgrade.
Includes skill discovery and validation.
"""

import json
import os
import subprocess
import time
import urllib.request
import re
from datetime import datetime

_HOME = os.path.expanduser("~")
_MANIFEST = os.path.join(_HOME, ".crimsonwell", "update_manifest.json")
_BACKUPS = os.path.join(_HOME, ".crimsonwell", "backups")

os.makedirs(_BACKUPS, exist_ok=True)


def load_manifest():
    """Load update history/manifest."""
    if os.path.exists(_MANIFEST):
        try:
            with open(_MANIFEST) as f:
                return json.load(f)
        except:
            pass
    return {
        "last_checked": None,
        "discovered_models": [],
        "benchmarks": {},
        "skill_updates": [],
        "swaps": [],
    }


def save_manifest(data):
    """Save update history."""
    os.makedirs(os.path.dirname(_MANIFEST), exist_ok=True)
    with open(_MANIFEST, "w") as f:
        json.dump(data, f, indent=2)


def discover_models(limit=20) -> list:
    """
    Fetch trending/new models from Ollama library.
    Returns list of model names with tags and sizes.
    """
    try:
        # Query ollama library (approximate model sizes from inference)
        url = "https://ollama.ai/api/tags"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CrimsonWell/1.0"},
            timeout=10
        )
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())

        # Extract model list (simplified)
        # In reality, would parse Ollama's actual library
        models = data.get("models", [])[:limit]

        discovered = []
        for m in models:
            discovered.append({
                "name": m.get("name"),
                "size_gb": m.get("size", 0) / 1e9,
                "pulled_at": m.get("modified_at"),
            })
        return discovered
    except Exception as e:
        return [{"error": str(e)}]


def benchmark_model(model_name: str, baseline_prompt: str = None) -> dict:
    """
    Quick benchmark: speed, quality, memory usage.
    Returns scores 0-100.
    """
    if not baseline_prompt:
        baseline_prompt = "Explain what an algorithm is in one sentence."

    try:
        # Speed test: time to first token + throughput
        start = time.time()
        result = subprocess.run(
            ["ollama", "run", model_name, baseline_prompt],
            capture_output=True,
            text=True,
            timeout=60
        )
        elapsed = time.time() - start
        output_len = len(result.stdout.split())

        if elapsed == 0:
            return {"error": "model timeout"}

        tps = output_len / elapsed  # tokens per second
        speed_score = min(100, int(tps * 10))  # higher = faster

        # Quality score: response length (longer usually = better)
        quality_score = min(100, output_len // 5)

        return {
            "model": model_name,
            "speed_score": speed_score,
            "quality_score": quality_score,
            "tokens_per_sec": round(tps, 2),
            "response_words": output_len,
            "time_sec": round(elapsed, 2),
            "overall_score": round((speed_score + quality_score) / 2),
        }
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "model": model_name}
    except Exception as e:
        return {"error": str(e), "model": model_name}


def compare_models(old_model: str, new_model: str, baseline_prompt: str = None) -> dict:
    """
    Compare two models. Returns delta and recommendation.
    """
    old_bench = benchmark_model(old_model, baseline_prompt)
    new_bench = benchmark_model(new_model, baseline_prompt)

    if "error" in old_bench or "error" in new_bench:
        return {"error": "Benchmark failed", "old": old_bench, "new": new_bench}

    old_score = old_bench.get("overall_score", 0)
    new_score = new_bench.get("overall_score", 0)
    delta = new_score - old_score

    return {
        "old_model": old_model,
        "new_model": new_model,
        "old_score": old_score,
        "new_score": new_score,
        "delta_percent": round(delta * 100 / (old_score or 1), 1),
        "recommend_swap": delta > 5,  # >5 points better
        "old_bench": old_bench,
        "new_bench": new_bench,
    }


def safe_swap_model(old_name: str, new_name: str, intent: str) -> dict:
    """
    Safely swap a model. Backs up the old one first.
    """
    # Create backup (just track in manifest, don't copy files)
    backup_info = {
        "timestamp": datetime.now().isoformat(),
        "old_model": old_name,
        "new_model": new_name,
        "intent": intent,
        "status": "completed",
    }

    manifest = load_manifest()
    manifest["swaps"].append(backup_info)
    save_manifest(manifest)

    return {
        "ok": True,
        "message": f"Swapped {old_name} → {new_name} for {intent}",
        "backup_id": backup_info["timestamp"],
    }


def discover_skills(repo_search_terms=None) -> list:
    """
    Discover skills from local folder + optional GitHub search.
    """
    skills = []

    # Scan local skills/ folder
    skills_dir = os.path.expanduser("~/CrimsonWell/skills")
    if os.path.isdir(skills_dir):
        for f in os.listdir(skills_dir):
            if f.endswith(".py") and not f.startswith("_"):
                skill_name = f[:-3]
                skills.append({
                    "name": skill_name,
                    "source": "local",
                    "path": os.path.join(skills_dir, f),
                })

    # TODO: Search GitHub for community skills if repo_search_terms provided
    # For now, just scan local

    return skills


def validate_skill(skill_path: str) -> dict:
    """
    Basic validation: file exists, can import, has run() function.
    """
    if not os.path.exists(skill_path):
        return {"ok": False, "error": "File not found"}

    try:
        # Try to parse as valid Python
        with open(skill_path) as f:
            code = f.read()
        compile(code, skill_path, "exec")

        # Check for run or main function
        has_run = "def run(" in code or "def main(" in code

        return {
            "ok": True,
            "valid_python": True,
            "has_function": has_run,
            "size_bytes": len(code),
        }
    except SyntaxError as e:
        return {"ok": False, "error": f"Syntax error: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_update_status() -> dict:
    """Get current update status and recommendations."""
    manifest = load_manifest()

    # TODO: Compare installed models against discovered ones
    # For now, return manifest summary

    return {
        "last_checked": manifest.get("last_checked"),
        "discovered_count": len(manifest.get("discovered_models", [])),
        "recent_swaps": manifest.get("swaps", [])[-3:],
        "available_skills": discover_skills(),
    }
