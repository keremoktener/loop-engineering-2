"""Phase 2 — PLAN. The swarm debates, then the work is broken into issues.

Faithful to the video: a GitHub Discussion is opened where the architect and the
specialist engineers argue the design. The agreed design is decomposed into
phases → tasks → ownership, and each task becomes a GitHub issue (a unit of work
with an inbox of comments).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .codex import Engine
from .config import Config
from .github import GitHub
from .memory import MemoryStore, Note
from .spec import loop_dir, spec_path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"


def plan_path(cfg: Config) -> Path:
    return loop_dir(cfg) / "plan.md"


def tasks_path(cfg: Config) -> Path:
    return loop_dir(cfg) / "tasks.json"


def _roster(cfg: Config) -> str:
    return "\n".join(
        f"- `{k}` — {v.get('name', k)} (owns: {', '.join(v.get('owns', []))})"
        for k, v in cfg.agents.items()
    )


def build_plan(cfg: Config, dry_run: bool = False) -> tuple[Path, list[dict]]:
    spec = spec_path(cfg).read_text() if spec_path(cfg).exists() else ""
    if not spec:
        raise SystemExit("No spec found. Run:  loopengine spec \"<intent>\"  first.")

    gh = GitHub(cfg.github, cfg.repo_slug, dry_run=dry_run)
    a_name, a_cfg = cfg.engine_for("architect")
    engine = Engine(a_cfg, cfg.workdir, dry_run=dry_run, name=a_name)
    architect = (ROOT / "prompts" / "architect.md").read_text()
    plan_tmpl = (TEMPLATES / "plan.md").read_text()

    # pull prior decisions/conventions so the architect builds on past lessons
    mem = MemoryStore(cfg.memory_cfg, cfg.workdir)
    prior = mem.retrieve(spec[:400], types={"decision", "convention", "pattern"}, top_k=5)
    mem_block = mem.render_block(prior)

    # 1) Open the debate room.
    disc_ref = gh.create_discussion(
        title="Design debate — see spec",
        body=("Architect + engineers: debate the design for this spec, surface "
              "risks and trade-offs, converge on an approach.\n\n---\n\n" + spec),
        category=cfg.github.get("discussion_category", "Ideas"),
    )

    # 2) Architect proposes the plan + task breakdown as JSON.
    prompt = f"""{architect}

You are the Architect leading a design debate (reference: {disc_ref}).
{mem_block}
Given the SPEC, produce TWO things:

(A) A human-readable plan following the template.
(B) A machine-readable task list.

Available specialists to assign work to:
{_roster(cfg)}

# SPEC
{spec}

# PLAN TEMPLATE (fill it)
{plan_tmpl}

After the plan, output a fenced ```json block EXACTLY like:
```json
{{"tasks":[
  {{"title":"...","area":["backend","auth"],"phase":1,"depends_on":[],"detail":"..."}}
]}}
```
Rules: 5–15 tasks, ordered by phase, small enough for one agent turn each,
`area` tags drawn from the specialists' `owns` domains.
"""
    if dry_run:
        plan_md = plan_tmpl
        tasks = [
            {"title": "Define DB schema + migrations", "area": ["backend", "db"],
             "phase": 1, "depends_on": [], "detail": "Tables for users, sessions."},
            {"title": "Build auth API (/login,/refresh)", "area": ["backend", "auth", "api"],
             "phase": 1, "depends_on": [0], "detail": "JWT issue + refresh."},
            {"title": "Login UI + session state", "area": ["frontend", "ui"],
             "phase": 2, "depends_on": [1], "detail": "Form, error states, guard."},
            {"title": "Wire MCP connector for provider X", "area": ["mcp", "integration"],
             "phase": 2, "depends_on": [], "detail": "Expose as tool."},
            {"title": "CI + deploy pipeline", "area": ["infra", "ci", "deploy"],
             "phase": 3, "depends_on": [1, 2], "detail": "Lint, test, build, ship."},
        ]
    else:
        result = engine.run(prompt)
        plan_md, tasks = _split_plan_and_tasks(result.output, plan_tmpl)

    # 2b) CTO ratifies the plan (Claude Code) — the "CTO in the discussion".
    if cfg.plan_cfg.get("cto_review", True) and "cto" in cfg.agents:
        plan_md = _cto_ratify(cfg, gh, disc_ref, spec, plan_md, tasks, dry_run)

    plan_path(cfg).write_text(plan_md)
    tasks_path(cfg).write_text(json.dumps({"tasks": tasks, "discussion": disc_ref}, indent=2))

    # 3) Materialise tasks as GitHub issues.
    labels = cfg.labels()
    created = []
    for idx, t in enumerate(tasks):
        body = (f"{t.get('detail','')}\n\n"
                f"**Phase:** {t.get('phase','?')}  \n"
                f"**Area:** {', '.join(t.get('area', []))}  \n"
                f"**Depends on:** {t.get('depends_on', [])}  \n"
                f"_Debate: {disc_ref}_")
        area_labels = [f"area:{a}" for a in t.get("area", [])]
        num = gh.create_task(
            title=f"[P{t.get('phase','?')}] {t['title']}",
            body=body,
            labels=[labels.get("task", "loop:task"),
                    labels.get("status_todo", "status:todo"), *area_labels],
        )
        created.append({**t, "issue": num, "index": idx})

    tasks_path(cfg).write_text(
        json.dumps({"tasks": created, "discussion": disc_ref}, indent=2))

    # record this run's plan as a decision note for future runs to build on
    if mem.enabled and not dry_run:
        all_tags = sorted({a for t in tasks for a in t.get("area", [])})
        spec_title = next((ln[2:].strip() for ln in spec.splitlines()
                           if ln.startswith("# ")), "plan")
        mem.write(Note(
            title=f"Plan: {spec_title}", body=plan_md[:1500],
            type="decision", namespace=mem.default_ns, tags=all_tags,
            task="", outcome="unknown",
        ))

    return plan_path(cfg), created


def _cto_ratify(cfg, gh, disc_ref, spec, plan_md, tasks, dry_run):
    """One Claude Code turn: the CTO reviews the architect's plan, flags risks,
    and signs off. The verdict is appended to the plan."""
    c_name, c_cfg = cfg.engine_for("cto")
    cto_persona = (ROOT / "prompts" / "cto.md").read_text()
    cto_engine = Engine(c_cfg, cfg.workdir, dry_run=dry_run, name=c_name)
    task_lines = "\n".join(
        f"- [P{t.get('phase','?')}] {t.get('title','')} "
        f"(area: {', '.join(t.get('area', []))}, depends_on: {t.get('depends_on', [])})"
        for t in tasks
    )
    prompt = f"""{cto_persona}

You are the CTO reviewing the Architect's plan before any work starts
(debate: {disc_ref}). Judge it against the spec. Be concise.

# SPEC
{spec}

# ARCHITECT'S PLAN
{plan_md}

# PROPOSED TASKS
{task_lines}

Respond with a short ratification block:
- Verdict: APPROVED or CHANGES REQUESTED
- Top risks (max 3)
- Any missing dependency edges or sequencing concerns
- One sentence: is the phasing right?
"""
    if dry_run:
        verdict = ("Verdict: APPROVED (dry-run). Risks: Phase 1 token spend; "
                   "verify auth before UI; confirm migration rollback. Phasing OK.")
    else:
        res = cto_engine.run(prompt)
        verdict = res.output.strip() if res.ok else f"(CTO review failed: {res.error})"
    return plan_md + f"\n\n---\n## CTO ratification ({c_name})\n{verdict}\n"


def _split_plan_and_tasks(output: str, fallback_plan: str) -> tuple[str, list[dict]]:
    m = re.search(r"```json\s*(\{.*?\})\s*```", output, re.DOTALL)
    tasks: list[dict] = []
    if m:
        try:
            tasks = json.loads(m.group(1)).get("tasks", [])
        except Exception:
            tasks = []
    plan_md = output[:m.start()].strip() if m else output.strip()
    if not plan_md:
        plan_md = fallback_plan
    return plan_md, tasks
