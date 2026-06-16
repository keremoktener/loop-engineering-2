"""Minimal git helpers for branch-per-run isolation."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path


def _git(workdir: Path, *args: str) -> tuple[int, str, str]:
    r = subprocess.run(["git", "-C", str(workdir), *args],
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def current_branch(workdir: Path) -> str:
    code, out, _ = _git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
    return out if code == 0 else ""


def is_dirty(workdir: Path) -> bool:
    code, out, _ = _git(workdir, "status", "--porcelain")
    return bool(out.strip())


def slugify(text: str, maxlen: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].strip("-") or "run"


def make_branch_name(prefix: str, label: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return f"{prefix}{stamp}-{slugify(label)}"


def create_branch(workdir: Path, name: str) -> tuple[bool, str]:
    """Create + switch to a new branch. Returns (ok, message)."""
    code, _, err = _git(workdir, "rev-parse", "--git-dir")
    if code != 0:
        return False, "not a git repository — skipping branch isolation"
    # if branch exists, just switch
    exists = _git(workdir, "rev-parse", "--verify", name)[0] == 0
    if exists:
        code, _, err = _git(workdir, "checkout", name)
    else:
        code, _, err = _git(workdir, "checkout", "-b", name)
    if code != 0:
        return False, f"could not create/switch branch: {err}"
    return True, name
