"""
Master Loop
Entry-point that mirrors the 10-step Master Agent Loop described in the
issue logs.  Each step calls the corresponding Python agent and logs
`[MasterLoop] Starting: <step>` / `[MasterLoop] Finished: <step>` around
the actual work so that downstream dashboards can track progress.

Usage:
    python -m backend.agents.master_loop            # run once
    python -m backend.agents.master_loop --loop     # run continuously (60 s interval)
    python backend/agents/master_loop.py --once     # run once (direct script)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# Ensure the repo root is on sys.path when executed as a script
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Individual step wrappers
# ---------------------------------------------------------------------------

def _import_agents():
    """Lazy import to avoid circular issues when used as a module."""
    from backend.agents import (
        drift_detection_agent,
        file_patch_agent,
        frontend_rebuild_agent,
        dashboard_repair_agent,
        self_healing_agent,
    )
    return (
        drift_detection_agent,
        file_patch_agent,
        frontend_rebuild_agent,
        dashboard_repair_agent,
        self_healing_agent,
    )


def _wrap(step_name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    _log(f"[MasterLoop] Starting: {step_name}")
    _log(f"Master Agent Loop: [{step_name}] starting…")
    start = time.monotonic()
    try:
        result = fn()
    except Exception as exc:
        result = {"error": str(exc), "status": "exception"}
        _log(f"Master Agent Loop: [{step_name}] ERROR — {exc}")
    elapsed = time.monotonic() - start
    _log(f"[MasterLoop] Finished: {step_name}")
    _log(f"Master Agent Loop: [{step_name}] complete ✓ ({elapsed:.2f}s)")
    return result


# ---------------------------------------------------------------------------
# The 10 canonical steps
# ---------------------------------------------------------------------------

def run_cycle() -> dict[str, Any]:
    """Execute one full Master Agent Loop cycle and return combined results."""
    (
        drift,
        patch,
        rebuild,
        repair,
        heal,
    ) = _import_agents()

    _log(f"[MasterLoop] Cycle start at {_ts()}")
    _log("Master Agent Loop: starting cycle")

    results: dict[str, Any] = {}

    # Step 1 — heartbeat / cycle start (no external agent)
    _log("[MasterLoop] Starting: heartbeat")
    _log("Master Agent Loop: [1/10] heartbeat starting…")
    results["heartbeat"] = {"timestamp": _ts(), "status": "ok"}
    _log("[MasterLoop] Finished: heartbeat")
    _log("Master Agent Loop: [1/10] heartbeat complete ✓")

    # Step 2 — drift detection
    results["driftDetection"] = _wrap("driftDetection", drift.run)

    # Step 3 — repair frontend (validate vue components, no build trigger)
    results["repairFrontend"] = _wrap("repairFrontend", rebuild.run_component_validation)

    # Step 4 — sync frontend assets
    results["syncAssets"] = _wrap(
        "syncAssets",
        heal.step_sync_assets,
    )

    # Step 5 — validate frontend (build freshness check)
    results["validateFrontend"] = _wrap("validateFrontend", rebuild.run_config_check)

    # Step 6 — update agent mesh
    results["updateAgentMesh"] = _wrap(
        "updateAgentMesh",
        heal.step_update_agent_mesh,
    )

    # Step 7 — self-healing (full pipeline without recursing into master_loop)
    results["runSelfHealing"] = _wrap(
        "runSelfHealing",
        lambda: heal.run(write_log=True),
    )

    # Step 8 — file patch
    results["filePatch"] = _wrap("filePatch", patch.run)

    # Step 9 — frontend rebuild (triggers npm build if stale)
    results["frontendRebuild"] = _wrap("frontendRebuild", rebuild.run)

    # Step 10 — dashboard repair
    results["dashboardRepair"] = _wrap("dashboardRepair", repair.run)

    _log("Master Agent Loop: cycle complete")
    _log(f"[MasterLoop] Cycle end at {_ts()}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MiroFish Master Agent Loop")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously (default interval: 60 s)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit (default behaviour)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between cycles when --loop is active (default: 60)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.loop:
        _log(f"Master Agent Loop: continuous mode, interval={args.interval}s")
        while True:
            try:
                run_cycle()
            except KeyboardInterrupt:
                _log("Master Agent Loop: interrupted by user — exiting")
                sys.exit(0)
            except Exception as exc:
                _log(f"Master Agent Loop: unhandled exception in cycle — {exc}")
            _log(f"Master Agent Loop: sleeping {args.interval}s until next cycle")
            time.sleep(args.interval)
    else:
        result = run_cycle()
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
