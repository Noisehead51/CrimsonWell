"""
Intent router for CrimsonWell.
Fast keyword-based routing — no LLM needed, runs in <1ms.
Routes your message to the right agent + system prompt.
"""
import re

# ─── AGENT DEFINITIONS ────────────────────────────────────────────────────────

AGENTS = {
    "code": {
        "name": "Coder",
        "icon": "💻",
        "color": "#3b82f6",
        "keywords": [
            "code", "script", "function", "python", "javascript", "typescript",
            "java", "rust", "c++", "golang", "program", "debug", "bug", "error",
            "import", "class", "def ", "async", "api", "endpoint", "database",
            "sql", "regex", "algorithm", "refactor", "unittest", "test", "flask",
            "django", "fastapi", "react", "vue", "node", "npm", "pip", "bash",
            "powershell", "shell", "dockerfile", "yaml", "json schema", "lint"
        ],
        "system": (
            "You are an expert software engineer. Write clean, correct, well-commented code. "
            "For Python, prefer standard library when possible. "
            "Always include complete, runnable examples — not snippets. "
            "Explain what the code does in plain language before the code block. "
            "Point out edge cases and potential issues."
        ),
    },
    "3d": {
        "name": "3D / Blender",
        "icon": "🧊",
        "color": "#f97316",
        "keywords": [
            "blender", "3d", "mesh", "geometry", "bpy", "object", "render",
            "animate", "animation", "sculpt", "texture", "uv", "material",
            "modifier", "armature", "rigging", "low poly", "lowpoly",
            "subdivision", "bevel", "extrude", "boolean", "stl", "obj",
            "fbx", "gltf", "glb", "cad", "parametric", "solid", "surface",
            "3d print", "model", "scene", "shader", "cycles", "eevee"
        ],
        "system": (
            "You are an expert Blender Python scripter and 3D modeling specialist. "
            "When writing Blender scripts: always start with import bpy, clear the scene, "
            "create geometry, apply materials. Make dimensions variables at the top. "
            "Explain each step in plain language — assume the user is new to scripting. "
            "Format all code in ```python blocks. Scripts must be copy-paste ready."
        ),
    },
    "research": {
        "name": "Research",
        "icon": "🔬",
        "color": "#8b5cf6",
        "keywords": [
            "research", "analyze", "analysis", "explain", "compare", "summarize",
            "summary", "study", "what is", "how does", "why does", "difference between",
            "pros and cons", "advantages", "disadvantages", "history of", "overview",
            "deep dive", "comprehensive", "detailed", "thorough", "investigate",
            "breakdown", "report", "article", "topic", "subject", "learn about"
        ],
        "system": (
            "You are a thorough research assistant. Structure your answers clearly: "
            "1) Key concepts, 2) Detailed explanation, 3) Examples, 4) Summary. "
            "Be accurate and cite your reasoning. For technical topics, include concrete numbers and specs. "
            "Flag anything you're uncertain about."
        ),
    },
    "math": {
        "name": "Math",
        "icon": "🧮",
        "color": "#06b6d4",
        "keywords": [
            "calculate", "calculation", "math", "mathematics", "equation", "formula",
            "solve", "compute", "derivative", "integral", "matrix", "vector",
            "probability", "statistics", "average", "median", "variance", "deviation",
            "algebra", "geometry", "trigonometry", "calculus", "physics", "formula",
            "how much", "how many", "percentage", "ratio", "convert", "units"
        ],
        "system": (
            "You are a precise mathematical assistant. Show your work step by step. "
            "Use clear notation. Double-check arithmetic. "
            "For complex problems, state your assumptions first, then solve methodically. "
            "Give numerical answers with appropriate precision and units."
        ),
    },
    "write": {
        "name": "Writer",
        "icon": "✍️",
        "color": "#10b981",
        "keywords": [
            "write", "writing", "essay", "email", "letter", "article", "blog",
            "document", "report", "content", "copy", "text", "draft", "improve",
            "rewrite", "edit", "proofread", "grammar", "tone", "style",
            "professional", "formal", "casual", "creative", "story", "description",
            "product description", "bio", "resume", "cover letter", "proposal"
        ],
        "system": (
            "You are a skilled professional writer. Adapt tone to the request "
            "(formal, casual, technical, creative). "
            "Be concise — cut filler words. Structure content with clear paragraphs. "
            "For emails: subject line + body. For documents: use headers. "
            "Offer an improved version if the user pastes existing text."
        ),
    },
    "agent": {
        "name": "Agent",
        "icon": "🤖",
        "color": "#ec4899",
        "keywords": [
            "do ", "execute", "run ", "create file", "open ", "launch", "install",
            "download", "automate", "automation", "task", "schedule", "monitor",
            "watch", "notify", "deploy", "build", "compile", "test ", "check",
            "verify", "scan", "find files", "rename", "move files", "backup"
        ],
        "system": (
            "You are an autonomous AI agent. Break the task into clear steps. "
            "State what you're going to do before doing it. "
            "Prefer safe, reversible actions. Ask for confirmation before destructive operations. "
            "Show results after each step."
        ),
    },
    "system": {
        "name": "System",
        "icon": "⚙️",
        "color": "#6366f1",
        "keywords": [
            "system", "install", "debug", "diagnose", "troubleshoot", "fix",
            "process", "service", "driver", "memory", "disk", "cpu", "performance",
            "error", "crash", "freeze", "slow", "battery", "network", "wifi",
            "windows", "version", "update", "reboot", "restart", "shutdown",
            "environment", "variable", "registry", "admin", "elevated"
        ],
        "system": (
            "You are a Windows PC system agent. Help debug, diagnose, and fix system issues. "
            "You can: check system health, install packages, manage services, view processes, "
            "diagnose problems, and run administrative commands. "
            "Always ask before running potentially destructive commands. "
            "Use get_system_info, check_disk, get_processes to diagnose issues. "
            "Explain what you find in clear terms, then recommend fixes."
        ),
    },
    "chat": {
        "name": "Chat",
        "icon": "💬",
        "color": "#6b7280",
        "keywords": [],
        "system": (
            "You are a helpful, knowledgeable AI assistant. Give clear, direct answers. "
            "Be conversational but informative. If the user needs a specific skill (coding, research, etc.), "
            "switch naturally to that mode. "
            "CURRENT DATE: March 25, 2026. Use this for all date/time references. "
            "When user asks for current/recent information, web links, or facts that might have changed, "
            "try starting with [SEARCH: your search query] on its own line. Example: [SEARCH: latest AI news]\n"
            "You can also [FETCH: URL] to read specific webpages."
        ),
    },
}

# ─── ROUTING ─────────────────────────────────────────────────────────────────

def route_intent(message: str) -> dict:
    """
    Classify a message and return the matching agent config + confidence.
    Returns dict with keys: intent, name, icon, color, system, confidence
    """
    msg = message.lower()
    scores = {}

    for intent_id, agent in AGENTS.items():
        if not agent["keywords"]:
            scores[intent_id] = 0
            continue
        # Count keyword hits (longer keywords count more)
        score = sum(len(kw) for kw in agent["keywords"] if kw in msg)
        scores[intent_id] = score

    best_id = max(scores, key=scores.get)
    best_score = scores[best_id]

    if best_score == 0:
        agent = AGENTS["chat"]
        return {**agent, "intent": "chat", "confidence": 0.5}

    total = sum(scores.values()) or 1
    confidence = min(best_score / total, 1.0)

    agent = AGENTS[best_id]
    return {**agent, "intent": best_id, "confidence": confidence}


def needs_clarification(message: str, confidence: float) -> bool:
    """Return True if confidence is low enough to warrant asking ONE question."""
    return confidence < 0.3 and len(message.split()) < 4
