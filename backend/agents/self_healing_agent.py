"""
Self-Healing Agent
Orchestrates all individual repair agents in sequence, collects their
results, writes a structured heal log to backend/logs/heal_log.json,
and prints a human-readable summary.

Healing pipeline (mirrors the Master Loop steps):
  1.  driftDetection         — drift_detection_agent
  2.  repairFrontend         — frontend_rebuild_agent (component validation)
  3.  syncAssets             — copy missing static assets into place
  4.  validateFrontend       — frontend_rebuild_agent (build check)
  5.  updateAgentMesh        — discover & register agent modules
  6.  runSelfHealing         — this module (recursive meta-step, skipped)
  7.  codeGen                — regenerate missing boilerplate files
  8.  filePatch              — file_patch_agent
  9.  frontendRebuild        — frontend_rebuild_agent (force rebuild trigger)
  10. dashboardRepair        — dashboard_repair_agent
  11. validateSchemas        — JSON schema validation
  12. detectProtocolDrift    — API contract checks
  13. meshDiscovery          — log discovered agents
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BACKEND_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = BACKEND_ROOT / "logs"
HEAL_LOG = LOG_DIR / "heal_log.json"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tag: str, msg: str) -> None:
    print(f"[{_ts()}] [{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------

def _run_step(
    step_num: int,
    total: int,
    label: str,
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    _log("SelfHealing", f"Step {step_num}/{total}: {label}")
    start = time.monotonic()
    try:
        result = fn()
    except Exception as exc:
        result = {
            "agent": label,
            "timestamp": _ts(),
            "error": str(exc),
            "status": "exception",
        }
        _log("SelfHealing", f"  ERROR in {label}: {exc}")
    elapsed = time.monotonic() - start
    result["elapsed_s"] = round(elapsed, 3)
    _log("SelfHealing", f"  Finished {label} in {elapsed:.2f}s")
    return result


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def step_drift_detection() -> dict[str, Any]:
    from . import drift_detection_agent
    return drift_detection_agent.run()


def step_repair_frontend() -> dict[str, Any]:
    """Validate Vue component files and report issues (no build trigger)."""
    from . import frontend_rebuild_agent
    return frontend_rebuild_agent.run_component_validation()


def step_validate_frontend() -> dict[str, Any]:
    """Validate frontend config files (package.json, vite.config.js)."""
    from . import frontend_rebuild_agent
    return frontend_rebuild_agent.run_config_check()


def step_frontend_rebuild() -> dict[str, Any]:
    """Run full frontend validation and trigger a build if dist/ is stale."""
    from . import frontend_rebuild_agent
    return frontend_rebuild_agent.run()


def step_sync_assets() -> dict[str, Any]:
    """
    Ensure the frontend/public/ directory contains the icon and any
    other static assets referenced by index.html.  Copy from static/
    if missing.
    """
    repo_root = BACKEND_ROOT.parent
    public_dir = repo_root / "frontend" / "public"
    static_dir = repo_root / "static"

    synced: list[str] = []
    skipped: list[str] = []

    if not static_dir.exists():
        _log("SyncAssets", "  static/ directory not found — skipping asset sync")
        return {
            "agent": "syncAssets",
            "timestamp": _ts(),
            "status": "static_dir_missing",
            "synced": [],
        }

    public_dir.mkdir(parents=True, exist_ok=True)

    for src_file in static_dir.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(static_dir)
        dest_file = public_dir / rel.name  # flatten into public/
        if dest_file.exists():
            skipped.append(str(rel))
        else:
            import shutil
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dest_file))
            synced.append(str(rel))
            _log("SyncAssets", f"  Copied {rel} → public/{rel.name}")

    _log("SyncAssets", f"  Done — synced={len(synced)}, skipped={len(skipped)}")
    return {
        "agent": "syncAssets",
        "timestamp": _ts(),
        "synced": synced,
        "skipped_existing": len(skipped),
    }


def step_update_agent_mesh() -> dict[str, Any]:
    """
    Walk backend/agents/ and collect the list of agent modules.
    Registers them in agents/mesh_registry.json.
    """
    agents_dir = BACKEND_ROOT / "agents"
    registry_file = agents_dir / "mesh_registry.json"

    # Load existing registry
    try:
        if registry_file.exists():
            existing: list[str] = json.loads(registry_file.read_text(encoding="utf-8"))
        else:
            existing = []
    except json.JSONDecodeError:
        existing = []

    discovered: list[str] = []
    for py_file in sorted(agents_dir.glob("*.py")):
        name = py_file.stem
        if name.startswith("_") or name == "master_loop":
            continue
        discovered.append(name)

    new_agents = [a for a in discovered if a not in existing]
    updated_registry = existing + new_agents

    registry_file.write_text(
        json.dumps(updated_registry, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    for ag in new_agents:
        _log("AgentMesh", f"  Registered new agent: {ag}")
    if not new_agents:
        _log("AgentMesh", f"  {len(discovered)} agents already registered")

    return {
        "agent": "updateAgentMesh",
        "timestamp": _ts(),
        "discovered": discovered,
        "newly_registered": new_agents,
        "total": len(updated_registry),
    }


def step_code_gen() -> dict[str, Any]:
    """
    Regenerate any missing boilerplate files:
      - backend/agents/patch_queue.json  (empty queue if absent)
      - backend/agents/patch_history.json (empty history if absent)
    """
    agents_dir = BACKEND_ROOT / "agents"
    generated: list[str] = []

    for fname, default in (
        ("patch_queue.json", "[]"),
        ("patch_history.json", "[]"),
    ):
        fp = agents_dir / fname
        if not fp.exists():
            fp.write_text(default + "\n", encoding="utf-8")
            generated.append(fname)
            _log("CodeGen", f"  Generated missing {fname}")
        else:
            _log("CodeGen", f"  {fname} exists — skipping")

    return {
        "agent": "codeGenAgent",
        "timestamp": _ts(),
        "generated": generated,
        "skipped": 2 - len(generated),
    }


def step_file_patch() -> dict[str, Any]:
    from . import file_patch_agent
    return file_patch_agent.run()


def step_dashboard_repair() -> dict[str, Any]:
    from . import dashboard_repair_agent
    return dashboard_repair_agent.run()


def step_validate_schemas() -> dict[str, Any]:
    """
    Validate that each JSON file in backend/agents/ can be loaded and
    matches a minimum expected structure.
    """
    agents_dir = BACKEND_ROOT / "agents"
    results: list[dict[str, Any]] = []

    for jf in sorted(agents_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            results.append({"file": jf.name, "valid": True, "type": type(data).__name__})
            _log("ValidateSchemas", f"  OK  {jf.name}")
        except json.JSONDecodeError as exc:
            results.append({"file": jf.name, "valid": False, "error": str(exc)})
            _log("ValidateSchemas", f"  FAIL {jf.name}: {exc}")

    return {
        "agent": "validateSchemas",
        "timestamp": _ts(),
        "checked": len(results),
        "valid": sum(1 for r in results if r["valid"]),
        "invalid": sum(1 for r in results if not r["valid"]),
        "details": results,
    }


def step_detect_protocol_drift() -> dict[str, Any]:
    """
    Verify that the Flask API blueprint prefixes defined in app/__init__.py
    match the documented contract (/api/graph, /api/simulation, /api/report).
    """
    app_init = BACKEND_ROOT / "app" / "__init__.py"
    expected = {"/api/graph", "/api/simulation", "/api/report"}
    found: set[str] = set()
    drifted: list[str] = []

    if app_init.exists():
        text = app_init.read_text(encoding="utf-8")
        import re
        for m in re.finditer(r"url_prefix\s*=\s*['\"]([^'\"]+)['\"]", text):
            found.add(m.group(1))

    for prefix in expected:
        if prefix in found:
            _log("ProtocolDrift", f"  OK  {prefix} registered")
        else:
            drifted.append(prefix)
            _log("ProtocolDrift", f"  DRIFT — {prefix} not found in app/__init__.py")

    return {
        "agent": "detectProtocolDrift",
        "timestamp": _ts(),
        "expected": sorted(expected),
        "found": sorted(found),
        "drifted": drifted,
        "status": "ok" if not drifted else "drift_detected",
    }


def step_mesh_discovery() -> dict[str, Any]:
    """
    Log all currently registered agent names.
    """
    agents_dir = BACKEND_ROOT / "agents"
    registry_file = agents_dir / "mesh_registry.json"

    try:
        registered: list[str] = (
            json.loads(registry_file.read_text(encoding="utf-8")) if registry_file.exists() else []
        )
    except json.JSONDecodeError:
        registered = []

    for ag in registered:
        _log("MeshDiscovery", f"  Agent registered: {ag}")

    return {
        "agent": "meshDiscovery",
        "timestamp": _ts(),
        "registered_agents": registered,
        "count": len(registered),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

PIPELINE: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("driftDetection", step_drift_detection),
    ("repairFrontend", step_repair_frontend),
    ("syncAssets", step_sync_assets),
    ("validateFrontend", step_validate_frontend),
    ("updateAgentMesh", step_update_agent_mesh),
    ("codeGen", step_code_gen),
    ("filePatch", step_file_patch),
    ("frontendRebuild", step_frontend_rebuild),
    ("dashboardRepair", step_dashboard_repair),
    ("validateSchemas", step_validate_schemas),
    ("detectProtocolDrift", step_detect_protocol_drift),
    ("meshDiscovery", step_mesh_discovery),
]


def run(*, write_log: bool = True) -> dict[str, Any]:
    """
    Execute the full self-healing pipeline.

    Args:
        write_log: If True, append the result to backend/logs/heal_log.json.

    Returns:
        A top-level summary dict with per-step results.
    """
    total = len(PIPELINE)
    _log("SelfHealing", f"Starting self-healing pipeline ({total} steps)")

    step_results: list[dict[str, Any]] = []
    for idx, (label, fn) in enumerate(PIPELINE, start=1):
        result = _run_step(idx, total, label, fn)
        step_results.append({"step": idx, "label": label, **result})

    summary = {
        "agent": "selfHealingAgent",
        "timestamp": _ts(),
        "steps_run": total,
        "steps": step_results,
    }

    if write_log:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Append to heal_log.json (keep last 50 runs)
        history: list[dict[str, Any]] = []
        if HEAL_LOG.exists():
            try:
                history = json.loads(HEAL_LOG.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                history = []
        history.append(summary)
        history = history[-50:]
        HEAL_LOG.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        _log("SelfHealing", f"Heal log written to {HEAL_LOG}")

    _log("SelfHealing", "Self-healing pipeline complete")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
