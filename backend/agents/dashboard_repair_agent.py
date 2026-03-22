"""
Dashboard Repair Agent
Validates key backend artefacts that support the live dashboards and
performs automatic repairs where possible:

  1. config.py integrity — parse the file as valid Python; report any
     SyntaxError with file + line detail.
  2. Flask API route files — verify each api/*.py compiles cleanly.
  3. JSON config/heartbeat files — parse every .json under backend/;
     repair malformed files by writing a safe empty-object fallback.
  4. Missing upload / log directories — create them with correct permissions.
  5. Health-endpoint smoke-test — if the Flask server appears to be running
     on localhost, hit /health and report its status.
"""

from __future__ import annotations

import ast
import json
import os
import py_compile
import socket
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_ROOT / "app"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tag: str, msg: str) -> None:
    print(f"[{_ts()}] [{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. Validate config.py
# ---------------------------------------------------------------------------

def _check_config_py() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    cfg = APP_DIR / "config.py"
    if not cfg.exists():
        issues.append({
            "check": "config.py",
            "status": "missing",
            "fixed": False,
            "detail": f"config.py not found at {cfg}",
        })
        _log("DashboardRepair", "  WARN config.py not found")
        return issues

    try:
        py_compile.compile(str(cfg), doraise=True)
        _log("DashboardRepair", "  OK config.py syntax valid")
    except py_compile.PyCompileError as exc:
        try:
            src = cfg.read_text(encoding="utf-8", errors="replace")
            ast.parse(src)
        except SyntaxError as se:
            line = se.lineno
        else:
            line = None
        issues.append({
            "check": "config.py",
            "status": "syntax_error",
            "line": line,
            "fixed": False,
            "detail": str(exc),
        })
        _log("DashboardRepair", f"  ERROR config.py syntax error at line {line}: {exc}")
    return issues


# ---------------------------------------------------------------------------
# 2. Compile API route files
# ---------------------------------------------------------------------------

def _check_api_routes() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    api_dir = APP_DIR / "api"
    if not api_dir.is_dir():
        _log("DashboardRepair", f"  WARN api/ directory not found at {api_dir}")
        return issues

    for py_file in sorted(api_dir.glob("*.py")):
        try:
            py_compile.compile(str(py_file), doraise=True)
            _log("DashboardRepair", f"  OK  api/{py_file.name}")
        except py_compile.PyCompileError as exc:
            try:
                ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as se:
                line = se.lineno
            else:
                line = None
            issues.append({
                "check": f"api/{py_file.name}",
                "status": "syntax_error",
                "line": line,
                "fixed": False,
                "detail": str(exc),
            })
            _log("DashboardRepair", f"  ERROR api/{py_file.name} line {line}: {exc}")
    return issues


# ---------------------------------------------------------------------------
# 3. Validate & repair JSON files
# ---------------------------------------------------------------------------

def _check_json_files(root: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    json_files = list(root.rglob("*.json"))
    _log("DashboardRepair", f"  Scanning {len(json_files)} JSON file(s)")

    for jf in sorted(json_files):
        # Skip node_modules, .venv, package-lock files (usually huge & machine-generated)
        parts = jf.parts
        if any(p in parts for p in ("node_modules", ".venv", "venv", "dist", "__pycache__")):
            continue
        if jf.name in ("package-lock.json", "uv.lock"):
            continue

        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
            json.loads(text)
        except json.JSONDecodeError as exc:
            rel = str(jf.relative_to(BACKEND_ROOT))
            # Repair: overwrite with safe empty object so downstream code
            # doesn't crash when it tries to load this file
            backup = jf.with_suffix(".json.bak")
            backup.write_text(text, encoding="utf-8")
            jf.write_text("{}\n", encoding="utf-8")
            _log(
                "DashboardRepair",
                f"  REPAIRED malformed JSON {rel} "
                f"(original backed up as {backup.name}): {exc}",
            )
            issues.append({
                "check": rel,
                "status": "malformed_json",
                "fixed": True,
                "detail": f"JSON decode error at pos {exc.pos}: {exc.msg}. "
                          f"Replaced with {{}} and backed up original.",
            })
        except OSError as exc:
            rel = str(jf.relative_to(BACKEND_ROOT))
            _log("DashboardRepair", f"  ERROR reading {rel}: {exc}")
            issues.append({
                "check": rel,
                "status": "read_error",
                "fixed": False,
                "detail": str(exc),
            })

    if not any(i["status"] == "malformed_json" for i in issues):
        _log("DashboardRepair", "  All JSON files are well-formed")
    return issues


# ---------------------------------------------------------------------------
# 4. Ensure required directories exist
# ---------------------------------------------------------------------------

REQUIRED_DIRS = [
    "logs",
    "uploads",
    "uploads/simulations",
]


def _ensure_directories() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for rel in REQUIRED_DIRS:
        d = BACKEND_ROOT / rel
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            _log("DashboardRepair", f"  CREATED missing directory: {rel}/")
            issues.append({
                "check": f"directory:{rel}",
                "status": "missing",
                "fixed": True,
                "detail": f"Created {d}",
            })
        else:
            _log("DashboardRepair", f"  OK  directory {rel}/ exists")
    return issues


# ---------------------------------------------------------------------------
# 5. Health-endpoint smoke-test
# ---------------------------------------------------------------------------

def _health_check(host: str = "127.0.0.1", port: int = 5001) -> dict[str, Any]:
    url = f"http://{host}:{port}/health"
    # First check if port is open (non-blocking)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((host, port))
        except (ConnectionRefusedError, socket.timeout, OSError):
            _log("DashboardRepair", f"  INFO Flask server not running on {url} (port closed)")
            return {"check": "health_endpoint", "status": "server_not_running", "fixed": False}

    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            _log("DashboardRepair", f"  OK  /health → {body}")
            return {
                "check": "health_endpoint",
                "status": "ok",
                "fixed": False,
                "detail": body,
            }
    except urllib.error.HTTPError as exc:
        _log("DashboardRepair", f"  WARN /health returned HTTP {exc.code}")
        return {
            "check": "health_endpoint",
            "status": f"http_error_{exc.code}",
            "fixed": False,
            "detail": str(exc),
        }
    except Exception as exc:
        _log("DashboardRepair", f"  WARN /health unreachable: {exc}")
        return {
            "check": "health_endpoint",
            "status": "unreachable",
            "fixed": False,
            "detail": str(exc),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run() -> dict[str, Any]:
    """
    Run all dashboard-repair checks and return a structured summary.
    """
    _log("DashboardRepair", "Starting dashboard repair scan")

    all_issues: list[dict[str, Any]] = []

    all_issues += _check_config_py()
    all_issues += _check_api_routes()
    all_issues += _check_json_files(BACKEND_ROOT)
    all_issues += _ensure_directories()

    health = _health_check()
    all_issues.append(health)

    fixed = [i for i in all_issues if i.get("fixed")]
    unfixed = [i for i in all_issues if not i.get("fixed")]

    summary = {
        "agent": "dashboardRepairAgent",
        "timestamp": _ts(),
        "total_checks": len(all_issues),
        "fixed": len(fixed),
        "issues_remaining": len([i for i in unfixed if i.get("status") not in ("ok", "server_not_running")]),
        "details": all_issues,
    }

    _log(
        "DashboardRepair",
        f"Done — {len(all_issues)} checks, {len(fixed)} fixed",
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
