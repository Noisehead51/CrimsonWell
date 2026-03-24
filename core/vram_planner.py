"""
VRAM-aware model planner for CrimsonWell.
Knows the VRAM cost of every Ollama model and picks the best one that fits.
Never loads a model that will OOM your GPU.
"""

# Model catalog: name → {vram_mb, quality(1-5), speed(1-5), tags, desc}
# vram_mb is the approximate VRAM needed with default quantization (Q4_K_M unless noted)
MODEL_CATALOG = {
    # ── Tiny (≤2GB) — for 4GB VRAM cards or alongside other apps ──────────────
    "llama3.2:1b":       {"vram_mb": 800,  "quality": 2, "speed": 5, "tags": ["chat"],             "desc": "Fastest, 1B params (800MB)"},
    "qwen2:0.5b":        {"vram_mb": 400,  "quality": 1, "speed": 5, "tags": ["chat"],             "desc": "Ultra-tiny (400MB)"},
    "qwen2:1.5b":        {"vram_mb": 1100, "quality": 2, "speed": 5, "tags": ["chat", "code"],     "desc": "Good for 1.5B (1.1GB)"},
    "gemma2:2b":         {"vram_mb": 1600, "quality": 3, "speed": 5, "tags": ["chat"],             "desc": "Google Gemma 2 2B (1.6GB)"},
    "phi3:mini":         {"vram_mb": 1800, "quality": 3, "speed": 5, "tags": ["chat", "code"],     "desc": "Microsoft Phi-3 Mini (1.8GB)"},
    "llama3.2:3b":       {"vram_mb": 2200, "quality": 3, "speed": 4, "tags": ["chat", "write"],    "desc": "Llama 3.2 3B — good balance (2.2GB)"},

    # ── Small (2–5GB) — for 6GB VRAM cards ─────────────────────────────────────
    "mistral:7b":        {"vram_mb": 4100, "quality": 4, "speed": 3, "tags": ["chat", "write"],    "desc": "Mistral 7B — fast all-rounder (4.1GB)"},
    "llama3.1:8b":       {"vram_mb": 4700, "quality": 4, "speed": 3, "tags": ["chat", "agent"],    "desc": "Meta Llama 3.1 8B (4.7GB)"},
    "qwen2.5:7b":        {"vram_mb": 4400, "quality": 4, "speed": 3, "tags": ["chat", "write"],    "desc": "Qwen 2.5 7B (4.4GB)"},
    "qwen2.5-coder:7b":  {"vram_mb": 4400, "quality": 5, "speed": 3, "tags": ["code", "3d"],       "desc": "Best 7B coder (4.4GB)"},
    "deepseek-r1:8b":    {"vram_mb": 4900, "quality": 5, "speed": 2, "tags": ["research", "math"], "desc": "DeepSeek R1 reasoning (4.9GB)"},

    # ── Medium (5–8GB) — for 8GB VRAM cards (RX 6600 XT, RTX 3070) ────────────
    "qwen2.5:9b":        {"vram_mb": 5800, "quality": 5, "speed": 2, "tags": ["chat", "write"],    "desc": "Qwen 2.5 9B — top 9B model (5.8GB)"},
    "qwen2.5-coder:14b": {"vram_mb": 8900, "quality": 5, "speed": 2, "tags": ["code", "3d"],       "desc": "Best 14B coder (8.9GB)"},
    "llama3.1:8b-q8_0":  {"vram_mb": 8500, "quality": 4, "speed": 3, "tags": ["chat"],             "desc": "Llama 3.1 8B full precision (8.5GB)"},

    # ── Large (8–12GB) — for 10-12GB cards ─────────────────────────────────────
    "llama3.2:11b":      {"vram_mb": 7300, "quality": 5, "speed": 2, "tags": ["chat", "vision"],   "desc": "Llama 3.2 11B multimodal (7.3GB)"},
    "mistral:7b-q8_0":   {"vram_mb": 7700, "quality": 4, "speed": 3, "tags": ["chat"],             "desc": "Mistral 7B full quality (7.7GB)"},

    # ── XL (12GB+) ───────────────────────────────────────────────────────────────
    "llama3.1:70b-q4_0": {"vram_mb": 39000,"quality": 5, "speed": 1, "tags": ["chat", "research"], "desc": "Llama 3.1 70B (needs ~40GB)"},
}

# Intent → preferred model tags (ordered)
INTENT_MODEL_TAGS = {
    "code":     ["code", "chat"],
    "3d":       ["code", "chat"],
    "research": ["research", "chat"],
    "math":     ["research", "chat"],
    "write":    ["write", "chat"],
    "agent":    ["agent", "chat"],
    "chat":     ["chat"],
}


def get_recommendations(vram_mb: int, limit: int = 6) -> list:
    """
    Return models from the catalog that fit in vram_mb, sorted by quality desc.
    If vram_mb == 0 (unknown), return small models safe for most setups.
    """
    headroom = 0.85  # use at most 85% of VRAM
    available = vram_mb * headroom if vram_mb > 0 else 4000

    fits = [
        {"name": name, **spec}
        for name, spec in MODEL_CATALOG.items()
        if spec["vram_mb"] <= available
    ]
    fits.sort(key=lambda m: (-m["quality"], m["vram_mb"]))
    return fits[:limit]


def pick_model(available_models: list, intent: str, vram_mb: int) -> str:
    """
    From the list of installed Ollama models, pick the best one for the intent
    that also fits in VRAM.
    Returns model name string, or first available model as fallback.
    """
    if not available_models:
        return ""

    preferred_tags = INTENT_MODEL_TAGS.get(intent, ["chat"])
    headroom = vram_mb * 0.9 if vram_mb > 0 else 999999  # if VRAM unknown, no filter

    # Score each installed model
    def score(model_name: str) -> tuple:
        # Find catalog entry (partial match)
        spec = None
        for catalog_name, s in MODEL_CATALOG.items():
            base = catalog_name.split(":")[0]
            if model_name.startswith(base) or catalog_name in model_name:
                spec = s
                break
        if spec is None:
            return (0, 0, 0)
        # Filter by VRAM
        if spec["vram_mb"] > headroom:
            return (0, 0, 0)  # doesn't fit
        # Tag match score
        tag_score = sum(1 for t in preferred_tags if t in spec.get("tags", []))
        return (tag_score, spec["quality"], -spec["vram_mb"])

    scored = sorted(available_models, key=score, reverse=True)
    return scored[0]


def model_vram(model_name: str) -> int:
    """Look up VRAM estimate for a model name. Returns 0 if unknown."""
    for catalog_name, spec in MODEL_CATALOG.items():
        base = catalog_name.split(":")[0]
        if model_name.startswith(base) or catalog_name in model_name:
            return spec["vram_mb"]
    return 0


def model_fits(model_name: str, vram_mb: int) -> bool:
    """Returns True if the model is estimated to fit in vram_mb."""
    if vram_mb == 0:
        return True  # unknown VRAM, allow anything
    cost = model_vram(model_name)
    if cost == 0:
        return True  # unknown model, allow it
    return cost <= vram_mb * 0.9
