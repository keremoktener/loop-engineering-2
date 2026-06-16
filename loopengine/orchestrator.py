"""The autonomous middle loop — the heart of loop engineering.

You define the work (spec) and verify it (verify). This module runs everything
in between with no human in the loop: pick the next ready task, route it to the
right specialist, run a Codex turn, post the result as a comment (the "email"),
update status, route follow-ups, repeat until done, stalled, or capped.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import gitutil
from .agents import build_turn_prompt
from .codex import Engine
from .config import Config
from .github import GitHub, parse_mentions, parse_status
from .memory import MemoryStore, Note
from .spec import loop_dir

# A dependency is "satisfied" for scheduling once its work has been *produced*,
# not once a human has signed off. Otherwise the before_merge gate would stall
# every downstream task mid-run and the swarm could never flow through phases.
PRODUCED_STATUSES = {"review", "done"}
ACTIONABLE_STATUSES = {"todo", "in-progress"}


@dataclass
class TaskView:
    number: int
    title: str
    body: str
    tags: list[str]
    status: str
    phase: int = 99
    depends_on: set[int] = field(default_factory=set)


def _status_of(labels: list[str]) -> str:
    for l in labels:
        if l.startswith("status:"):
            return l.split(":", 1)[1]
    return "todo"


def _tags_of(title: str, body: str, labels: list[str]) -> list[str]:
    tags = [l.split(":", 1)[1] for l in labels if l.startswith("area:")]
    text = (title + " " + body).lower()
    for kw in ["backend", "frontend", "ui", "api", "db", "auth", "mcp", "tools",
               "integration", "infra", "deploy", "ci", "worker", "queue",
               "design", "architecture", "review", "test"]:
        if kw in text:
            tags.append(kw)
    return tags


class Orchestrator:
    def __init__(self, cfg: Config, dry_run: bool = False, verbose: bool = True):
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        self.gh = GitHub(cfg.github, cfg.repo_slug, dry_run=dry_run)
        self.engines = {
            name: Engine(ecfg, cfg.workdir, dry_run=dry_run, name=name)
            for name, ecfg in cfg.engines.items()
        }
        self.labels = cfg.labels()
        self.memory = MemoryStore(cfg.memory_cfg, cfg.workdir)

    def _engine_for(self, agent_key: str) -> Engine:
        name, _ = self.cfg.engine_for(agent_key)
        return self.engines.get(name) or next(iter(self.engines.values()))

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _load_graph(self) -> dict[int, dict]:
        """Map issue number → {phase, depends_on:set[int]} from tasks.json.

        tasks.json stores `depends_on` as task *indices*; we translate those into
        issue numbers via each task's `index`/`issue` fields. Missing file → empty
        graph (deps treated as none, i.e. legacy behaviour)."""
        path = loop_dir(self.cfg) / "tasks.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except Exception:
            return {}
        tasks = data.get("tasks", [])
        idx_to_issue = {t.get("index", i): t.get("issue")
                        for i, t in enumerate(tasks) if t.get("issue")}
        graph: dict[int, dict] = {}
        for i, t in enumerate(tasks):
            issue = t.get("issue")
            if not issue:
                continue
            deps = {idx_to_issue[d] for d in t.get("depends_on", [])
                    if d in idx_to_issue}
            graph[issue] = {"phase": int(t.get("phase", 99)), "depends_on": deps}
        return graph

    def _load_tasks(self, graph: dict[int, dict]) -> list[TaskView]:
        issues = self.gh.list_tasks(self.labels.get("task", "loop:task"))
        views = []
        for i in issues:
            g = graph.get(i.number, {})
            views.append(TaskView(
                number=i.number, title=i.title, body=i.body,
                tags=_tags_of(i.title, i.body, i.labels),
                status=_status_of(i.labels),
                phase=g.get("phase", 99),
                depends_on=set(g.get("depends_on", set())),
            ))
        return views

    def _produced(self, tasks: list[TaskView]) -> set[int]:
        """Issue numbers whose work exists: status review/done, or closed
        (closed issues never appear in the open list, so any dep number we don't
        see among open tasks is treated as produced)."""
        open_numbers = {t.number for t in tasks}
        produced = {t.number for t in tasks if t.status in PRODUCED_STATUSES}
        all_deps = {d for t in tasks for d in t.depends_on}
        produced |= {d for d in all_deps if d not in open_numbers}
        return produced

    def _pick_next(self, tasks: list[TaskView]) -> tuple[TaskView | None, str]:
        """Dependency-aware, cycle-safe selection.

        Returns (task, note). Among tasks whose deps are all produced, prefer
        in-progress over todo, then earlier phase, then lower issue number. If
        actionable tasks remain but none are dependency-ready (a cycle, or all
        deps themselves blocked), fall back to phase/issue order so the loop can
        never deadlock — and flag it."""
        actionable = [t for t in tasks if t.status in ACTIONABLE_STATUSES]
        if not actionable:
            return None, ""
        produced = self._produced(tasks)
        ready = [t for t in actionable if t.depends_on <= produced]
        status_rank = {"in-progress": 0, "todo": 1}
        if ready:
            ready.sort(key=lambda t: (status_rank[t.status], t.phase, t.number))
            return ready[0], ""
        # nothing dependency-ready but work remains → cycle or all-deps-blocked
        actionable.sort(key=lambda t: (t.phase, t.number))
        return actionable[0], "cycle-or-block"

    def _setup_branch(self, spec: str) -> str:
        """Create an isolated branch for this run if configured. Returns name/''."""
        loop_cfg = self.cfg.loop
        if not loop_cfg.get("branch_per_run", False):
            return ""
        prefix = loop_cfg.get("branch_prefix", "loop/")
        label = (spec.strip().splitlines()[0] if spec.strip() else "run")
        label = label.lstrip("# ").replace("Spec:", "").strip()
        name = gitutil.make_branch_name(prefix, label)
        if self.dry_run:
            self.log(f"   (dry-run) would create + switch to branch: {name}")
            return name
        if gitutil.is_dirty(self.cfg.workdir):
            self.log("   ⚠️ working tree is dirty — branch will carry uncommitted changes")
        ok, msg = gitutil.create_branch(self.cfg.workdir, name)
        if ok:
            self.log(f"   ✓ isolated on branch: {msg}")
            return msg
        self.log(f"   ⚠️ branch isolation skipped: {msg}")
        return ""

    def run(self, spec: str, plan: str) -> dict:
        loop_cfg = self.cfg.loop
        max_iter = int(loop_cfg.get("max_iterations", 60))
        max_blocks = int(loop_cfg.get("max_consecutive_blocks", 4))
        poll = int(loop_cfg.get("poll_recent_messages", 8))

        self.gh.ensure_labels()
        graph = self._load_graph()
        base_branch = gitutil.current_branch(self.cfg.workdir) if not self.dry_run else ""
        run_branch = self._setup_branch(spec)
        self._record_run(run_branch, base_branch)

        iterations, consecutive_blocks, cycle_warned = 0, 0, False
        summary = {"turns": 0, "completed": [], "blocked": [],
                   "branch": run_branch, "base_branch": base_branch,
                   "stopped_reason": ""}

        self.log("──── entering autonomous loop ────")
        while iterations < max_iter:
            if self.dry_run:
                tasks = _demo_tasks()
                graph = _demo_graph()
                for t in tasks:  # attach demo graph
                    g = graph.get(t.number, {})
                    t.phase = g.get("phase", t.phase)
                    t.depends_on = set(g.get("depends_on", set()))
            else:
                tasks = self._load_tasks(graph)

            task, note = self._pick_next(tasks)
            if task is None:
                summary["stopped_reason"] = "all tasks resolved"
                break
            if note == "cycle-or-block" and not cycle_warned:
                self.log("   ⚠️ no dependency-ready task (cycle or all deps blocked) "
                         "— falling back to phase/issue order")
                cycle_warned = True

            agent_key = self.cfg.agent_for_tags(task.tags)
            agent_cfg = self.cfg.agents.get(agent_key, {})
            engine = self._engine_for(agent_key)
            dep_note = f" deps={sorted(task.depends_on)}" if task.depends_on else ""
            self.log(f"[iter {iterations+1}] P{task.phase} #{task.number} "
                     f"'{task.title}' → {agent_key} via {engine.name} "
                     f"(status={task.status}{dep_note})")

            inbox = self.gh.recent_comments(task.number, poll)
            mem_query = f"{task.title} {' '.join(task.tags)}"
            mem_notes = self.memory.retrieve(mem_query, top_k=self.memory.top_k)
            mem_block = self.memory.render_block(mem_notes)
            if mem_notes:
                self.log(f"   ↳ memory: injected {len(mem_notes)} note(s)")
            prompt = build_turn_prompt(
                agent_key=agent_key, agent_cfg=agent_cfg,
                spec=spec, plan=plan,
                task_title=task.title, task_body=task.body,
                task_number=task.number, inbox=inbox,
                repo_slug=self.cfg.repo_slug,
                memory_block=mem_block,
            )

            self.gh.set_status(task.number, self.labels, "status_doing")
            result = engine.run(prompt)
            summary["turns"] += 1

            report = result.output if result.ok else f"⚠️ turn failed:\n{result.error}"
            self.gh.comment(
                task.number,
                f"**{agent_cfg.get('name', agent_key)}** ({engine.name}) turn:\n\n{report}")

            # write an episode note tied to this task (reinforced later at verify)
            if result.ok and not self.dry_run:
                self.memory.write(Note(
                    title=task.title, body=_distill_report(result.output),
                    type="episode", namespace=self.memory.default_ns,
                    tags=task.tags, task=f"#{task.number}",
                    outcome="unknown",
                ))

            new_status = parse_status(result.output) or ("review" if result.ok else "blocked")
            mentions = parse_mentions(result.output)

            if new_status == "done":
                if self.cfg.loop.get("human_gates", {}).get("before_merge", True):
                    # human verifies before merge → park in review, don't close
                    self.gh.set_status(task.number, self.labels, "status_review")
                    self.log(f"   ✓ #{task.number} produced → review (awaits verify gate)")
                else:
                    self.gh.set_status(task.number, self.labels, "status_done")
                    self.gh.close_task(task.number)
                    summary["completed"].append(task.number)
                consecutive_blocks = 0
            elif new_status == "blocked":
                self.gh.set_status(task.number, self.labels, "status_blocked")
                summary["blocked"].append(task.number)
                consecutive_blocks += 1
                if mentions:
                    self.gh.comment(task.number,
                                    f"🔁 routing to {' '.join('@agent:'+m for m in mentions)}")
            else:
                self.gh.set_status(task.number, self.labels, "status_review")
                consecutive_blocks = 0

            if consecutive_blocks >= max_blocks:
                summary["stopped_reason"] = f"stalled: {max_blocks} consecutive blocks"
                break

            iterations += 1
            if self.dry_run:
                summary["stopped_reason"] = "dry-run: single illustrative iteration"
                break
            time.sleep(0.5)  # be gentle on the GitHub API

        if not summary["stopped_reason"]:
            summary["stopped_reason"] = f"hit max_iterations={max_iter}"
        self.log(f"──── loop exit: {summary['stopped_reason']} ────")
        return summary

    def _record_run(self, branch: str, base: str) -> None:
        if self.dry_run:
            return
        try:
            (loop_dir(self.cfg) / "run.json").write_text(
                json.dumps({"branch": branch, "base_branch": base}, indent=2))
        except Exception:
            pass


def _distill_report(output: str) -> str:
    """Pull the agent's REPORT block (or a short head) for the memory note body."""
    m = re.search(r"REPORT:\s*(.+?)(?:\n\s*STATUS:|\Z)", output or "", re.DOTALL | re.IGNORECASE)
    text = m.group(1) if m else (output or "")
    return " ".join(text.split())[:600]


def _demo_tasks() -> list[TaskView]:
    return [
        TaskView(number=101, title="Build auth API (/login,/refresh)",
                 body="JWT issue + refresh. See spec §2.",
                 tags=["backend", "api", "auth"], status="todo"),
        TaskView(number=102, title="Login UI + session state",
                 body="Form, error states, guard.",
                 tags=["frontend", "ui"], status="todo"),
        TaskView(number=100, title="Define DB schema + migrations",
                 body="Tables for users, sessions.",
                 tags=["backend", "db"], status="todo"),
    ]


def _demo_graph() -> dict[int, dict]:
    # 100 (schema) ← 101 (auth API) ← 102 (login UI)
    return {
        100: {"phase": 1, "depends_on": set()},
        101: {"phase": 1, "depends_on": {100}},
        102: {"phase": 2, "depends_on": {101}},
    }
