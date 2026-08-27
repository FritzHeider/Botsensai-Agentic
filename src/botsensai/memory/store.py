"""Agentic memory: the notes Botsensai writes to itself and reads back.

Three properties make this useful rather than decorative:

1. **Append-only with explicit supersession.** Nothing is ever mutated in place.
   A revised belief is a new entry that points at the one it replaces. This is
   what lets a backtest ask "what did the bot believe at 03:14 last Tuesday"
   and get an honest answer instead of today's hindsight.

2. **Time-bounded retrieval.** `recall()` takes an `as_of` and will not return
   an entry created after it. Memory is the single easiest place to leak
   look-ahead bias into a backtest, so the guard lives in the store rather than
   trusting every caller to remember.

3. **Confidence that decays and updates on evidence.** A heuristic that keeps
   being right gets reinforced; one that stops being right decays out of the
   retrieval window instead of being enforced forever.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from botsensai.models import MemoryEntry, MemoryKind, utcnow
from botsensai.util.logging import get_logger
from botsensai.util.text import tokens

log = get_logger(__name__)

MEMORY_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memories (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    created_at     REAL NOT NULL,
    valid_from     REAL NOT NULL,
    valid_until    REAL,
    subject        TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    tags           TEXT,
    confidence     REAL NOT NULL,
    evidence       TEXT,
    supersedes     TEXT,
    hit_count      INTEGER DEFAULT 0,
    last_used_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_mem_subject ON memories(subject, created_at);
CREATE INDEX IF NOT EXISTS ix_mem_kind ON memories(kind, created_at);
CREATE INDEX IF NOT EXISTS ix_mem_created ON memories(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title, body, tags, content='memories', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS memory_feedback (
    id            TEXT NOT NULL,
    recorded_at   REAL NOT NULL,
    was_correct   INTEGER NOT NULL,
    note          TEXT,
    PRIMARY KEY (id, recorded_at)
);
"""


def _ts(value: datetime | None) -> float | None:
    return None if value is None else value.timestamp()


def _dt(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(float(value), tz=UTC)


class MemoryStore:
    """Durable, time-aware note store for the agent."""

    def __init__(
        self,
        path: str | Path = "data/memory.db",
        decay_half_life_hours: float = 72.0,
        min_confidence: float = 0.35,
    ) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.decay_half_life_hours = decay_half_life_hours
        self.min_confidence = min_confidence
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(MEMORY_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- write -------------------------------------------------------------- #

    def remember(
        self,
        kind: MemoryKind | str,
        subject: str,
        title: str,
        body: str,
        *,
        tags: Sequence[str] = (),
        confidence: float = 0.5,
        evidence: Sequence[str] = (),
        supersedes: str | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        created_at: datetime | None = None,
    ) -> MemoryEntry:
        """Write one note. Returns the stored entry.

        `created_at` is injectable so the backtester can write memories with the
        simulated clock rather than wall-clock time.
        """
        now = created_at or utcnow()
        entry = MemoryEntry(
            id=uuid.uuid4().hex[:16],
            kind=MemoryKind(kind) if isinstance(kind, str) else kind,
            created_at=now,
            valid_from=valid_from or now,
            valid_until=valid_until,
            subject=subject,
            title=title.strip(),
            body=body.strip(),
            tags=list(tags),
            confidence=max(0.0, min(1.0, confidence)),
            evidence=list(evidence),
            supersedes=supersedes,
        )
        with self.conn:
            self.conn.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.kind.value,
                    _ts(entry.created_at),
                    _ts(entry.valid_from),
                    _ts(entry.valid_until),
                    entry.subject,
                    entry.title,
                    entry.body,
                    orjson.dumps(entry.tags).decode(),
                    entry.confidence,
                    orjson.dumps(entry.evidence).decode(),
                    entry.supersedes,
                    0,
                    None,
                ),
            )
            rowid = self.conn.execute(
                "SELECT rowid FROM memories WHERE id = ?", (entry.id,)
            ).fetchone()["rowid"]
            self.conn.execute(
                "INSERT INTO memories_fts(rowid, title, body, tags) VALUES (?,?,?,?)",
                (rowid, entry.title, entry.body, " ".join(entry.tags)),
            )
            if supersedes:
                # Close out the superseded belief at the moment the new one starts,
                # rather than deleting it, so history stays reconstructible.
                self.conn.execute(
                    "UPDATE memories SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
                    (_ts(entry.valid_from), supersedes),
                )
        log.debug("memory.write", kind=entry.kind.value, subject=subject, title=entry.title)
        return entry

    def revise(self, old_id: str, body: str, confidence: float, **kwargs: Any) -> MemoryEntry | None:
        """Supersede an existing entry with an updated version of itself."""
        old = self.get(old_id)
        if old is None:
            return None
        return self.remember(
            kind=old.kind,
            subject=old.subject,
            title=kwargs.pop("title", old.title),
            body=body,
            tags=kwargs.pop("tags", old.tags),
            confidence=confidence,
            evidence=kwargs.pop("evidence", old.evidence),
            supersedes=old.id,
            **kwargs,
        )

    def record_feedback(self, memory_id: str, was_correct: bool, note: str = "") -> float:
        """Reinforce or weaken a belief based on what actually happened.

        Uses a bounded additive update rather than a Bayesian one on purpose:
        the evidence stream here is not independent (the same meta produces many
        correlated confirmations), so a proper Bayesian update would race to
        certainty on what is really one observation repeated.
        """
        entry = self.get(memory_id)
        if entry is None:
            return 0.0
        delta = 0.08 if was_correct else -0.12
        new_conf = max(0.0, min(1.0, entry.confidence + delta))
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_feedback VALUES (?,?,?,?)",
                (memory_id, utcnow().timestamp(), int(was_correct), note),
            )
            self.conn.execute(
                "UPDATE memories SET confidence = ? WHERE id = ?", (new_conf, memory_id)
            )
        return new_conf

    # -- read --------------------------------------------------------------- #

    def get(self, memory_id: str) -> MemoryEntry | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def recall(
        self,
        *,
        as_of: datetime | None = None,
        subject: str | None = None,
        kinds: Iterable[MemoryKind | str] | None = None,
        tags: Iterable[str] | None = None,
        query: str | None = None,
        limit: int = 24,
        min_confidence: float | None = None,
        apply_decay: bool = True,
    ) -> list[MemoryEntry]:
        """Retrieve relevant memories that existed at `as_of`.

        Ranking blends recency decay, stored confidence, and lexical match
        against `query`. The `created_at <= as_of` filter is non-negotiable and
        is what keeps the backtester honest.
        """
        t = as_of or utcnow()
        t_ts = t.timestamp()
        floor = self.min_confidence if min_confidence is None else min_confidence

        clauses = ["created_at <= ?", "valid_from <= ?", "(valid_until IS NULL OR valid_until > ?)"]
        params: list[Any] = [t_ts, t_ts, t_ts]

        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        kind_list = [k.value if isinstance(k, MemoryKind) else str(k) for k in (kinds or [])]
        if kind_list:
            clauses.append(f"kind IN ({','.join('?' * len(kind_list))})")
            params.extend(kind_list)

        sql = f"SELECT * FROM memories WHERE {' AND '.join(clauses)}"
        rows = self.conn.execute(sql, params).fetchall()
        entries = [_row_to_entry(r) for r in rows]

        tag_set = {s.lower() for s in (tags or [])}
        if tag_set:
            entries = [e for e in entries if tag_set & {x.lower() for x in e.tags}]

        query_tokens = set(tokens(query)) if query else set()

        scored: list[tuple[float, MemoryEntry]] = []
        for e in entries:
            conf = e.confidence
            if apply_decay:
                age_hours = max(0.0, (t - e.created_at).total_seconds() / 3600.0)
                conf *= 0.5 ** (age_hours / max(1e-6, self.decay_half_life_hours))
            if conf < floor:
                continue
            relevance = 1.0
            if query_tokens:
                body_tokens = set(tokens(f"{e.title} {e.body} {' '.join(e.tags)}"))
                overlap = len(query_tokens & body_tokens)
                relevance = 1.0 + overlap / max(1, len(query_tokens))
            # Heuristics that have proven useful get a mild boost.
            usage = 1.0 + math.log1p(e.hit_count) * 0.1
            scored.append((conf * relevance * usage, e))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = [e for _, e in scored[:limit]]
        self._mark_used([e.id for e in top], t)
        return top

    def _mark_used(self, ids: Sequence[str], at: datetime) -> None:
        if not ids:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE memories SET hit_count = hit_count + 1, last_used_at = ? WHERE id = ?",
                [(at.timestamp(), i) for i in ids],
            )

    def search(self, text: str, limit: int = 20) -> list[MemoryEntry]:
        """Full-text search across all memories, ignoring time bounds."""
        safe = text.replace('"', " ").strip()
        if not safe:
            return []
        rows = self.conn.execute(
            """SELECT m.* FROM memories_fts f
               JOIN memories m ON m.rowid = f.rowid
               WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?""",
            (safe, limit),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def briefing(self, as_of: datetime | None = None, limit: int = 12) -> str:
        """Render active global heuristics and regime notes as prompt-ready text.

        This is what gets injected into an LLM call when the bot is asked to
        reason about a new token, and into the content generator so blog posts
        reflect what the system has actually learned rather than generic filler.
        """
        entries = self.recall(
            as_of=as_of,
            kinds=[MemoryKind.HEURISTIC, MemoryKind.REGIME, MemoryKind.HYPOTHESIS],
            limit=limit,
        )
        if not entries:
            return "No prior learnings recorded yet."
        lines = []
        for e in entries:
            conf = f"{e.confidence:.0%}"
            lines.append(f"[{e.kind.value}/{conf}] {e.title}: {e.body}")
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS c, AVG(confidence) AS avg_conf FROM memories GROUP BY kind"
        ).fetchall()
        return {
            r["kind"]: {"count": int(r["c"]), "avg_confidence": round(float(r["avg_conf"]), 3)}
            for r in rows
        }


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        id=row["id"],
        kind=MemoryKind(row["kind"]),
        created_at=_dt(row["created_at"]) or utcnow(),
        valid_from=_dt(row["valid_from"]) or utcnow(),
        valid_until=_dt(row["valid_until"]),
        subject=row["subject"],
        title=row["title"],
        body=row["body"],
        tags=orjson.loads(row["tags"]) if row["tags"] else [],
        confidence=float(row["confidence"]),
        evidence=orjson.loads(row["evidence"]) if row["evidence"] else [],
        supersedes=row["supersedes"],
        hit_count=int(row["hit_count"] or 0),
        last_used_at=_dt(row["last_used_at"]),
    )


__all__ = ["MEMORY_SCHEMA", "MemoryStore"]
