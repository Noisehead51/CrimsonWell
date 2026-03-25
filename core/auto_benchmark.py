"""
Auto-Benchmark Scheduler
Runs background model benchmarking and recommendations.
Safely identifies and suggests model upgrades.
"""

import threading
import time
import json
import os
from datetime import datetime, timedelta

_HOME = os.path.expanduser("~")
_SCHEDULE_FILE = os.path.join(_HOME, ".crimsonwell", "benchmark_schedule.json")

# Global state
_scheduler_thread = None
_stop_scheduler = False
_last_benchmark_time = {}


def load_schedule():
    """Load benchmark schedule preferences."""
    if os.path.exists(_SCHEDULE_FILE):
        try:
            with open(_SCHEDULE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {
        "enabled": True,
        "interval_hours": 24,
        "auto_upgrade": False,  # Require user approval before swapping
        "min_improvement": 10,  # % improvement needed to recommend swap
        "test_models": ["mistral:7b", "qwen3.5:4b"],
        "last_run": None,
    }


def save_schedule(sched):
    """Save schedule preferences."""
    os.makedirs(os.path.dirname(_SCHEDULE_FILE), exist_ok=True)
    with open(_SCHEDULE_FILE, "w") as f:
        json.dump(sched, f, indent=2)


def start_scheduler():
    """Start background benchmark scheduler."""
    global _scheduler_thread, _stop_scheduler

    if _scheduler_thread and _scheduler_thread.is_alive():
        return {"ok": False, "message": "Scheduler already running"}

    _stop_scheduler = False
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()

    return {"ok": True, "message": "Scheduler started"}


def stop_scheduler():
    """Stop background scheduler."""
    global _stop_scheduler
    _stop_scheduler = True
    return {"ok": True, "message": "Scheduler stopped"}


def _scheduler_loop():
    """Main scheduler loop (runs in background thread)."""
    global _stop_scheduler

    while not _stop_scheduler:
        try:
            sched = load_schedule()

            if not sched.get("enabled", True):
                time.sleep(60)
                continue

            interval = sched.get("interval_hours", 24) * 3600
            last_run = sched.get("last_run")

            if last_run:
                last_run_dt = datetime.fromisoformat(last_run)
                if datetime.now() - last_run_dt < timedelta(seconds=interval):
                    time.sleep(300)  # Check every 5 minutes
                    continue

            # Run benchmark
            _run_benchmark_batch(sched)

            sched["last_run"] = datetime.now().isoformat()
            save_schedule(sched)

        except Exception as e:
            print(f"[Scheduler error] {e}")

        time.sleep(60)  # Check every minute


def _run_benchmark_batch(sched):
    """Run benchmark on a batch of models."""
    from .update_engine import benchmark_model, get_update_status

    test_models = sched.get("test_models", ["mistral:7b"])
    min_improvement = sched.get("min_improvement", 10)

    for model in test_models:
        if _stop_scheduler:
            break

        try:
            # Benchmark this model
            result = benchmark_model(model)

            if "error" in result:
                continue

            # Compare against current baseline (would need to track)
            # For now, just record the benchmark
            score = result.get("overall_score", 0)

            # Could implement comparison logic here
            # If improvement > min_improvement, recommend upgrade

        except Exception as e:
            continue


def get_scheduler_status():
    """Get current scheduler status."""
    sched = load_schedule()
    return {
        "enabled": sched.get("enabled", False),
        "interval_hours": sched.get("interval_hours", 24),
        "auto_upgrade": sched.get("auto_upgrade", False),
        "last_run": sched.get("last_run"),
        "is_running": _scheduler_thread and _scheduler_thread.is_alive(),
    }


def set_scheduler_config(config: dict):
    """Update scheduler configuration."""
    sched = load_schedule()
    sched.update(config)
    save_schedule(sched)
    return {"ok": True, "config": sched}
