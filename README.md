# Loop Engineering

> *You're in the loop at the start (spec) and the end (verify). The agents are in the loop in the middle.*

**MIT-licensed CLI kit for coordinating AI coding agents through GitHub issues, discussions, branch isolation, memory, and verification gates.**

A small, runnable implementation of **loop engineering**: define the outcome, let a swarm of specialist agents work through the middle, then verify before anything is merged. The goal is to help developers and open-source maintainers experiment with autonomous coding workflows without losing structure, reviewability, or human control.

Driven by the **Codex CLI**, coordinated through **GitHub issues + discussions** as the agents' inboxes, and designed so the human remains responsible for the spec, verification, and final merge decision.

## The idea, mapped to this kit

| In the video | In this kit |
|---|---|
| Spec the epic with Spec Kit (big work only) | `loopengine spec "<intent>"` → `.loop/spec.md` |
| Voice-describe to Claude Code / Codex | the spec becomes the immovable source of truth |
| Agents open a GitHub **discussion** and debate the design | `loopengine plan` opens a Discussion + an architect-led debate |
| Plan into **phases → tasks → ownership** | the plan is decomposed into GitHub **issues**, one per task, labelled by area/owner |
| Say "go", it runs for hours; **email inboxes** so agents talk to each other | `loopengine loop` — the autonomous middle loop; issue **comments** are the inboxes, `@agent:<key>` mentions are the hand-offs |
| Your only job left: **verify** | `loopengine verify` — runs your checks, gates everything for sign-off |

You are deliberately **out of the loop in the middle**. You touch two things:
the spec at the start and the verification at the end.

## The four phases

```
  ┌── YOU ──┐                  ┌──── SWARM (no human) ────┐         ┌── YOU ──┐
  │  spec   │ ──▶  plan  ──▶   │   loop: pick → route →    │  ──▶    │ verify  │
  │ (Ph 1)  │   (Ph 2,debate)  │  codex turn → comment →   │ (Ph 3)  │ (Ph 4)  │
  └─────────┘                  │  status → repeat …        │         └─────────┘
                               └───────────────────────────┘
```

- **Phase 1 — spec** *(you)*: turn a one-line intent into a structured,
  testable spec. Spec-Kit-flavoured. For epics, not small chores — it costs more
  tokens, exactly as the video warns.
- **Phase 2 — plan** *(swarm debates)*: a GitHub Discussion is opened; the
  architect and engineers argue the design and converge; the result becomes
  phases → tasks → owners, each task a GitHub issue.
- **Phase 3 — loop** *(autonomous)*: the orchestrator repeatedly picks the next
  ready task, routes it to the right specialist, runs a headless `codex exec`
  turn, posts the result as a comment, updates status, and routes hand-offs —
  until everything is resolved, stalled, or capped.
- **Phase 4 — verify** *(you)*: run lint/typecheck/test/build; passing tasks
  close, failing ones bounce back into the loop.

## Quick start

```bash
# 0. deps
pip install -r requirements.txt
./scripts/setup.sh                 # checks codex + gh are installed & authed

# 1. point it at your repo
#    edit config.yaml → repo.slug, repo.workdir, engine.model, verify.commands

# 2. rehearse the whole flow WITHOUT spending anything
python3 -m loopengine spec "Add passwordless email login" --dry-run
python3 -m loopengine plan --dry-run
python3 -m loopengine loop --dry-run
python3 -m loopengine verify --dry-run

# 3. for real
python3 -m loopengine init
python3 -m loopengine spec "Add passwordless email login"
#   → review/edit .loop/spec.md
python3 -m loopengine plan
#   → review the plan + the GitHub issues it created
python3 -m loopengine loop
python3 -m loopengine verify
```

Or the guided driver: `./scripts/loop.sh "Add passwordless email login"`.

## The swarm — and which engine each seat runs on

Two engines are wired in (`config.yaml → engines`), and every agent names the one
it runs through:

| Seat | Engine | Why |
|---|---|---|
| **Architect** | **Codex** | drafts the spec and the task breakdown; runs the debate |
| **CTO** | **Claude Code** | senior authority — ratifies the plan, reviews produced work |
| Backend / Frontend / MCP / Infra engineers | **Codex** | the implementers |

This mirrors the clip: he calls it the "Codex architect" with the engineers in
that same Codex debate, and keeps the **CTO as a distinct senior seat** — here
that seat is Claude Code (point it at your strong model; `model: opus` by
default). The CTO isn't just a reviewer at the end: after the Codex architect
drafts the plan, the Claude Code CTO **ratifies it** before any issue is created
(`plan.cto_review`), and during the loop any `review`/`qa` task routes to it.

**Subagents (the engineers) run on Codex by default.** That's the faithful
choice and the cheaper one — Claude Code is reserved for the high-leverage CTO
judgement. It's one line to change: set `engine: claude_code` on any single agent
(e.g. put the MCP engineer on Claude Code for a gnarly integration) without
touching the others. Add a third engine to `engines:` and assign it the same way.

## Adapting

- **Different Codex / Claude Code flags or version:** edit that engine's
  `exec_template` in `config.yaml` (run `codex exec --help` / `claude --help`).
  Claude Code headless uses `claude -p`; unattended edits need a permissive
  `--permission-mode` (default `acceptEdits`) — sandbox the run externally.
- **Move a seat to a different model:** change that engine's `model`, or give one
  agent its own engine entry.
- **Turn off the CTO plan-ratification turn:** `plan.cto_review: false`.
- **Phase-by-phase verification:** `loop.human_gates.after_each_phase: true`.

## Scheduling — dependency-aware, cycle-safe

The plan records `depends_on` for every task. The loop honours it: a task is only
eligible once **all its dependencies have been produced** (reached `review`/`done`
or been closed). Among eligible tasks it prefers in-progress over fresh, then
earlier phase, then lower issue number — so Phase 1 work lands before the Phase 2
work that needs it, instead of the swarm guessing at interfaces that don't exist
yet.

"Produced" deliberately means *the code exists*, not *the human signed off* —
otherwise the `before_merge` gate would freeze every downstream task mid-run. The
human gate still holds at the end; it just doesn't stall the autonomous middle.

If no task is dependency-ready, the loop selects the earliest phase/issue among
actionable cycle members and reports `cycle:<sorted SCC members>`, including
non-actionable members. Without an actionable cycle member, it selects the
earliest actionable phase/issue and reports `blocked:<sorted unmet dependencies>`.
Self-dependencies count as cycles; produced or closed dependencies add no edges.
Each distinct diagnostic is warned once per run. SCC detection is iterative and
linear in graph size, so deep dependency chains and cycles need no recursion.

## Branch-per-run isolation

Each autonomous run gets its own throwaway branch (`loop/<timestamp>-<slug>`,
configurable). The swarm's edits land there; `master`/`main` stays untouched
while a multi-hour run churns. At the verify gate, if everything's green the kit
prints the exact merge command — you review the diff and merge by hand (honouring
`before_merge`). Re-running on an existing run branch just switches to it, so an
interrupted run resumes in place rather than forking again.

Branch isolation keeps edits reviewable while the scheduler distinguishes cycle
members from blocked dependencies and reports the precise stall reason.

## Memory — a vault that learns across runs

The swarm starts each run a little smarter than the last. Memory is just a folder
of markdown notes (a pattern, a decision, an episode, or a convention each), with
light frontmatter. The loop:

- **retrieves** the few most relevant notes before a turn and injects them into
  the agent's prompt (ranked by tag/keyword match, then by how many times the
  note has proven itself);
- **writes** a new note after each turn and a decision note after each plan;
- **reinforces** at the verify gate — notes tied to a task that passed get a
  weight boost; ones that failed are flagged and (if they never recover) pruned;
- **distills** a namespace into a `lessons.md` digest once it accrues enough notes.

It's plain markdown in a folder, so **open that folder as an Obsidian vault** any
time to browse the graph (notes link via `[[wikilinks]]`), fix a bad note, or
delete one. The app is for you; the files are read directly by the loop, so
nothing depends on Obsidian being open during an autonomous run.

Scope is yours (`config.yaml → memory`): `shared` (`~/.loop/vault` — one brain
across all your projects, the "improves across projects" property) or `per_repo`
(versioned with the code), or point `vault:` at an existing Obsidian vault.
Notes are namespaced per repo; a `global` namespace is always included for
stack-level lessons. Inspect it with `loopengine memory stats | search "<q>" |
distill | prune`.

**Recall** is keyword + light stemming + curated dev-domain synonyms, so a query
about "login sessions" surfaces a note about "token refresh" — no dependency, no
API cost, which at this vault size beats generic embeddings on a bounded code
vocabulary. Extend the clusters via `memory.extra_synonyms`. For a large,
cross-domain vault, an **optional semantic layer** (`memory.embeddings.enabled`,
off by default) embeds notes once (cached by content hash) and blends cosine
similarity into the ranking; if it's off or unavailable, recall falls back to the
lexical scorer.

Two guards, on by design: `top_k` caps how many notes hit each prompt (tokens),
and the reinforce-then-prune cycle keeps a bad memory from quietly poisoning
future runs — the cheapest fix being to delete the note in Obsidian.

## How the "inboxes" work

There's no message broker — GitHub *is* the bus, just like the video's email
setup:
- each **task = an issue**; its **comments = its inbox**.
- an agent turn **reads the last N comments** before acting (`loop.poll_recent_messages`).
- to hand off, an agent writes `@agent:backend-engineer …`; the orchestrator
  parses the mention and keeps the thread moving.
- the design debate lives in a real **Discussion** (GraphQL via `gh`), falling
  back to a pinned issue if Discussions aren't enabled.

## Safety rails (so a multi-hour autonomous run can't run away)

- `loop.max_iterations` — hard stop on total turns.
- `loop.max_consecutive_blocks` — bail if the swarm stalls.
- `loop.branch_per_run` — every run isolated on its own branch; merge is manual.
- dependency-aware scheduler — won't start a task before its prerequisites exist,
  and falls back instead of deadlocking on a bad graph.
- `loop.human_gates.before_merge` — nothing closes without your verify.
- `engine.sandbox` (in `exec_template`) — Codex runs sandboxed; raise to
  `danger-full-access` only if you mean it.
- `--dry-run` — exercise the full state machine with zero external calls.

> Tip: keep autonomous runs on a **branch**. Let the swarm push to it; you review
> the diff at the verify gate.

## Files

```
config.yaml            all tunables (repo, engine, agents, gates, verify)
loopengine/
  cli.py               spec | plan | loop | verify | status | init
  spec.py              Phase 1
  plan.py              Phase 2 (discussion + task issues)
  orchestrator.py      Phase 3 (the autonomous loop + dependency scheduler)
  verify.py            Phase 4
  memory.py            evolving Obsidian-vault memory (retrieve/reinforce/prune)
  gitutil.py           branch-per-run isolation
  codex.py             headless Codex CLI wrapper
  github.py            issues / comments / discussions = the inboxes
  agents.py            persona loading + per-turn prompt assembly
  config.py            config loader + task routing
prompts/               one persona per agent + the operating contract
templates/             spec / plan / discussion skeletons
scripts/               setup.sh, loop.sh
```

---

Built as a faithful, minimal take on loop engineering. It's a starting frame, not
a framework — read the ~700 lines, then bend it to your stack.
