"""Evolving memory for the loop engine — an Obsidian-friendly vault of notes.

Memory is just a folder of markdown notes. Each note is one lesson (a pattern, a
decision, an episode, or a convention) with a little frontmatter. The loop reads
the most relevant notes before a turn, writes new ones after, reinforces the ones
that pass verification, and flags/prunes the ones that don't — so it gets better
over time without ever depending on the Obsidian app being open.

Open the same folder as an Obsidian vault any time to browse the graph (via
[[wikilinks]]), curate, or delete a bad note. The app is for you; the files are
for both of you.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit("PyYAML required: pip install pyyaml") from e

VALID_TYPES = {"pattern", "decision", "episode", "convention"}
_WORD = re.compile(r"[a-z0-9]+")

# Light suffix stemmer (longest suffix first) — collapses session/sessions,
# renew/renewal, stream/streaming, migrate/migration, etc.
_STEM_SUFFIXES = ("izations", "ization", "ations", "ation", "ings", "ing",
                  "ers", "er", "ed", "es", "als", "al", "s")

# Curated dev-domain clusters: any token in a cluster pulls in the whole cluster
# at query time, so "login session" reaches a note about "token refresh". This is
# the bounded vocabulary of code work — more precise here than generic embeddings.
DEV_SYNONYMS: dict[str, set[str]] = {
    "auth": {"auth", "authentication", "authorize", "authorization", "login",
             "logon", "signin", "session", "token", "jwt", "refresh", "renew",
             "renewal", "rotate", "rotation", "credential", "oauth", "sso",
             "password", "passwordless"},
    "db": {"db", "database", "postgres", "postgresql", "sql", "supabase",
           "prisma", "migration", "schema", "table", "query", "rls", "index"},
    "frontend": {"frontend", "ui", "interface", "component", "react", "nextjs",
                 "next", "page", "view", "form", "client", "css", "tailwind"},
    "api": {"api", "endpoint", "route", "handler", "rest", "graphql", "trpc",
            "request", "response", "controller", "webhook"},
    "infra": {"infra", "infrastructure", "deploy", "deployment", "ci", "cd",
              "pipeline", "docker", "queue", "worker", "cron", "trigger", "job",
              "fanout", "parallel", "concurrency", "ratelimit", "retry"},
    "mcp": {"mcp", "tool", "connector", "integration", "plugin", "server"},
    "payment": {"payment", "pay", "billing", "stripe", "iyzico", "checkout",
                "subscription", "invoice"},
    "test": {"test", "testing", "qa", "verify", "verification", "assertion",
             "coverage", "lint", "typecheck"},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:n].strip("-") or "note")


def _stem(w: str) -> str:
    for suf in _STEM_SUFFIXES:
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _build_clusters(extra: dict | None) -> dict[str, set[str]]:
    clusters = {k: set(v) for k, v in DEV_SYNONYMS.items()}
    for k, v in (extra or {}).items():
        clusters.setdefault(k, set()).update(t.lower() for t in v)
    return clusters


def _expand(tokens: set[str], clusters: dict[str, set[str]]) -> set[str]:
    """Add stems and synonym-cluster members so related vocab cross-matches."""
    out: set[str] = set()
    for t in tokens:
        s = _stem(t)
        out.add(t)
        out.add(s)
        for members in clusters.values():
            if t in members or s in members:
                out |= members
    return out


@dataclass
class Note:
    title: str
    body: str
    type: str = "episode"
    namespace: str = "global"
    tags: list[str] = field(default_factory=list)
    outcome: str = "unknown"          # unknown | passed | failed
    weight: int = 1
    task: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    path: Path | None = None

    def frontmatter(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "namespace": self.namespace,
                "outcome": self.outcome, "weight": self.weight, "tags": self.tags,
                "task": self.task, "created": self.created, "updated": self.updated}

    def render(self) -> str:
        fm = yaml.safe_dump(self.frontmatter(), sort_keys=False).strip()
        return f"---\n{fm}\n---\n# {self.title}\n\n{self.body.strip()}\n"


def _parse(path: Path) -> Note | None:
    try:
        raw = path.read_text()
    except Exception:
        return None
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.DOTALL)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    rest = m.group(2).strip()
    title = ""
    tm = re.match(r"^#\s+(.*)", rest)
    if tm:
        title = tm.group(1).strip()
        rest = rest[tm.end():].strip()
    w = fm.get("weight", 1)
    w = 1 if w is None else int(w)
    return Note(
        title=title or path.stem, body=rest,
        type=fm.get("type", "episode"), namespace=fm.get("namespace", "global"),
        tags=list(fm.get("tags", []) or []), outcome=fm.get("outcome", "unknown"),
        weight=w, task=fm.get("task", "") or "",
        id=fm.get("id", uuid.uuid4().hex[:8]),
        created=fm.get("created", _now()), updated=fm.get("updated", _now()),
        path=path,
    )


class MemoryStore:
    """File-based store over a vault folder. Optional embeddings can be layered
    later behind the same interface; nothing here needs them."""

    def __init__(self, cfg: dict[str, Any], workdir: Path):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.workdir = workdir
        self.top_k = int(self.cfg.get("top_k", 5))
        self.max_chars = int(self.cfg.get("max_note_chars", 280))
        self.default_ns = self.cfg.get("namespace", "global") or "global"
        self.vault = self._resolve_vault()
        self.clusters = _build_clusters(self.cfg.get("extra_synonyms"))
        # Optional semantic layer — OFF by default, costs nothing until enabled.
        self.embed = EmbeddingIndex(self.cfg.get("embeddings", {}) or {}, self.vault)

    def _resolve_vault(self) -> Path:
        explicit = self.cfg.get("vault")
        if explicit:
            return Path(explicit).expanduser().resolve()
        if self.cfg.get("scope", "shared") == "per_repo":
            return (self.workdir / ".loop" / "vault").resolve()
        return (Path.home() / ".loop" / "vault").resolve()

    def _ns_dirs(self, namespace: str | None) -> list[Path]:
        ns = namespace or self.default_ns
        out = []
        for d in {ns, "global"}:
            p = self.vault / d
            if p.exists():
                out.append(p)
        return out

    def _all_notes(self, namespace: str | None) -> list[Note]:
        notes = []
        for d in self._ns_dirs(namespace):
            for f in d.glob("*.md"):
                n = _parse(f)
                if n:
                    notes.append(n)
        return notes

    # -- write -------------------------------------------------------------
    def write(self, note: Note) -> Path | None:
        if not self.enabled:
            return None
        note.namespace = note.namespace or self.default_ns
        if note.type not in VALID_TYPES:
            note.type = "episode"
        d = self.vault / note.namespace
        d.mkdir(parents=True, exist_ok=True)
        fname = f"{note.type}-{_slug(note.title)}-{note.id}.md"
        path = d / fname
        note.updated = _now()
        path.write_text(note.render())
        return path

    # -- retrieve ----------------------------------------------------------
    def retrieve(self, query: str, namespace: str | None = None,
                 top_k: int | None = None, types: set[str] | None = None) -> list[Note]:
        if not self.enabled:
            return []
        k = top_k or self.top_k
        q_exp = _expand(_tokens(query), self.clusters)
        q_raw = _tokens(query)
        notes = self._all_notes(namespace)
        if types:
            notes = [n for n in notes if n.type in types]
        # optional semantic scores (empty dict if embeddings disabled/unavailable)
        sem = self.embed.similarities(query, notes) if self.embed.enabled else {}
        scored = []
        for n in notes:
            score = self._score(n, q_raw, q_exp) + sem.get(n.id, 0.0) * 4.0
            if score > 0:
                scored.append((score, n))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored[:k]]

    def _score(self, n: Note, q_raw: set[str], q_exp: set[str]) -> float:
        # exact tag hits are the strongest signal; expanded (synonym/stem) hits
        # catch related vocabulary; raw text hits are the weakest.
        tags = {t.lower() for t in n.tags}
        note_exp = _expand(_tokens(n.title + " " + n.body) | tags, self.clusters)
        tag_hits = len(q_raw & tags)
        exp_hits = len(q_exp & note_exp)
        raw_hits = len(q_raw & _tokens(n.title + " " + n.body))
        base = tag_hits * 3.0 + exp_hits * 0.6 + raw_hits * 0.8
        base += min(n.weight, 5) * 0.5
        if n.outcome == "passed":
            base += 1.0
        if n.outcome == "failed":
            base -= 2.0
        return base

    def render_block(self, notes: list[Note]) -> str:
        if not notes:
            return ""
        lines = ["# RELEVANT MEMORY (lessons from past runs — apply or avoid)"]
        for n in notes:
            gist = " ".join(n.body.split())[: self.max_chars]
            flag = {"passed": "✓ proven", "failed": "✗ AVOID", "unknown": "·"}.get(n.outcome, "·")
            lines.append(f"- [{n.type}/{flag} w{n.weight}] **{n.title}** — {gist}")
        return "\n".join(lines)

    # -- reinforce (the part that makes it 'evolve') -----------------------
    def reinforce(self, task_ref: str, outcome: str) -> int:
        """At the verify gate: boost notes tied to a task that passed, flag those
        that failed. Returns how many notes were updated."""
        if not self.enabled or not task_ref:
            return 0
        touched = 0
        for n in self._all_notes(None):
            if n.task and task_ref in n.task:
                n.outcome = outcome
                if outcome == "passed":
                    n.weight = min(n.weight + 1, 9)
                elif outcome == "failed":
                    n.weight = max(n.weight - 1, 0)
                if n.path:
                    n.updated = _now()
                    n.path.write_text(n.render())
                    touched += 1
        return touched

    def prune(self, weight_floor: int = 0) -> int:
        """Delete failed notes that never recovered — cheapest defense against a
        poisoned memory rotting future runs."""
        if not self.enabled:
            return 0
        removed = 0
        for n in self._all_notes(None):
            if n.outcome == "failed" and n.weight <= weight_floor and n.path:
                try:
                    n.path.unlink()
                    removed += 1
                except Exception:
                    pass
        return removed

    # -- distill -----------------------------------------------------------
    def distill(self, namespace: str | None = None, min_notes: int = 12) -> Path | None:
        """Heuristic consolidation: when a namespace accrues many notes, write a
        ranked `lessons.md` digest of the strongest proven ones so the signal
        doesn't drown in raw episodes. (An engine-backed summary can replace this
        later behind the same method.)"""
        if not self.enabled:
            return None
        ns = namespace or self.default_ns
        notes = [n for n in self._all_notes(ns) if n.path and n.path.parent.name == ns]
        if len(notes) < min_notes:
            return None
        proven = sorted([n for n in notes if n.outcome != "failed"],
                        key=lambda n: n.weight, reverse=True)[:20]
        lines = [f"# Lessons — {ns}", f"_distilled {_now()} from {len(notes)} notes_\n"]
        for n in proven:
            lines.append(f"## {n.title}  (w{n.weight}, {n.type})")
            lines.append(" ".join(n.body.split())[:400] + "\n")
        out = self.vault / ns / "lessons.md"
        out.write_text("\n".join(lines))
        return out

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        notes = self._all_notes(None) if self.vault.exists() else []
        by_type: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        for n in notes:
            by_type[n.type] = by_type.get(n.type, 0) + 1
            by_outcome[n.outcome] = by_outcome.get(n.outcome, 0) + 1
        return {"vault": str(self.vault), "total": len(notes),
                "by_type": by_type, "by_outcome": by_outcome}


class EmbeddingIndex:
    """Optional semantic layer — OFF unless `memory.embeddings.enabled: true`.

    When on, it embeds each note once (cached in `<vault>/.embed-cache.json` keyed
    by note-id + content hash, so only changed notes re-embed) and ranks by cosine
    similarity to the query. Purely additive: if disabled, the provider package is
    missing, or a call fails, retrieval silently falls back to the lexical scorer.

    Default provider `openai` uses OPENAI_API_KEY via the openai package; set
    `provider: none` to keep it inert. Turn this on only once the vault is large
    enough that synonym/keyword recall starts missing things.
    """

    def __init__(self, cfg: dict, vault: Path):
        self.cfg = cfg or {}
        self.vault = vault
        self.enabled = bool(self.cfg.get("enabled", False)) \
            and self.cfg.get("provider", "openai") != "none"
        self.model = self.cfg.get("model", "text-embedding-3-small")
        self.cache_path = vault / ".embed-cache.json"
        self._cache: dict = {}
        if self.enabled:
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except Exception:
                self._cache = {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(self._cache))
        except Exception:
            pass

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    def _embed(self, texts: list) -> list | None:
        try:
            from openai import OpenAI            # optional dep, only if enabled
        except Exception:
            return None
        try:
            resp = OpenAI().embeddings.create(model=self.model, input=texts)
            return [d.embedding for d in resp.data]
        except Exception:
            return None

    def similarities(self, query: str, notes: list) -> dict:
        if not self.enabled or not notes:
            return {}
        to_embed, idx = [], []
        for i, n in enumerate(notes):
            h = self._hash(n.title + n.body)
            c = self._cache.get(n.id)
            if not c or c.get("h") != h:
                to_embed.append(n.title + "\n" + n.body)
                idx.append((i, h))
        if to_embed:
            vecs = self._embed(to_embed)
            if vecs is None:
                return {}
            for (i, h), v in zip(idx, vecs):
                self._cache[notes[i].id] = {"h": h, "v": v}
            self._save_cache()
        qv = self._embed([query])
        if not qv:
            return {}
        out = {}
        for n in notes:
            c = self._cache.get(n.id)
            if c and c.get("v"):
                out[n.id] = _cosine(qv[0], c["v"])
        return out


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, dot / (na * nb))
