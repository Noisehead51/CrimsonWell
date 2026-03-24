"""
Usage learner for CrimsonWell.
Tracks which models and intents you use most.
Pre-warms the top model on startup so your first query is instant.
Learns over time — the more you use it, the smarter the pre-loading gets.
"""
import json, os, time, threading, urllib.request

LEARN_FILE = os.path.join(os.path.expanduser("~"), ".crimsonwell", "usage.json")
OLLAMA = "http://localhost:11434"


def _load() -> dict:
    try:
        os.makedirs(os.path.dirname(LEARN_FILE), exist_ok=True)
        with open(LEARN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"models": {}, "intents": {}, "last_model": ""}


def _save(data: dict):
    try:
        os.makedirs(os.path.dirname(LEARN_FILE), exist_ok=True)
        with open(LEARN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def record(intent: str, model: str):
    """Record a usage event."""
    data = _load()
    data["models"][model] = data["models"].get(model, 0) + 1
    data["intents"][intent] = data["intents"].get(intent, 0) + 1
    data["last_model"] = model
    _save(data)


def get_top_model(n: int = 1) -> list:
    """Return the n most-used models."""
    data = _load()
    ranked = sorted(data["models"].items(), key=lambda x: -x[1])
    return [m for m, _ in ranked[:n]]


def get_last_model() -> str:
    return _load().get("last_model", "")


def prewarm(model: str):
    """Send a tiny request to load the model into VRAM in the background."""
    def _do():
        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "options": {"num_predict": 1}
            }).encode()
            req = urllib.request.Request(
                f"{OLLAMA}/api/chat", data=payload,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=60)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def prewarm_top():
    """Pre-warm the most-used model on startup (non-blocking)."""
    top = get_top_model(1)
    if top:
        prewarm(top[0])
