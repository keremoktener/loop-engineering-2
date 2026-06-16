"""Phase 4 — VERIFY. You're back in the loop.

Runs the project's own checks against the work the swarm produced and writes a
report. This is the human gate: nothing the swarm marked 'review' is considered
done until verification passes and you sign off.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .github import GitHub
from .memory import MemoryStore
from .spec import loop_dir


@dataclass
class CheckResult:
    command: str
    ok: bool
    output: str


def run_verification(cfg: Config, dry_run: bool = False) -> list[CheckResult]:
    commands = (cfg.verify.get("commands") or [])
    fail_fast = bool(cfg.verify.get("fail_fast", False))
    run_info = _run_info(cfg)
    results: list[CheckResult] = []
    for cmd in commands:
        if dry_run:
            results.append(CheckResult(cmd, True, "[dry-run] not executed"))
            continue
        proc = subprocess.run(cmd, shell=True, cwd=str(cfg.workdir),
                              capture_output=True, text=True)
        ok = proc.returncode == 0
        tail = (proc.stdout + proc.stderr)[-1500:]
        results.append(CheckResult(cmd, ok, tail))
        if not ok and fail_fast:
            break

    _write_report(cfg, results, run_info)
    _post_review_summary(cfg, results, dry_run)
    _print_merge_hint(results, run_info)
    return results


def _run_info(cfg: Config) -> dict:
    path = loop_dir(cfg) / "run.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _print_merge_hint(results: list[CheckResult], run_info: dict) -> None:
    branch = run_info.get("branch")
    base = run_info.get("base_branch") or "main"
    if branch and all(r.ok for r in results):
        print(f"\nBranch '{branch}' is green. To merge:\n"
              f"  git checkout {base} && git merge --no-ff {branch}")


def _write_report(cfg: Config, results: list[CheckResult], run_info: dict | None = None) -> Path:
    lines = ["# Verification report\n"]
    if run_info and run_info.get("branch"):
        lines.append(f"_Branch:_ `{run_info['branch']}`  (base: `{run_info.get('base_branch','?')}`)\n")
    for r in results:
        lines.append(f"## {'✅' if r.ok else '❌'} `{r.command}`")
        lines.append("```\n" + r.output.strip() + "\n```\n")
    out = loop_dir(cfg) / "verify-report.md"
    out.write_text("\n".join(lines))
    return out


def _post_review_summary(cfg: Config, results: list[CheckResult], dry_run: bool) -> None:
    gh = GitHub(cfg.github, cfg.repo_slug, dry_run=dry_run)
    labels = cfg.labels()
    passed = all(r.ok for r in results)
    review_label = labels.get("status_review", "status:review")

    mem = MemoryStore(cfg.memory_cfg, cfg.workdir)
    mcfg = cfg.memory_cfg
    reinforce = mem.enabled and mcfg.get("reinforce_on_verify", True) and not dry_run

    for issue in gh.list_tasks(labels.get("task", "loop:task")):
        if review_label in issue.labels:
            verdict = ("✅ verification passed — closing." if passed
                       else "❌ verification failed — see report, returning to loop.")
            gh.comment(issue.number, f"Human verify gate: {verdict}")
            if reinforce:
                mem.reinforce(f"#{issue.number}", "passed" if passed else "failed")
            if passed:
                gh.set_status(issue.number, labels, "status_done")
                gh.close_task(issue.number)
            else:
                gh.set_status(issue.number, labels, "status_todo")

    # keep the memory healthy: drop never-recovered anti-patterns, consolidate.
    if mem.enabled and not dry_run:
        if mcfg.get("prune_failed", True):
            removed = mem.prune()
            if removed:
                print(f"memory: pruned {removed} failed note(s)")
        digest = mem.distill(min_notes=int(mcfg.get("distill_min_notes", 12)))
        if digest:
            print(f"memory: distilled lessons → {digest}")
