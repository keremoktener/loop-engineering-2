"""loopengine — CLI entrypoint.

    loopengine init                      # check tools, create labels
    loopengine spec "<intent>"           # Phase 1  (you)
    loopengine plan                      # Phase 2  (swarm debates → issues)
    loopengine loop                      # Phase 3  (autonomous, no human)
    loopengine verify                    # Phase 4  (you sign off)
    loopengine status                    # where things stand

Add --dry-run to any command to rehearse without calling codex/gh.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .codex import Engine
from .github import GitHub
from .orchestrator import Orchestrator
from .spec import build_spec, spec_path, loop_dir
from .plan import build_plan, plan_path, tasks_path
from .verify import run_verification


def _cfg(args):
    return load_config(args.config)


def cmd_init(args):
    cfg = _cfg(args)
    gh = GitHub(cfg.github, cfg.repo_slug, dry_run=args.dry_run)
    print(f"repo:    {cfg.repo_slug or '(not detected — set repo.slug in config)'}")
    print(f"workdir: {cfg.workdir}")
    for name, ecfg in cfg.engines.items():
        eng = Engine(ecfg, cfg.workdir, dry_run=args.dry_run, name=name)
        print(f"engine:  {name:<12} cli='{ecfg.get('cli','?')}' "
              f"{'found' if eng.available() else 'NOT found on PATH'}")
    # which agent runs on which engine
    for akey, aspec in cfg.agents.items():
        ename, _ = cfg.engine_for(akey)
        print(f"  · {aspec.get('name', akey):<24} → {ename}")
    print(f"gh:      {'authed' if gh.authed() else 'NOT authed (run: gh auth login)'}")
    gh.ensure_labels()
    print("labels:  ensured" if not args.dry_run else "labels:  (dry-run)")


def cmd_spec(args):
    cfg = _cfg(args)
    p = build_spec(cfg, args.intent, dry_run=args.dry_run)
    print(f"✓ spec written → {p}")
    print("  review/edit it, then:  loopengine plan")


def cmd_plan(args):
    cfg = _cfg(args)
    p, tasks = build_plan(cfg, dry_run=args.dry_run)
    print(f"✓ plan written → {p}")
    print(f"✓ {len(tasks)} tasks created as GitHub issues")
    for t in tasks:
        print(f"   #{t.get('issue','?')}  [P{t.get('phase','?')}] {t['title']}")
    if cfg.loop.get("human_gates", {}).get("before_plan_approval", True):
        print("\n⏸  human gate: review the plan + issues, then:  loopengine loop")


def cmd_loop(args):
    cfg = _cfg(args)
    spec = spec_path(cfg).read_text() if spec_path(cfg).exists() else ""
    plan = plan_path(cfg).read_text() if plan_path(cfg).exists() else ""
    if not spec or not plan:
        sys.exit("Missing spec or plan. Run `spec` then `plan` first.")
    orch = Orchestrator(cfg, dry_run=args.dry_run)
    summary = orch.run(spec=spec, plan=plan)
    print("\n── loop summary ──")
    if summary.get("branch"):
        print(f"branch:       {summary['branch']}  (base: {summary.get('base_branch') or '?'})")
    print(f"turns run:    {summary['turns']}")
    print(f"completed:    {summary['completed']}")
    print(f"in review:    (run `loopengine verify` to sign off)")
    print(f"blocked:      {summary['blocked']}")
    print(f"stopped:      {summary['stopped_reason']}")
    print("\nnext:  loopengine verify")


def cmd_verify(args):
    cfg = _cfg(args)
    results = run_verification(cfg, dry_run=args.dry_run)
    print("── verification ──")
    for r in results:
        print(f"  {'✅' if r.ok else '❌'} {r.command}")
    out = loop_dir(cfg) / "verify-report.md"
    print(f"\nreport → {out}")
    if all(r.ok for r in results):
        print("All checks green. Review-status tasks were closed. 🎉")
    else:
        print("Some checks failed — failing tasks bounced back to the loop.")


def cmd_status(args):
    cfg = _cfg(args)
    print(f"repo: {cfg.repo_slug}")
    for label, name in [
        (cfg.labels().get("status_todo"), "todo"),
        (cfg.labels().get("status_doing"), "in-progress"),
        (cfg.labels().get("status_review"), "review"),
        (cfg.labels().get("status_blocked"), "blocked"),
    ]:
        gh = GitHub(cfg.github, cfg.repo_slug, dry_run=args.dry_run)
        tasks = [t for t in gh.list_tasks(cfg.labels().get("task", "loop:task"))
                 if label in t.labels]
        print(f"\n{name} ({len(tasks)}):")
        for t in tasks:
            print(f"  #{t.number}  {t.title}")


def cmd_memory(args):
    cfg = _cfg(args)
    from .memory import MemoryStore
    mem = MemoryStore(cfg.memory_cfg, cfg.workdir)
    if not mem.enabled:
        print("memory is disabled (set memory.enabled: true in config)")
        return
    if args.action == "stats":
        s = mem.stats()
        print(f"vault:   {s['vault']}")
        print(f"total:   {s['total']} notes")
        print(f"by type: {s['by_type']}")
        print(f"outcome: {s['by_outcome']}")
    elif args.action == "search":
        notes = mem.retrieve(args.query or "", top_k=args.k)
        if not notes:
            print("(no matching notes)")
        for n in notes:
            print(f"[{n.type}/{n.outcome} w{n.weight}] {n.title}")
            print(f"   {' '.join(n.body.split())[:160]}")
    elif args.action == "distill":
        out = mem.distill(min_notes=1)
        print(f"distilled → {out}" if out else "(nothing to distill)")
    elif args.action == "prune":
        print(f"pruned {mem.prune()} failed note(s)")


def build_parser() -> argparse.ArgumentParser:
    # Shared flags usable before OR after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="path to config.yaml")
    common.add_argument("--dry-run", action="store_true",
                        help="rehearse without calling codex/gh")

    p = argparse.ArgumentParser(
        prog="loopengine", parents=[common],
        description="Loop engineering: you at the edges, agents in the loop.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common],
                   help="check tools + create labels").set_defaults(fn=cmd_init)

    sp = sub.add_parser("spec", parents=[common],
                        help="Phase 1: build the spec from an intent")
    sp.add_argument("intent", help="one-line description of the epic")
    sp.set_defaults(fn=cmd_spec)

    sub.add_parser("plan", parents=[common],
                   help="Phase 2: swarm debate → plan → issues").set_defaults(fn=cmd_plan)
    sub.add_parser("loop", parents=[common],
                   help="Phase 3: run the autonomous loop").set_defaults(fn=cmd_loop)
    sub.add_parser("verify", parents=[common],
                   help="Phase 4: run checks + human sign-off").set_defaults(fn=cmd_verify)
    sub.add_parser("status", parents=[common],
                   help="show task board").set_defaults(fn=cmd_status)

    mp = sub.add_parser("memory", parents=[common], help="inspect the evolving memory")
    mp.add_argument("action", choices=["stats", "search", "distill", "prune"])
    mp.add_argument("query", nargs="?", default="", help="search text (for `search`)")
    mp.add_argument("-k", type=int, default=8, help="results for `search`")
    mp.set_defaults(fn=cmd_memory)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
