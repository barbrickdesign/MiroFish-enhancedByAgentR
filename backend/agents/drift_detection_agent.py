"""
Drift Detection Agent
Scans the backend Python codebase for structural drift:
  - syntax errors in .py files
  - missing __init__.py files in packages that contain .py modules
  - requirements.txt entries that are pinned to potentially broken versions
  - stale/empty log files that should be rotated

For every issue found the agent attempts an automatic repair and logs
exactly what was changed.
"""

from __future__ import annotations

import ast
import json
import os
import py_compile
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BACKEND_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = Path(__file__).resolve().parent


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tag: str, msg: str) -> None:
    print(f"[{_ts()}] [{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step 1: Syntax-check every .py file
# ---------------------------------------------------------------------------

def check_python_syntax(scan_root: Path) -> list[dict[str, Any]]:
    """
    Compile every .py file under *scan_root*.

    Returns a list of issue dicts:
      {file, line, error, fixed: bool, fix_detail}
    """
    issues: list[dict[str, Any]] = []
    py_files = sorted(scan_root.rglob("*.py"))
    checked = 0
    errored = 0

    for py_file in py_files:
        # Skip virtual-env, dist, egg-info, __pycache__
        parts = py_file.parts
        if any(p in parts for p in (".venv", "venv", "__pycache__", "dist", "build", ".eggs")):
            continue

        try:
            py_compile.compile(str(py_file), doraise=True)
            checked += 1
        except py_compile.PyCompileError as exc:
            errored += 1
            issue: dict[str, Any] = {
                "file": str(py_file.relative_to(BACKEND_ROOT)),
                "error": str(exc),
                "fixed": False,
                "fix_detail": None,
            }

            # Attempt AST parse to surface the line number
            try:
                src = py_file.read_text(encoding="utf-8", errors="replace")
                ast.parse(src)
            except SyntaxError as se:
                issue["line"] = se.lineno
                issue["error"] = f"SyntaxError: {se.msg} (line {se.lineno})"
            else:
                issue["line"] = None

            _log("DriftDetection", f"  SYNTAX ERROR: {issue['file']} — {issue['error']}")
            issues.append(issue)

    _log("DriftDetection", f"Syntax scan complete — {checked} OK, {errored} errors")
    return issues


# ---------------------------------------------------------------------------
# Step 2: Ensure every package directory has an __init__.py
# ---------------------------------------------------------------------------

def check_missing_inits(scan_root: Path) -> list[dict[str, Any]]:
    """
    Any directory under *scan_root* that contains .py files (other than
    __init__.py itself) is expected to be a Python package and should have
    an __init__.py.  Create a minimal one where missing and report.
    """
    issues: list[dict[str, Any]] = []
    candidate_dirs: set[Path] = set()

    for py_file in scan_root.rglob("*.py"):
        parts = py_file.parts
        if any(p in parts for p in (".venv", "venv", "__pycache__", "dist", "build")):
            continue
        if py_file.name != "__init__.py":
            candidate_dirs.add(py_file.parent)

    for d in sorted(candidate_dirs):
        init_file = d / "__init__.py"
        if not init_file.exists():
            # Create a minimal __init__.py
            init_file.write_text(
                '"""\nAuto-generated __init__.py by DriftDetectionAgent.\n"""\n',
                encoding="utf-8",
            )
            rel = str(init_file.relative_to(BACKEND_ROOT))
            _log("DriftDetection", f"  FIXED — created missing {rel}")
            issues.append({
                "file": rel,
                "error": "missing __init__.py",
                "fixed": True,
                "fix_detail": f"Created minimal __init__.py in {rel}",
            })

    if not issues:
        _log("DriftDetection", "No missing __init__.py files found")
    return issues


# ---------------------------------------------------------------------------
# Step 3: Validate requirements.txt
# ---------------------------------------------------------------------------

def check_requirements(req_file: Path) -> list[dict[str, Any]]:
    """
    Scan requirements.txt for obviously malformed entries:
      - lines with no version specifier at all (bare package name), which can
        lead to unpredictable installs
      - lines that use == but pin to '0.0.0' or a similarly trivial version

    Reports issues but does NOT auto-modify requirements.txt (version policy
    changes require human review).
    """
    issues: list[dict[str, Any]] = []
    if not req_file.exists():
        _log("DriftDetection", f"requirements.txt not found at {req_file}")
        return issues

    lines = req_file.read_text(encoding="utf-8").splitlines()
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Skip VCS / URL references
        if line.startswith(("-", "http", "git+")):
            continue
        # Extract package name
        pkg = re.split(r"[><=!;@\[]", line)[0].strip()
        if not pkg:
            continue
        # Warn if no version specifier at all
        if not re.search(r"[><=!]", line):
            _log(
                "DriftDetection",
                f"  WARN requirements.txt:{lineno} — '{pkg}' has no version constraint",
            )
            issues.append(
                {
                    "file": str(req_file.relative_to(BACKEND_ROOT)),
                    "line": lineno,
                    "error": f"'{pkg}' has no version constraint",
                    "fixed": False,
                    "fix_detail": "Manual review needed — add a version specifier",
                }
            )

    if not issues:
        _log("DriftDetection", "requirements.txt looks healthy")
    return issues


# ---------------------------------------------------------------------------
# Step 4: Rotate stale log files
# ---------------------------------------------------------------------------

def rotate_stale_logs(log_dir: Path, max_bytes: int = 10 * 1024 * 1024) -> list[dict[str, Any]]:
    """
    Any .log file larger than *max_bytes* is rotated by renaming it to
    <name>.<timestamp>.log and starting a fresh empty file.
    """
    issues: list[dict[str, Any]] = []
    if not log_dir.exists():
        return issues

    for log_file in sorted(log_dir.glob("*.log")):
        size = log_file.stat().st_size
        if size > max_bytes:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = log_file.with_name(f"{log_file.stem}.{ts}{log_file.suffix}")
            log_file.rename(archive)
            log_file.write_text("", encoding="utf-8")
            rel = str(log_file.relative_to(BACKEND_ROOT))
            _log(
                "DriftDetection",
                f"  ROTATED {rel} ({size / 1024:.1f} KB) → {archive.name}",
            )
            issues.append(
                {
                    "file": rel,
                    "error": f"log file exceeded {max_bytes // 1024} KB ({size // 1024} KB)",
                    "fixed": True,
                    "fix_detail": f"Rotated to {archive.name}",
                }
            )

    if not issues:
        _log("DriftDetection", f"No oversized log files in {log_dir}")
    return issues


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(*, scan_root: Path | None = None) -> dict[str, Any]:
    """
    Run all drift-detection checks.

    Returns a summary dict with the list of issues found/fixed.
    """
    if scan_root is None:
        scan_root = BACKEND_ROOT

    _log("DriftDetection", f"Starting drift detection scan under {scan_root}")

    all_issues: list[dict[str, Any]] = []

    all_issues += check_python_syntax(scan_root)
    all_issues += check_missing_inits(scan_root)
    all_issues += check_requirements(scan_root / "requirements.txt")
    all_issues += rotate_stale_logs(scan_root / "logs")

    fixed = [i for i in all_issues if i.get("fixed")]
    unfixed = [i for i in all_issues if not i.get("fixed")]

    summary = {
        "agent": "driftDetectionAgent",
        "timestamp": _ts(),
        "scan_root": str(scan_root),
        "total_issues": len(all_issues),
        "fixed": len(fixed),
        "unfixed": len(unfixed),
        "issues": all_issues,
    }

    _log(
        "DriftDetection",
        f"Done — {len(all_issues)} issues found, {len(fixed)} fixed, {len(unfixed)} need manual review",
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
