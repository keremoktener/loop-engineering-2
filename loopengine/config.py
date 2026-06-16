"""Configuration loading + light validation for the loop engineering kit."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install it with:  pip install pyyaml"
    ) from e

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    # convenience accessors -------------------------------------------------
    @property
    def repo_slug(self) -> str:
        slug = (self.raw.get("repo", {}) or {}).get("slug", "") or ""
        if slug:
            return slug
        return _detect_repo_slug(self.workdir)

    @property
    def workdir(self) -> Path:
        wd = (self.raw.get("repo", {}) or {}).get("workdir", ".") or "."
        return Path(wd).expanduser().resolve()

    @property
    def engine(self) -> dict[str, Any]:
        """Back-compat: the default engine's config dict."""
        return self.engine_for(None)[1]

    @property
    def engines(self) -> dict[str, Any]:
        """All named engines. Falls back to wrapping a legacy single `engine:`
        block as {'codex': ...} so old configs keep working."""
        eng = self.raw.get("engines")
        if eng:
            return {k: v for k, v in eng.items() if k != "default"}
        return {"codex": self.raw.get("engine", {}) or {}}

    @property
    def default_engine_name(self) -> str:
        eng = self.raw.get("engines") or {}
        name = eng.get("default")
        if name and name in self.engines:
            return name
        return next(iter(self.engines), "codex")

    def engine_for(self, agent_key: str | None) -> tuple[str, dict[str, Any]]:
        """Resolve (engine_name, engine_cfg) for an agent. None → default engine."""
        name = self.default_engine_name
        if agent_key:
            name = (self.agents.get(agent_key, {}) or {}).get("engine", name)
        engines = self.engines
        if name not in engines:
            name = next(iter(engines), "codex")
        return name, engines.get(name, {})

    @property
    def plan_cfg(self) -> dict[str, Any]:
        return self.raw.get("plan", {}) or {}

    @property
    def memory_cfg(self) -> dict[str, Any]:
        m = dict(self.raw.get("memory", {}) or {})
        if not m.get("namespace"):
            slug = self.repo_slug.split("/")[-1] if self.repo_slug else ""
            m["namespace"] = slug or "global"
        return m

    @property
    def github(self) -> dict[str, Any]:
        return self.raw.get("github", {}) or {}

    @property
    def agents(self) -> dict[str, Any]:
        return self.raw.get("agents", {}) or {}

    @property
    def loop(self) -> dict[str, Any]:
        return self.raw.get("loop", {}) or {}

    @property
    def verify(self) -> dict[str, Any]:
        return self.raw.get("verify", {}) or {}

    def labels(self) -> dict[str, str]:
        return (self.github.get("labels", {}) or {})

    def agent_for_tags(self, tags: list[str]) -> str:
        """Route a task to the best-matching agent by its `owns` domains."""
        tags = [t.lower() for t in tags]
        best, best_score = None, 0
        for key, spec in self.agents.items():
            owns = [o.lower() for o in (spec.get("owns") or [])]
            score = len(set(tags) & set(owns))
            if score > best_score:
                best, best_score = key, score
        if best:
            return best
        # default to the lead agent, else first defined
        for key, spec in self.agents.items():
            if spec.get("is_lead"):
                return key
        return next(iter(self.agents), "architect")


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG
    if not p.exists():
        raise SystemExit(f"Config not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    return Config(raw=raw, path=p)


def _detect_repo_slug(workdir: Path) -> str:
    try:
        url = subprocess.check_output(
            ["git", "-C", str(workdir), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""
    # git@github.com:owner/name.git  |  https://github.com/owner/name.git
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        return url.split(":", 1)[-1]
    if "github.com/" in url:
        return url.split("github.com/", 1)[-1]
    return ""
