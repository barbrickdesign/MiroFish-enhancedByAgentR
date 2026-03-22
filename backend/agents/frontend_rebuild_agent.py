"""
Frontend Rebuild Agent
Validates the Vue / Vite frontend and triggers a real npm build when the
dist/ output is stale or absent.

Checks performed:
  1. Verify frontend/package.json is valid JSON.
  2. Verify vite.config.js exists and is readable.
  3. Validate that every .vue component is a non-empty UTF-8 file and
     contains the mandatory <template> tag.
  4. Check whether frontend/dist/ exists and is non-empty.
  5. If dist/ is missing OR any .vue file has been modified more recently
     than the newest file in dist/, trigger `npm run build` inside the
     frontend directory and report the outcome with full stdout/stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tag: str, msg: str) -> None:
    print(f"[{_ts()}] [{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. package.json validity
# ---------------------------------------------------------------------------

def _check_package_json() -> dict[str, Any]:
    pj = FRONTEND_DIR / "package.json"
    if not pj.exists():
        _log("FrontendRebuild", "  WARN frontend/package.json not found")
        return {"check": "package.json", "status": "missing", "fixed": False}
    try:
        json.loads(pj.read_text(encoding="utf-8"))
        _log("FrontendRebuild", "  OK  frontend/package.json valid JSON")
        return {"check": "package.json", "status": "ok", "fixed": False}
    except json.JSONDecodeError as exc:
        _log("FrontendRebuild", f"  ERROR frontend/package.json malformed: {exc}")
        return {
            "check": "package.json",
            "status": "malformed_json",
            "fixed": False,
            "detail": str(exc),
        }


# ---------------------------------------------------------------------------
# 2. vite.config.js existence
# ---------------------------------------------------------------------------

def _check_vite_config() -> dict[str, Any]:
    vc = FRONTEND_DIR / "vite.config.js"
    if not vc.exists():
        _log("FrontendRebuild", "  WARN frontend/vite.config.js not found")
        return {"check": "vite.config.js", "status": "missing", "fixed": False}
    if vc.stat().st_size == 0:
        _log("FrontendRebuild", "  WARN frontend/vite.config.js is empty")
        return {"check": "vite.config.js", "status": "empty", "fixed": False}
    _log("FrontendRebuild", "  OK  frontend/vite.config.js exists")
    return {"check": "vite.config.js", "status": "ok", "fixed": False}


# ---------------------------------------------------------------------------
# 3. Vue component validation
# ---------------------------------------------------------------------------

def _check_vue_components() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    vue_files = sorted(FRONTEND_DIR.rglob("*.vue"))
    _log("FrontendRebuild", f"  Scanning {len(vue_files)} .vue component(s)")

    for vf in vue_files:
        parts = vf.parts
        if any(p in parts for p in ("node_modules", "dist")):
            continue
        rel = str(vf.relative_to(REPO_ROOT))
        try:
            content = vf.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log("FrontendRebuild", f"  ERROR reading {rel}: {exc}")
            issues.append({"check": rel, "status": "read_error", "fixed": False, "detail": str(exc)})
            continue

        if not content.strip():
            _log("FrontendRebuild", f"  WARN {rel} is empty")
            issues.append({"check": rel, "status": "empty_component", "fixed": False})
            continue

        if "<template>" not in content:
            _log("FrontendRebuild", f"  WARN {rel} missing <template> block")
            issues.append({"check": rel, "status": "missing_template_tag", "fixed": False})
        else:
            _log("FrontendRebuild", f"  OK  {rel}")

    return issues


# ---------------------------------------------------------------------------
# 4 & 5. dist/ freshness check and optional rebuild
# ---------------------------------------------------------------------------

def _newest_mtime(directory: Path) -> float:
    """Return the mtime of the most-recently modified file under *directory*."""
    mtimes = [f.stat().st_mtime for f in directory.rglob("*") if f.is_file()]
    return max(mtimes) if mtimes else 0.0


def _check_and_rebuild() -> dict[str, Any]:
    src_dir = FRONTEND_DIR / "src"
    dist_dir = FRONTEND_DIR / "dist"

    dist_exists = dist_dir.exists() and any(dist_dir.iterdir())
    if not dist_exists:
        _log("FrontendRebuild", "  dist/ missing or empty — triggering build")
        return _run_build(reason="dist/ missing or empty")

    dist_newest = _newest_mtime(dist_dir)
    src_newest = _newest_mtime(src_dir) if src_dir.exists() else 0.0

    if src_newest > dist_newest:
        src_dt = datetime.fromtimestamp(src_newest, tz=timezone.utc).isoformat()
        dist_dt = datetime.fromtimestamp(dist_newest, tz=timezone.utc).isoformat()
        _log(
            "FrontendRebuild",
            f"  src/ newer than dist/ ({src_dt} > {dist_dt}) — triggering build",
        )
        return _run_build(reason=f"src/ modified at {src_dt}, dist/ last built at {dist_dt}")

    _log("FrontendRebuild", "  dist/ is up-to-date — no build needed")
    return {"check": "dist_freshness", "status": "up_to_date", "fixed": False}


def _run_build(*, reason: str) -> dict[str, Any]:
    """
    Run `npm run build` inside FRONTEND_DIR.
    Returns a structured result with returncode, stdout, stderr.
    """
    npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
    cmd = [npm_cmd, "run", "build"]

    _log("FrontendRebuild", f"  Running: {' '.join(cmd)} in {FRONTEND_DIR}")
    start = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(FRONTEND_DIR),
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes
        )
    except FileNotFoundError:
        _log("FrontendRebuild", "  ERROR npm not found — is Node.js installed?")
        return {
            "check": "build",
            "status": "npm_not_found",
            "fixed": False,
            "reason": reason,
            "detail": "npm executable not found",
        }
    except subprocess.TimeoutExpired:
        _log("FrontendRebuild", "  ERROR build timed out after 300 s")
        return {
            "check": "build",
            "status": "timeout",
            "fixed": False,
            "reason": reason,
        }

    elapsed = time.monotonic() - start

    if proc.returncode == 0:
        _log("FrontendRebuild", f"  BUILD SUCCESS in {elapsed:.1f}s")
        # Report file count in dist/
        dist_dir = FRONTEND_DIR / "dist"
        dist_files = list(dist_dir.rglob("*")) if dist_dir.exists() else []
        return {
            "check": "build",
            "status": "success",
            "fixed": True,
            "reason": reason,
            "elapsed_s": round(elapsed, 1),
            "dist_files": len([f for f in dist_files if f.is_file()]),
            "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
        }
    else:
        _log(
            "FrontendRebuild",
            f"  BUILD FAILED (exit {proc.returncode}) in {elapsed:.1f}s\n"
            f"{proc.stderr[-1000:]}",
        )
        return {
            "check": "build",
            "status": "failed",
            "fixed": False,
            "reason": reason,
            "returncode": proc.returncode,
            "elapsed_s": round(elapsed, 1),
            "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run() -> dict[str, Any]:
    """Run all frontend-rebuild checks and return a structured summary."""
    if not FRONTEND_DIR.exists():
        _log("FrontendRebuild", f"frontend/ directory not found at {FRONTEND_DIR}")
        return {
            "agent": "frontendRebuildAgent",
            "timestamp": _ts(),
            "status": "frontend_dir_missing",
            "details": [],
        }

    _log("FrontendRebuild", "Starting frontend rebuild validation")

    details: list[dict[str, Any]] = []

    details.append(_check_package_json())
    details.append(_check_vite_config())
    details += _check_vue_components()
    details.append(_check_and_rebuild())

    def _is_warning(detail: dict[str, Any]) -> bool:
        return not detail.get("fixed") and detail.get("status") not in (
            "ok", "up_to_date", "success", "server_not_running"
        )

    fixed = [d for d in details if d.get("fixed")]
    ok = [d for d in details if d.get("status") == "ok"]
    warn = [d for d in details if _is_warning(d)]

    summary = {
        "agent": "frontendRebuildAgent",
        "timestamp": _ts(),
        "checks": len(details),
        "ok": len(ok),
        "fixed": len(fixed),
        "warnings": len(warn),
        "details": details,
    }

    _log(
        "FrontendRebuild",
        f"Done — {len(details)} checks, {len(ok)} OK, {len(fixed)} fixed, {len(warn)} warnings",
    )
    return summary

# ---------------------------------------------------------------------------
# Focused entry-points called by self_healing_agent
# ---------------------------------------------------------------------------

def run_component_validation() -> dict[str, Any]:
    """
    Validate Vue components only (no build trigger).
    Used by the repairFrontend step.
    """
    if not FRONTEND_DIR.exists():
        return {"agent": "frontendComponentValidation", "timestamp": _ts(), "status": "frontend_dir_missing"}
    _log("FrontendRebuild", "Component-only validation starting")
    details: list[dict[str, Any]] = []
    details.append(_check_package_json())
    details += _check_vue_components()
    ok = sum(1 for d in details if d.get("status") == "ok")
    issues = [d for d in details if d.get("status") not in ("ok",)]
    _log("FrontendRebuild", f"Component validation done — {ok} OK, {len(issues)} issues")
    return {"agent": "repairFrontend", "timestamp": _ts(), "ok": ok, "issues": len(issues), "details": details}


def run_config_check() -> dict[str, Any]:
    """
    Validate frontend config files only (package.json, vite.config.js).
    Used by the validateFrontend step.
    """
    _log("FrontendRebuild", "Config-only validation starting")
    details = [_check_package_json(), _check_vite_config()]
    ok = sum(1 for d in details if d.get("status") == "ok")
    _log("FrontendRebuild", f"Config validation done — {ok}/{len(details)} OK")
    return {"agent": "validateFrontend", "timestamp": _ts(), "ok": ok, "details": details}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
