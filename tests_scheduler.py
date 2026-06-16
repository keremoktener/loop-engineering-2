"""Standalone proof for the dependency scheduler. Run: python3 tests_scheduler.py"""
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
    # cycle falls back, never deadlocks
    t, n = orch._pick_next([
        T(10, "A", "", [], "todo", 1, {11}),
        T(11, "B", "", [], "todo", 1, {10})])
    assert t is not None and n == "cycle-or-block", "cycle must fall back"
    # closed dep counts as produced
    t, n = orch._pick_next([T(100, "UI", "", [], "todo", 2, {999})])
    assert t.number == 100 and n == "", "closed dep is satisfied"
    print("scheduler: all assertions passed ✅")

    # engine routing: architect→codex, cto→claude_code, engineers→codex
    assert cfg.engine_for("architect")[0] == "codex"
    assert cfg.engine_for("cto")[0] == "claude_code"
    assert cfg.engine_for("backend-engineer")[0] == "codex"
    assert cfg.agent_for_tags(["review", "qa"]) == "cto"
    print("engines:   architect=codex, cto=claude_code, engineers=codex ✅")

    _memory_test()


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
