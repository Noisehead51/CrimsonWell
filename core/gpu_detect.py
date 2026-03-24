"""
GPU detection for CrimsonWell.
Supports AMD (Vulkan/ROCm), NVIDIA (CUDA), Intel Arc, and CPU fallback.
Works on Windows via WMI. Linux/macOS fall back gracefully.
"""
import subprocess, re, os, json, sys, urllib.request

def _wmic(query: str) -> str:
    try:
        r = subprocess.run(query, shell=True, capture_output=True, text=True, timeout=8)
        return r.stdout
    except Exception:
        return ""

def get_gpu_info() -> dict:
    """
    Returns:
      name       - GPU display name
      vendor     - 'amd' | 'nvidia' | 'intel' | 'unknown'
      vram_mb    - total VRAM in MB (0 if unknown)
      backend    - suggested Ollama backend hint
      ollama_env - dict of env vars to set before launching Ollama
    """
    info = {"name": "CPU / Unknown GPU", "vendor": "unknown",
            "vram_mb": 0, "backend": "cpu", "ollama_env": {}}

    if sys.platform == "win32":
        raw = _wmic("wmic path win32_VideoController get Name,AdapterRAM /value")
        names, vrams = [], []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("Name=") and line[5:]:
                names.append(line[5:].strip())
            elif line.startswith("AdapterRAM=") and line[11:].isdigit():
                vrams.append(int(line[11:]))

        # Pick the discrete GPU (highest VRAM) or first
        best_idx = vrams.index(max(vrams)) if vrams else 0
        name = names[best_idx] if names else "Unknown"
        vram = vrams[best_idx] if vrams else 0
        vram_mb = vram // (1024 * 1024)

        # WMI sometimes lies (reports 4MB for discrete), cap minimum
        if 0 < vram_mb < 512:
            vram_mb = 0  # unreliable, zero it out

        info["name"] = name
        info["vram_mb"] = vram_mb

    elif sys.platform == "linux":
        # Try lspci for GPU name
        try:
            r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "VGA" in line or "3D" in line or "Display" in line:
                    info["name"] = line.split(":")[-1].strip()
                    break
        except Exception:
            pass

    # Vendor detection from name
    name_lower = info["name"].lower()
    if any(k in name_lower for k in ["amd", "radeon", "rx ", "vega", "rdna", "fiji", "polaris"]):
        info["vendor"] = "amd"
        info["backend"] = "vulkan"
        info["ollama_env"] = {
            "OLLAMA_GPU_OVERHEAD": "0",
        }
    elif any(k in name_lower for k in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"]):
        info["vendor"] = "nvidia"
        info["backend"] = "cuda"
        info["ollama_env"] = {}
    elif any(k in name_lower for k in ["intel", "arc ", "iris", "uhd", "xe "]):
        info["vendor"] = "intel"
        info["backend"] = "vulkan"
        info["ollama_env"] = {}

    if info["vram_mb"] > 100:
        info["backend"] = info["backend"]  # keep
    else:
        # Try to estimate from known GPU names
        vram_hints = {
            "rx 7900": 24576, "rx 7800": 16384, "rx 7700": 12288, "rx 7600": 8192,
            "rx 6950": 16384, "rx 6900": 16384, "rx 6800": 16384, "rx 6750": 12288,
            "rx 6700": 10240, "rx 6650": 8192, "rx 6600": 8192, "rx 6500": 4096,
            "rx 5700": 8192, "rx 5600": 6144, "rx 5500": 4096,
            "rx 580": 8192, "rx 570": 8192, "rx 480": 8192, "rx 470": 8192,
            "rtx 4090": 24576, "rtx 4080": 16384, "rtx 4070": 12288, "rtx 4060": 8192,
            "rtx 3090": 24576, "rtx 3080": 10240, "rtx 3070": 8192, "rtx 3060": 12288,
            "rtx 3050": 8192, "gtx 1080": 8192, "gtx 1070": 8192, "gtx 1060": 6144,
            "arc a770": 16384, "arc a750": 8192, "arc a580": 8192, "arc a380": 6144,
        }
        n = info["name"].lower()
        for key, mb in vram_hints.items():
            if key in n:
                info["vram_mb"] = mb
                break

    return info


def get_ollama_loaded_vram() -> int:
    """Query Ollama /api/ps to estimate how much VRAM is in use (MB)."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/ps")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            total = sum(m.get("size_vram", 0) for m in data.get("models", []))
            return total // (1024 * 1024)
    except Exception:
        return 0
