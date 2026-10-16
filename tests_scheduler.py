"""Standalone proof for the dependency scheduler. Run: python3 tests_scheduler.py"""
from __future__ import annotations

from copy import deepcopy
from itertools import permutations
from random import Random
from unittest.mock import patch

from loopengine.codex import TurnResult
from loopengine.config import load_config
from loopengine.orchestrator import Orchestrator, TaskView

def main():
    cfg = load_config("config.yaml")
    orch = Orchestrator(cfg, dry_run=True)
    T = TaskView
    # dep order beats issue-number order
    t, n = orch._pick_next([
        T(100, "UI", "", ["frontend"], "todo", 2, {200}),
        T(200, "API", "", ["backend"], "todo", 1, set())])
    assert t.number == 200 and n == "", "dep order must win"
    # dependent unlocks once dep is produced
    t, n = orch._pick_next([
        T(100, "UI", "", [], "todo", 2, {200}),
        T(200, "API", "", [], "review", 1, set())])
    assert t.number == 100 and n == "", "dependent should unlock at review"
    # cycle fallback identifies the whole component
    t, n = orch._pick_next([
        T(10, "A", "", [], "todo", 1, {11}),
        T(11, "B", "", [], "todo", 1, {10})])
    assert t.number == 10 and n == "cycle:10,11", "cycle must identify its members"
    # a downstream task cannot break the cycle that blocks it
    cycle = [T(5, "Dependent", "", [], "todo", 1, {10}),
             T(10, "A", "", [], "todo", 1, {11}),
             T(11, "B", "", [], "todo", 1, {10})]
    t, n = orch._pick_next(cycle)
    assert t.number == 10 and n == "cycle:10,11", "pick a cycle member, not its dependent"
    # a blocked dependency is distinct from a cycle
    blocked = [T(7, "Dependent", "", [], "todo", 1, {3}),
               T(3, "Blocked", "", [], "blocked", 1, set())]
    t, n = orch._pick_next(blocked)
    assert t.number == 7 and n == "blocked:3", "identify the unmet dependency"
    t, n = orch._pick_next([T(4, "Self", "", [], "todo", 1, {4})])
    assert t.number == 4 and n == "cycle:4", "self-dependency is a cycle"
    # separate cycles must not be combined in the note
    separate = [T(20, "A", "", [], "todo", 1, {21}),
                T(21, "B", "", [], "todo", 1, {22}),
                T(22, "C", "", [], "todo", 1, {20}),
                T(30, "D", "", [], "in-progress", 1, {31}),
                T(31, "E", "", [], "todo", 1, {30})]
    t, n = orch._pick_next(separate)
    assert t.number == 20 and n == "cycle:20,21,22", "report only the selected SCC"
    # branching and cross-component edges must preserve SCC boundaries
    branching = [T(40, "A", "", [], "todo", 1, {41, 42}),
                 T(41, "B", "", [], "todo", 1, {40}),
                 T(42, "C", "", [], "todo", 1, {43}),
                 T(43, "D", "", [], "todo", 1, {42})]
    t, n = orch._pick_next(branching)
    assert t.number == 40 and n == "cycle:40,41", "one-way SCC links do not merge cycles"
    # produced members remove dependency edges, even with reverse dependencies
    review = [T(10, "A", "", [], "todo", 1, {11}),
              T(11, "B", "", [], "review", 1, {10})]
    t, n = orch._pick_next(review)
    assert t.number == 10 and n == "", "review breaks a dependency cycle"
    t, n = orch._pick_next(cycle + [T(99, "Ready", "", [], "todo", 9, set())])
    assert t.number == 99 and n == "", "ready work always beats cycle fallback"
    # blocked members still participate in SCCs and their diagnostics
    mixed = [T(10, "A", "", [], "todo", 1, {11}),
             T(11, "B", "", [], "blocked", 1, {10})]
    t, n = orch._pick_next(mixed)
    assert t.number == 10 and n == "cycle:10,11", "include non-actionable SCC members"
    # cycles without actionable members do not displace blocked actionable work
    non_actionable = blocked + [T(20, "A", "", [], "blocked", 1, {21}),
                               T(21, "B", "", [], "blocked", 1, {20})]
    t, n = orch._pick_next(non_actionable)
    assert t.number == 7 and n == "blocked:3", "cycle fallback needs actionable members"
    # diagnostic dependencies exclude produced and closed issues and are sorted
    unmet = [T(7, "Dependent", "", [], "todo", 1, {9, 3, 4, 999}),
             T(9, "Blocked", "", [], "blocked", 1, set()),
             T(3, "Blocked", "", [], "blocked", 1, set()),
             T(4, "Produced", "", [], "done", 1, {7})]
    t, n = orch._pick_next(unmet)
    assert t.number == 7 and n == "blocked:3,9", "report only unmet open dependencies"
    # phase precedes issue number in both fallback paths
    t, n = orch._pick_next([T(4, "Later", "", [], "todo", 2, {4}),
                            T(40, "Earlier", "", [], "todo", 1, {40})])
    assert t.number == 40 and n == "cycle:40", "cycle selection uses phase first"
    t, n = orch._pick_next(blocked + [T(70, "Earlier", "", [], "todo", 0, {3})])
    assert t.number == 70 and n == "blocked:3", "blocked selection uses phase first"
    t, n = orch._pick_next(blocked + [T(8, "Doing", "", [], "in-progress", 1, {3})])
    assert t.number == 7 and n == "blocked:3", "blocked selection uses number, not status"
    # ready scheduling still ranks status, phase, then issue number
    ready = [T(1, "Fresh", "", [], "todo", 0, set()),
             T(2, "Later", "", [], "in-progress", 2, set()),
             T(8, "Higher", "", [], "in-progress", 1, set()),
             T(7, "Lower", "", [], "in-progress", 1, set())]
    t, n = orch._pick_next(ready)
    assert t.number == 7 and n == "", "preserve ready task ordering"
    assert orch._pick_next([]) == (None, ""), "empty input has no task"
    assert orch._pick_next([T(1, "Done", "", [], "done")]) == (None, ""), "no actionable task"
    # a normal chain chooses its ready tail
    chain = [T(i, "Chain", "", [], "todo", 1, {i + 1}) for i in range(1, 5000)]
    chain.append(T(5000, "Tail", "", [], "todo", 1, set()))
    t, n = orch._pick_next(chain)
    assert t.number == 5000 and n == "", "long chain tail is ready"
    # a blocked tail forces SCC traversal of the entire 5,000-node chain
    stalled_chain = deepcopy(chain)
    stalled_chain[-1].status = "blocked"
    t, n = orch._pick_next(stalled_chain)
    assert t.number == 1 and n == "blocked:2", "long stalled chain must not recurse"
    long_cycle = deepcopy(chain)
    long_cycle[-1].depends_on = {1}
    t, n = orch._pick_next(long_cycle)
    assert t.number == 1 and n == "cycle:" + ",".join(map(str, range(1, 5001))), "long cycle must not recurse"
    # selection is order-independent and never mutates list order or task fields
    rng = Random(0)
    for tasks, expected in [(cycle, (10, "cycle:10,11")),
                            (blocked, (7, "blocked:3")),
                            (separate, (20, "cycle:20,21,22")),
                            (branching, (40, "cycle:40,41")),
                            (mixed, (10, "cycle:10,11")),
                            (unmet, (7, "blocked:3,9")),
                            (ready, (7, "")),
                            (stalled_chain, (1, "blocked:2")),
                            (long_cycle, (1, "cycle:" + ",".join(map(str, range(1, 5001)))))]:
        orders = [list(tasks), list(reversed(tasks))]
        shuffled = list(tasks)
        rng.shuffle(shuffled)
        orders.append(shuffled)
        if len(tasks) <= 3:
            orders.extend(list(order) for order in permutations(tasks))
        for order in orders:
            snapshot, originals = deepcopy(order), list(order)
            t, n = orch._pick_next(order)
            assert (t.number, n) == expected, "input order must not affect selection"
            assert order == snapshot, "scheduler must not mutate input task fields"
            assert all(a is b for a, b in zip(order, originals)), "scheduler must preserve input order and objects"
            assert any(t is original for original in originals), "return the original TaskView"
    # closed dep counts as produced
    t, n = orch._pick_next([T(100, "UI", "", [], "todo", 2, {999})])
    assert t.number == 100 and n == "", "closed dep is satisfied"
    _warning_test(cfg)
    print("scheduler: all assertions passed ✅")

    # engine routing: architect→codex, cto→claude_code, engineers→codex
    assert cfg.engine_for("architect")[0] == "codex"
    assert cfg.engine_for("cto")[0] == "claude_code"
    assert cfg.engine_for("backend-engineer")[0] == "codex"
    assert cfg.agent_for_tags(["review", "qa"]) == "cto"
    print("engines:   architect=codex, cto=claude_code, engineers=codex ✅")

    _memory_test()


def _warning_test(cfg):
    T = TaskView
    cycle = [T(10, "A", "", [], "todo", 1, {11}),
             T(11, "B", "", [], "todo", 1, {10})]
    blocked = [T(7, "Dependent", "", [], "todo", 1, {3}),
               T(3, "Blocked", "", [], "blocked", 1, set())]
    other_cycle = [T(20, "A", "", [], "todo", 1, {21}),
                   T(21, "B", "", [], "todo", 1, {20})]
    snapshots = [cycle, cycle, blocked, blocked, other_cycle, other_cycle,
                 [T(99, "Ready", "", [], "todo", 1, set())]]
    run_cfg = deepcopy(cfg)
    run_cfg.raw["loop"].update(max_iterations=len(snapshots), max_consecutive_blocks=20)
    run_cfg.raw["memory"] = {"enabled": False}
    runner = Orchestrator(run_cfg)
    messages = []
    runner.log = messages.append
    # Replace external boundaries; exercise the real picker and run loop.
    with patch.object(runner, "gh") as gh, \
            patch.object(runner, "_engine_for") as engine_for, \
            patch.object(runner, "_load_graph", return_value={}), \
            patch.object(runner, "_load_tasks") as load_tasks, \
            patch.object(runner, "_setup_branch", return_value=""), \
            patch.object(runner, "_record_run"), \
            patch("loopengine.orchestrator.gitutil.current_branch", return_value="main"), \
            patch("loopengine.orchestrator.time.sleep"):
        gh.recent_comments.return_value = []
        engine_for.return_value.name = "test"
        engine_for.return_value.run.return_value = TurnResult(False, "STATUS: blocked", "test")
        for _ in range(2):
            load_tasks.side_effect = snapshots
            messages.clear()
            summary = runner.run("test spec", "test plan")
            warnings = [m for m in messages if "⚠️" in m]
            assert summary["turns"] == len(snapshots), "exercise every diagnostic and repetition"
            assert len(warnings) == 3, "warn once per distinct note in each run"
            for warning, note in zip(warnings, ["cycle:10,11", "blocked:3", "cycle:20,21"]):
                assert note in warning, "warning must include its precise note"


def _memory_test():
    import tempfile, shutil
    from pathlib import Path
    from loopengine.memory import MemoryStore, Note
    vault = Path(tempfile.mkdtemp()) / "vault"
    try:
        m = MemoryStore({"enabled": True, "vault": str(vault), "namespace": "demo"}, Path("."))
        m.write(Note(title="RLS pattern", body="row level security on org_id",
                     type="pattern", namespace="demo", tags=["supabase", "rls"], task="#1"))
        m.write(Note(title="Bad: unbounded fanout", body="burned credits",
                     type="episode", namespace="demo", tags=["infra"], task="#2"))
        assert m.retrieve("supabase rls", namespace="demo"), "retrieval must find tagged note"
        # synonym recall: query with different words than the note
        m.write(Note(title="JWT token rotation", body="rotating refresh token, store hash",
                     type="pattern", namespace="demo", tags=["auth"], task="#3"))
        login = m.retrieve("handle login sessions", namespace="demo")
        assert login and login[0].title == "JWT token rotation", "synonym recall failed"
        m.reinforce("#1", "passed")          # proven → weight up
        m.reinforce("#2", "failed")          # anti-pattern → weight to floor
        assert m.prune() == 1, "failed note must prune"
        assert m.retrieve("supabase rls", namespace="demo")[0].weight >= 2
        print("memory:    retrieve/reinforce/prune loop ✅")
    finally:
        shutil.rmtree(vault.parent, ignore_errors=True)

if __name__ == "__main__":
    main()
