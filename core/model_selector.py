"""
Smart Model Selector
Picks the best model based on task type, VRAM, and benchmarks.
Auto-switches models for optimal performance/speed tradeoff.
"""

import json
import os

_HOME = os.path.expanduser("~")
_PREFS_FILE = os.path.join(_HOME, ".crimsonwell", "model_preferences.json")


def load_preferences():
    """Load user model preferences and benchmark history."""
    if os.path.exists(_PREFS_FILE):
        try:
            with open(_PREFS_FILE) as f:
                return json.load(f)
        except:
            pass
    return {
        "speed_vs_quality": 0.5,  # 0=fast, 1=quality
        "model_overrides": {},  # intent -> specific model
        "benchmarks": {},  # model -> scores
    }


def save_preferences(prefs):
    """Save model preferences."""
    os.makedirs(os.path.dirname(_PREFS_FILE), exist_ok=True)
    with open(_PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def select_model(
    available_models: list,
    intent: str,
    vram_mb: int,
    benchmarks: dict = None,
    speed_preference: float = None,
) -> str:
    """
    Pick the best model for this task.

    Args:
        available_models: list of model names
        intent: 'code', 'chat', 'research', 'agent', '3d', 'system', etc
        vram_mb: available VRAM in MB
        benchmarks: dict of {model_name: score}
        speed_preference: 0=fastest, 1=best quality, 0.5=balanced

    Returns:
        best model name
    """
    prefs = load_preferences()

    # Check for user override
    if intent in prefs["model_overrides"]:
        override = prefs["model_overrides"][intent]
        if override in available_models:
            return override

    if speed_preference is None:
        speed_preference = prefs.get("speed_vs_quality", 0.5)

    # Model preferences by intent (order = preference)
    intent_preferences = {
        "code": ["qwen2.5-coder:7b", "qwen2.5-coder:1.5b", "mistral:7b", "qwen3.5:4b"],
        "3d": ["qwen2.5-coder:7b", "mistral:7b", "qwen3.5:4b"],
        "research": ["deepseek-r1:8b", "llama3.1:8b", "mistral:7b", "qwen3.5:4b"],
        "math": ["deepseek-r1:8b", "llama3.1:8b", "mistral:7b"],
        "write": ["mistral:7b", "qwen3.5:4b", "qwen2.5:9b"],
        "agent": ["mistral:7b", "qwen3.5:4b", "llama3.1:8b"],
        "chat": ["qwen3.5:4b", "mistral:7b", "qwen2.5:9b"],
        "system": ["mistral:7b", "qwen3.5:4b"],
    }

    preferred = intent_preferences.get(intent, ["mistral:7b", "qwen3.5:4b"])

    # Filter by VRAM (rough estimates)
    vram_estimates = {
        "qwen2.5-coder:7b": 4400,
        "qwen2.5-coder:1.5b": 1800,
        "mistral:7b": 4100,
        "qwen3.5:4b": 2400,
        "qwen2.5:9b": 5800,
        "qwen2.5:14b": 9000,
        "llama3.1:8b": 4900,
        "deepseek-r1:8b": 4900,
        "gemma3:4b": 2200,
    }

    fitting_models = [
        m for m in preferred
        if m in available_models and vram_estimates.get(m, 4000) < vram_mb * 0.85
    ]

    if not fitting_models:
        # Fallback: pick any available
        fitting_models = available_models

    # Rank by speed preference
    if speed_preference < 0.3:
        # Prefer fastest (smaller models)
        fitting_models.sort(key=lambda m: vram_estimates.get(m, 4000))
    elif speed_preference > 0.7:
        # Prefer best quality (larger models)
        fitting_models.sort(key=lambda m: vram_estimates.get(m, 4000), reverse=True)
    # else: balanced, use preferred order

    return fitting_models[0] if fitting_models else available_models[0]


def set_speed_preference(preference: float):
    """
    Set global speed vs quality preference.
    0.0 = fastest (small models)
    0.5 = balanced
    1.0 = best quality (large models)
    """
    preference = max(0.0, min(1.0, preference))
    prefs = load_preferences()
    prefs["speed_vs_quality"] = preference
    save_preferences(prefs)


def override_model_for_intent(intent: str, model: str):
    """User can override model selection for a specific intent."""
    prefs = load_preferences()
    prefs["model_overrides"][intent] = model
    save_preferences(prefs)


def record_benchmark(model: str, score: float):
    """Record benchmark score for a model."""
    prefs = load_preferences()
    if "benchmarks" not in prefs:
        prefs["benchmarks"] = {}
    prefs["benchmarks"][model] = {
        "score": score,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
    save_preferences(prefs)
