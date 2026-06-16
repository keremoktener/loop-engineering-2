"""Agent registry: loads persona prompts and assembles the per-turn prompt."""
from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"


def load_persona(key: str) -> str:
    f = PROMPTS / f"{key}.md"
    return f.read_text() if f.exists() else f"You are the {key} agent."


def load_system_loop() -> str:
    f = PROMPTS / "system_loop.md"
    return f.read_text() if f.exists() else ""


def build_turn_prompt(
    *,
    agent_key: str,
    agent_cfg: dict[str, Any],
    spec: str,
    plan: str,
    task_title: str,
    task_body: str,
    task_number: int,
    inbox: list[dict[str, str]],
    repo_slug: str,
    memory_block: str = "",
) -> str:
    """Compose the full prompt handed to the engine for one agent turn."""
    persona = load_persona(agent_cfg.get("prompt", agent_key))
    system = load_system_loop()

    inbox_txt = "\n".join(
        f"- @{c['author']}: {c['body'].strip()[:600]}" for c in inbox
    ) or "(inbox empty)"

    mem = f"\n{memory_block}\n" if memory_block else ""

    return f"""{system}

# YOUR ROLE
{persona}

You are acting as **{agent_cfg.get('name', agent_key)}** (`{agent_key}`).
{mem}
# PROJECT SPEC (source of truth — do not contradict)
{spec.strip() or '(no spec provided)'}

# APPROVED PLAN
{plan.strip() or '(no plan provided)'}

# YOUR CURRENT TASK  (GitHub issue #{task_number} in {repo_slug or 'this repo'})
## {task_title}
{task_body.strip()}

# INBOX — recent messages from teammates on this task
{inbox_txt}

# WHAT TO DO NOW
1. Do ONLY this task. Make the smallest correct change set that satisfies it.
2. Edit files directly in the working tree.
3. If you are blocked or need another specialist, leave a message addressed to
   them using `@agent:<key>` and end your turn.
4. End your reply with a STATUS line and a short report block:

REPORT:
<2-6 lines: what you changed, files touched, how to verify>

@agent:reviewer please verify   # (optional — mention any teammate you need)

STATUS: <todo|in-progress|review|blocked|done>
"""
