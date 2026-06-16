"""GitHub coordination layer — the agents' "email inboxes".

Tasks are issues. Progress is issue comments. The design debate is a Discussion
(GraphQL) with a pinned-issue fallback. Inter-agent messages are comments with a
`@agent:<key>` mention convention the orchestrator parses.

Everything goes through the `gh` CLI so it inherits your existing auth.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Issue:
    number: int
    title: str
    labels: list[str] = field(default_factory=list)
    body: str = ""


MENTION_RE = re.compile(r"@agent:([a-z0-9\-]+)", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*STATUS:\s*(todo|in-progress|review|blocked|done)\s*$",
                       re.IGNORECASE | re.MULTILINE)


class GitHub:
    def __init__(self, gh_cfg: dict[str, Any], repo_slug: str, dry_run: bool = False):
        self.cfg = gh_cfg
        self.repo = repo_slug
        self.dry_run = dry_run
        self.cli = gh_cfg.get("cli", "gh")

    # -- low level ----------------------------------------------------------
    def available(self) -> bool:
        return shutil.which(self.cli) is not None

    def authed(self) -> bool:
        if not self.available():
            return False
        r = subprocess.run([self.cli, "auth", "status"],
                           capture_output=True, text=True)
        return r.returncode == 0

    def _run(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        cmd = [self.cli] + args
        if self.dry_run:
            print(f"[DRY-RUN gh] {' '.join(cmd)}")
            return 0, "", ""
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def _repo_args(self) -> list[str]:
        return ["--repo", self.repo] if self.repo else []

    # -- labels -------------------------------------------------------------
    def ensure_labels(self) -> None:
        palette = {
            "loop:task": "1d76db", "loop:discussion": "5319e7",
            "status:todo": "ededed", "status:in-progress": "fbca04",
            "status:review": "0e8a16", "status:blocked": "d93f0b",
            "status:done": "0052cc",
        }
        for name, color in palette.items():
            self._run(["label", "create", name, "--color", color,
                       "--force", *self._repo_args()])

    # -- tasks (issues) -----------------------------------------------------
    def create_task(self, title: str, body: str, labels: list[str]) -> int:
        for l in labels:
            self._run(["label", "create", l, "--color", "ededed",
                       "--force", *self._repo_args()])
        args = ["issue", "create", "--title", title, "--body", body, *self._repo_args()]
        for l in labels:
            args += ["--label", l]
        code, out, err = self._run(args)
        if self.dry_run:
            return 0
        m = re.search(r"/issues/(\d+)", out.strip())
        if not m:
            raise RuntimeError(f"could not parse issue number from: {out}\n{err}")
        return int(m.group(1))

    def list_tasks(self, label: str) -> list[Issue]:
        if self.dry_run:
            return []
        args = ["issue", "list", "--label", label, "--state", "open",
                "--json", "number,title,labels,body", "--limit", "200", *self._repo_args()]
        code, out, _ = self._run(args)
        if code != 0 or not out.strip():
            return []
        data = json.loads(out)
        return [
            Issue(number=i["number"], title=i["title"],
                  labels=[l["name"] for l in i.get("labels", [])],
                  body=i.get("body", ""))
            for i in data
        ]

    def comment(self, number: int, body: str) -> None:
        self._run(["issue", "comment", str(number), "--body", body, *self._repo_args()])

    def recent_comments(self, number: int, limit: int = 8) -> list[dict[str, str]]:
        """Read the latest comments on a task = an agent 'checking its inbox'."""
        if self.dry_run:
            return []
        args = ["issue", "view", str(number), "--json", "comments", *self._repo_args()]
        code, out, _ = self._run(args)
        if code != 0 or not out.strip():
            return []
        comments = json.loads(out).get("comments", [])
        return [{"author": (c.get("author") or {}).get("login", "?"),
                 "body": c.get("body", "")} for c in comments[-limit:]]

    def set_status(self, number: int, labels_cfg: dict[str, str], status_key: str) -> None:
        status_labels = {v for k, v in labels_cfg.items() if k.startswith("status_")}
        new = labels_cfg.get(status_key)
        args = ["issue", "edit", str(number), *self._repo_args()]
        for l in status_labels:
            if l != new:
                args += ["--remove-label", l]
        if new:
            args += ["--add-label", new]
        self._run(args)

    def close_task(self, number: int) -> None:
        self._run(["issue", "close", str(number), *self._repo_args()])

    # -- design debate (discussion) ----------------------------------------
    def create_discussion(self, title: str, body: str, category: str) -> str:
        """Open a real GitHub Discussion via GraphQL. Returns a URL or marker."""
        if not self.repo or "/" not in self.repo:
            return self._discussion_fallback(title, body)
        owner, name = self.repo.split("/", 1)
        # resolve repo id + category id
        q = (
            'query($o:String!,$n:String!){repository(owner:$o,name:$n){id '
            'discussionCategories(first:25){nodes{id name}}}}'
        )
        code, out, err = self._run(
            ["api", "graphql", "-f", f"query={q}", "-F", f"o={owner}", "-F", f"n={name}"]
        )
        if self.dry_run:
            print(f"[DRY-RUN gh] would open discussion '{title}' in #{category}")
            return "[dry-run-discussion]"
        if code != 0:
            return self._discussion_fallback(title, body)
        try:
            data = json.loads(out)["data"]["repository"]
            repo_id = data["id"]
            cats = {c["name"]: c["id"] for c in data["discussionCategories"]["nodes"]}
            cat_id = cats.get(category) or next(iter(cats.values()), None)
            if not cat_id:
                return self._discussion_fallback(title, body)
        except Exception:
            return self._discussion_fallback(title, body)

        m = (
            'mutation($r:ID!,$c:ID!,$t:String!,$b:String!){'
            'createDiscussion(input:{repositoryId:$r,categoryId:$c,title:$t,body:$b})'
            '{discussion{url}}}'
        )
        code, out, err = self._run(
            ["api", "graphql", "-f", f"query={m}", "-F", f"r={repo_id}",
             "-F", f"c={cat_id}", "-F", f"t={title}", "-F", f"b={body}"]
        )
        if code != 0:
            return self._discussion_fallback(title, body)
        try:
            return json.loads(out)["data"]["createDiscussion"]["discussion"]["url"]
        except Exception:
            return self._discussion_fallback(title, body)

    def _discussion_fallback(self, title: str, body: str) -> str:
        num = self.create_task(
            f"[DESIGN] {title}", body,
            labels=["loop:discussion"],
        )
        return f"issue#{num}"


# -- helpers used by the orchestrator --------------------------------------
def parse_mentions(text: str) -> list[str]:
    return [m.lower() for m in MENTION_RE.findall(text or "")]


def parse_status(text: str) -> str | None:
    m = STATUS_RE.search(text or "")
    return m.group(1).lower() if m else None
