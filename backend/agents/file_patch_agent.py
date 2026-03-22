"""
File Patch Agent
Reads pending patch entries from patch_queue.json and applies them as
**real, on-disk writes** — not dry runs.

Patch entry schema (patch_queue.json):
  [
    {
      "id":       "<unique string>",
      "file":     "<path relative to BACKEND_ROOT>",
      "find":     "<literal string to locate>",
      "replace":  "<replacement string>",
      "encoding": "utf-8"          // optional, default utf-8
    },
    ...
  ]

After applying, successfully patched entries are moved to
patch_history.json with an applied_at timestamp and a diff summary.
Entries that cannot be applied (file missing, pattern not found) remain
in the queue and are logged as failures.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = Path(__file__).resolve().parent
QUEUE_FILE = AGENTS_DIR / "patch_queue.json"
HISTORY_FILE = AGENTS_DIR / "patch_history.json"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tag: str, msg: str) -> None:
    print(f"[{_ts()}] [{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _load_queue() -> list[dict[str, Any]]:
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_queue(queue: list[dict[str, Any]]) -> None:
    QUEUE_FILE.write_text(
        json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_history(history: list[dict[str, Any]]) -> None:
    HISTORY_FILE.write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Core patch logic
# ---------------------------------------------------------------------------

def _apply_patch(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Attempt to apply a single patch entry.

    Returns the entry dict enriched with:
      - applied: bool
      - applied_at: ISO timestamp (if applied)
      - failure_reason: str (if not applied)
      - occurrences: int — how many replacements were made
    """
    result = dict(entry)

    target_rel: str = entry.get("file", "")
    find: str = entry.get("find", "")
    replace: str = entry.get("replace", "")
    encoding: str = entry.get("encoding", "utf-8")

    target_path = (BACKEND_ROOT / target_rel).resolve()

    # Security: ensure the resolved path is still within BACKEND_ROOT
    try:
        target_path.relative_to(BACKEND_ROOT)
    except ValueError:
        result["applied"] = False
        result["failure_reason"] = f"Path traversal rejected: {target_rel}"
        _log("FilePatch", f"  REJECTED (path traversal) — {target_rel}")
        return result

    if not target_path.exists():
        result["applied"] = False
        result["failure_reason"] = f"File not found: {target_rel}"
        _log("FilePatch", f"  SKIP (not found) — {target_rel}")
        return result

    if not find:
        result["applied"] = False
        result["failure_reason"] = "Empty 'find' string"
        _log("FilePatch", f"  SKIP (empty find) — {target_rel}")
        return result

    try:
        original = target_path.read_text(encoding=encoding, errors="replace")
    except OSError as exc:
        result["applied"] = False
        result["failure_reason"] = f"Read error: {exc}"
        _log("FilePatch", f"  ERROR reading {target_rel}: {exc}")
        return result

    occurrences = original.count(find)
    if occurrences == 0:
        result["applied"] = False
        result["failure_reason"] = f"Pattern not found in {target_rel}"
        _log("FilePatch", f"  SKIP (pattern not found) — {target_rel}")
        return result

    patched = original.replace(find, replace)

    # Atomic write via temp file in the same directory
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    try:
        tmp_path.write_text(patched, encoding=encoding)
        shutil.move(str(tmp_path), str(target_path))
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        result["applied"] = False
        result["failure_reason"] = f"Write error: {exc}"
        _log("FilePatch", f"  ERROR writing {target_rel}: {exc}")
        return result

    result["applied"] = True
    result["applied_at"] = _ts()
    result["occurrences"] = occurrences
    result["diff_summary"] = (
        f"Replaced {occurrences} occurrence(s) of {find!r} → {replace!r} in {target_rel}"
    )
    _log(
        "FilePatch",
        f"  PATCHED {target_rel} — {occurrences} replacement(s): {find!r} → {replace!r}",
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enqueue(
    file: str,
    find: str,
    replace: str,
    *,
    patch_id: str | None = None,
    encoding: str = "utf-8",
) -> str:
    """
    Add a new patch entry to the queue and return its id.
    Idempotent: if an entry with the same id already exists it is not
    duplicated.
    """
    import uuid

    if patch_id is None:
        patch_id = str(uuid.uuid4())

    queue = _load_queue()
    existing_ids = {e.get("id") for e in queue}
    if patch_id in existing_ids:
        _log("FilePatch", f"Entry {patch_id} already in queue — skipping enqueue")
        return patch_id

    queue.append(
        {
            "id": patch_id,
            "file": file,
            "find": find,
            "replace": replace,
            "encoding": encoding,
            "queued_at": _ts(),
        }
    )
    _save_queue(queue)
    _log("FilePatch", f"Enqueued patch {patch_id} for {file}")
    return patch_id


def run() -> dict[str, Any]:
    """
    Process all entries in patch_queue.json.

    Returns a summary of applied / failed patches.
    """
    queue = _load_queue()
    if not queue:
        _log("FilePatch", "Patch queue is empty — nothing to do")
        return {
            "agent": "filePatchAgent",
            "timestamp": _ts(),
            "applied": 0,
            "failed": 0,
            "patches": [],
        }

    history = _load_history()
    remaining_queue: list[dict[str, Any]] = []
    applied_patches: list[dict[str, Any]] = []
    failed_patches: list[dict[str, Any]] = []

    _log("FilePatch", f"Processing {len(queue)} patch(es) from queue")

    for entry in queue:
        result = _apply_patch(entry)
        if result.get("applied"):
            history.append(result)
            applied_patches.append(result)
        else:
            remaining_queue.append(entry)
            failed_patches.append(result)

    _save_queue(remaining_queue)
    _save_history(history)

    summary = {
        "agent": "filePatchAgent",
        "timestamp": _ts(),
        "applied": len(applied_patches),
        "failed": len(failed_patches),
        "patches": applied_patches + failed_patches,
    }

    _log(
        "FilePatch",
        f"Done — applied={len(applied_patches)}, failed={len(failed_patches)}, "
        f"remaining_in_queue={len(remaining_queue)}",
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
