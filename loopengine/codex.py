"""Thin wrapper around the Codex CLI for headless (non-interactive) agent turns.

Each agent "turn" is one `codex exec` invocation: persona + context in, a
unified diff / status report out. Keep the exact flags in config.yaml so this
survives codex version drift.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TurnResult:
    ok: bool
    output: str
    error: str = ""
    returncode: int = 0


class Engine:
    """Runs one agent turn through a configured CLI (Codex, Claude Code, …).

    Engine-agnostic: it just renders an argv template and feeds the prompt via
    stdin (or as the final arg). Which CLI it is comes entirely from config."""

    def __init__(self, engine_cfg: dict[str, Any], workdir: Path,
                 dry_run: bool = False, name: str = "engine"):
        self.cfg = engine_cfg
        self.workdir = workdir
        self.dry_run = dry_run
        self.name = name

    def available(self) -> bool:
        return shutil.which(self.cfg.get("cli", "codex")) is not None

    def _build_argv(self) -> list[str]:
        model = self.cfg.get("model", "gpt-5-codex")
        argv: list[str] = []
        for tok in self.cfg.get("exec_template", []):
            argv.append(
                tok.replace("{model}", str(model))
                   .replace("{workdir}", str(self.workdir))
            )
        return argv

    def run(self, prompt: str) -> TurnResult:
        """Run a single agent turn. In dry-run, echo the plan instead of executing."""
        argv = self._build_argv()
        via_stdin = bool(self.cfg.get("prompt_via_stdin", True))
        if not via_stdin:
            argv = argv + [prompt]

        if self.dry_run:
            preview = prompt.strip().splitlines()
            head = "\n".join(preview[:14])
            return TurnResult(
                ok=True,
                output=(
                    "[DRY-RUN] would invoke:\n  "
                    + " ".join(argv if via_stdin else argv[:-1])
                    + ("  < (prompt via stdin)" if via_stdin else "")
                    + "\n--- prompt head ---\n" + head
                    + ("\n... (truncated)" if len(preview) > 14 else "")
                ),
            )

        if not self.available():
            return TurnResult(
                ok=False, output="",
                error=f"'{self.cfg.get('cli','codex')}' ({self.name}) not found on "
                      f"PATH. Install it or run with --dry-run.",
                returncode=127,
            )

        try:
            proc = subprocess.run(
                argv,
                input=prompt if via_stdin else None,
                text=True,
                capture_output=True,
                timeout=int(self.cfg.get("timeout_seconds", 1800)),
                cwd=str(self.workdir),
            )
        except subprocess.TimeoutExpired:
            return TurnResult(ok=False, output="", error="codex turn timed out", returncode=124)

        return TurnResult(
            ok=(proc.returncode == 0),
            output=proc.stdout or "",
            error=proc.stderr or "",
            returncode=proc.returncode,
        )


# Back-compat alias (the kit was Codex-only at first).
CodexEngine = Engine
