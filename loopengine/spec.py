"""Phase 1 — SPEC. You're in the loop here.

Turns a one-line intent into a structured spec (Spec-Kit-flavoured) that becomes
the immovable source of truth for the whole run. For big epics only — small
tasks don't need this (it burns far more tokens, exactly as the video warns).
"""
from __future__ import annotations

from pathlib import Path

from .agents import build_turn_prompt
from .codex import Engine
from .config import Config

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
LOOP_DIR_NAME = ".loop"


def loop_dir(cfg: Config) -> Path:
    d = cfg.workdir / LOOP_DIR_NAME
    d.mkdir(exist_ok=True)
    return d


def spec_path(cfg: Config) -> Path:
    return loop_dir(cfg) / "spec.md"


def build_spec(cfg: Config, intent: str, dry_run: bool = False) -> Path:
    template = (TEMPLATES / "spec.md").read_text()
    persona_dir = ROOT / "prompts"
    architect = (persona_dir / "architect.md").read_text()

    prompt = f"""{architect}

You are the Architect. Convert the intent below into a complete specification by
filling out EVERY section of this template. Be concrete and testable. Do not
write code. Output ONLY the filled markdown.

# INTENT
{intent}

# TEMPLATE TO FILL
{template}
"""
    ename, ecfg = cfg.engine_for("architect")
    engine = Engine(ecfg, cfg.workdir, dry_run=dry_run, name=ename)
    out = spec_path(cfg)
    if dry_run:
        filled = template.replace("<one-line intent>", intent)
        out.write_text(f"<!-- DRY-RUN spec from intent: {intent} -->\n\n{filled}")
        return out
    result = engine.run(prompt)
    body = result.output.strip() if result.ok else template
    out.write_text(body)
    return out
