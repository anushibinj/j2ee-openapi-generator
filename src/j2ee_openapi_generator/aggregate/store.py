"""SQLite store for the aggregate stage. Single writer, deterministic row order."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from j2ee_openapi_generator.aggregate.dedupe import normalize_path
from j2ee_openapi_generator.aggregate.models import (
    ConflictRow,
    CoverageRow,
    Decision,
    OperationRow,
    StoreSnapshot,
)
from j2ee_openapi_generator.index.models import EntityRecord

METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    method TEXT NOT NULL,
    norm_path TEXT NOT NULL,
    path TEXT NOT NULL,
    tag TEXT NOT NULL,
    operation_id TEXT,
    description TEXT,
    packet_ids TEXT NOT NULL,
    operation TEXT NOT NULL,
    PRIMARY KEY (method, norm_path)
);
CREATE TABLE IF NOT EXISTS evidence (
    method TEXT NOT NULL,
    norm_path TEXT NOT NULL,
    seq INTEGER NOT NULL,
    claim TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    snippet TEXT NOT NULL,
    PRIMARY KEY (method, norm_path, seq)
);
CREATE TABLE IF NOT EXISTS schemas (
    name TEXT NOT NULL,
    method TEXT NOT NULL,
    norm_path TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (name, method, norm_path, role)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    packet_ids TEXT NOT NULL,
    proposals TEXT NOT NULL,
    resolved INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage (
    entry_id TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    tag TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lookups (
    type TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS questions (
    tag TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (tag, text)
);
"""

_TABLES = (
    "operations",
    "evidence",
    "schemas",
    "conflicts",
    "decisions",
    "coverage",
    "meta",
    "tags",
    "lookups",
    "questions",
)

_METHOD_CASE = (
    "CASE method "
    + " ".join(f"WHEN '{m}' THEN {i}" for i, m in enumerate(METHOD_ORDER))
    + f" ELSE {len(METHOD_ORDER)} END"
)


class Store:
    """Aggregate store. ``path`` may be ``:memory:``."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def reset(self) -> None:
        """Drop every row; the store is empty and ready for an idempotent rebuild."""
        for t in _TABLES:
            self._db.execute(f"DROP TABLE IF EXISTS {t}")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ------------------------------------------------------------------ writes
    def put_operations(self, rows: list[OperationRow]) -> None:
        ordered = sorted(
            rows,
            key=lambda r: (r.path, _method_rank(r.method), normalize_path(r.path)),
        )
        for r in ordered:
            norm = normalize_path(r.path)
            self._db.execute(
                "INSERT OR REPLACE INTO operations VALUES (?,?,?,?,?,?,?,?)",
                (
                    r.method,
                    norm,
                    r.path,
                    r.operation.tag,
                    r.operation_id,
                    r.description,
                    json.dumps(r.packet_ids),
                    r.operation.model_dump_json(),
                ),
            )
            for i, e in enumerate(r.operation.evidence):
                self._db.execute(
                    "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?)",
                    (r.method, norm, i, e.claim, e.file, e.line, e.snippet),
                )
            for name, role in _schema_refs(r):
                self._db.execute(
                    "INSERT OR IGNORE INTO schemas VALUES (?,?,?,?)", (name, r.method, norm, role)
                )
        self._db.commit()

    def put_conflicts(self, conflicts: list[ConflictRow]) -> None:
        for c in sorted(conflicts, key=lambda c: c.id):
            self._db.execute(
                "INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,?,?)",
                (
                    c.id,
                    c.kind,
                    c.summary,
                    json.dumps(c.packet_ids),
                    json.dumps(c.proposals, sort_keys=True),
                    int(c.resolved),
                ),
            )
        self._db.commit()

    def put_decisions(self, decisions: list[Decision]) -> None:
        for d in sorted(decisions, key=lambda d: d.key):
            self._db.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?)",
                (d.key, d.action, d.model_dump_json()),
            )
        self._db.commit()

    def put_coverage(self, rows: list[CoverageRow]) -> None:
        for c in sorted(rows, key=lambda c: c.entry_id):
            self._db.execute(
                "INSERT OR REPLACE INTO coverage VALUES (?,?)", (c.entry_id, c.model_dump_json())
            )
        self._db.commit()

    def put_questions(self, by_tag: dict[str, list[str]]) -> None:
        for tag in sorted(by_tag):
            for q in by_tag[tag]:
                self._db.execute("INSERT OR IGNORE INTO questions VALUES (?,?)", (tag, q))
        self._db.commit()

    def put_lookups(self, types: list[str]) -> None:
        for t in sorted(set(types)):
            self._db.execute("INSERT OR IGNORE INTO lookups VALUES (?)", (t,))
        self._db.commit()

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        self._db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_operation_texts(
        self,
        method: str,
        path: str,
        operation_id: str | None,
        summary: str | None,
        description: str | None,
    ) -> None:
        """Update texts of the operation ``METHOD path``. ``None`` leaves a field unchanged.

        ``path`` may use any placeholder names; an unknown operation is ignored.
        """
        m = method.upper()
        norm = normalize_path(path)
        row = self._db.execute(
            "SELECT operation FROM operations WHERE method=? AND norm_path=?", (m, norm)
        ).fetchone()
        if row is None:
            return
        if summary is not None:
            data = json.loads(row[0])
            data["summary"] = summary
            self._db.execute(
                "UPDATE operations SET operation=? WHERE method=? AND norm_path=?",
                (json.dumps(data), m, norm),
            )
        if operation_id is not None:
            self._db.execute(
                "UPDATE operations SET operation_id=? WHERE method=? AND norm_path=?",
                (operation_id, m, norm),
            )
        if description is not None:
            self._db.execute(
                "UPDATE operations SET description=? WHERE method=? AND norm_path=?",
                (description, m, norm),
            )
        self._db.commit()

    def set_tag_description(self, tag: str, description: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO tags VALUES (?,?)", (tag, description))
        self._db.commit()

    # ------------------------------------------------------------------ reads
    def operations(self) -> list[OperationRow]:
        cur = self._db.execute(
            "SELECT method, path, operation_id, description, packet_ids, operation "
            f"FROM operations ORDER BY path, {_METHOD_CASE}, norm_path"
        )
        return [
            OperationRow(
                method=m,
                path=p,
                operation_id=oid,
                description=desc,
                packet_ids=json.loads(pids),
                operation=json.loads(op),
            )
            for m, p, oid, desc, pids, op in cur.fetchall()
        ]

    def tag_descriptions(self) -> dict[str, str]:
        cur = self._db.execute("SELECT tag, description FROM tags ORDER BY tag")
        return {t: d for t, d in cur.fetchall()}

    def tags(self) -> list[str]:
        cur = self._db.execute("SELECT DISTINCT tag FROM operations ORDER BY tag")
        return [r[0] for r in cur.fetchall()]

    def conflicts(self) -> list[ConflictRow]:
        cur = self._db.execute(
            "SELECT id, kind, summary, packet_ids, proposals, resolved FROM conflicts "
            "ORDER BY kind, id"
        )
        return [
            ConflictRow(
                id=i,
                kind=k,
                summary=s,
                packet_ids=json.loads(p),
                proposals=json.loads(pr),
                resolved=bool(r),
            )
            for i, k, s, p, pr, r in cur.fetchall()
        ]

    def decisions(self) -> list[Decision]:
        cur = self._db.execute("SELECT body FROM decisions ORDER BY key")
        return [Decision.model_validate_json(r[0]) for r in cur.fetchall()]

    def coverage(self) -> list[CoverageRow]:
        cur = self._db.execute("SELECT body FROM coverage ORDER BY entry_id")
        return [CoverageRow.model_validate_json(r[0]) for r in cur.fetchall()]

    def open_questions(self) -> dict[str, list[str]]:
        cur = self._db.execute("SELECT tag, text FROM questions ORDER BY tag, text")
        out: dict[str, list[str]] = {}
        for tag, text in cur.fetchall():
            out.setdefault(tag, []).append(text)
        return out

    def lookup_types(self) -> list[str]:
        cur = self._db.execute("SELECT type FROM lookups ORDER BY type")
        return [r[0] for r in cur.fetchall()]

    def schema_names(self) -> list[str]:
        cur = self._db.execute("SELECT DISTINCT name FROM schemas ORDER BY name")
        return [r[0] for r in cur.fetchall()]

    def snapshot(self, entities: list[EntityRecord], error_codes: dict[str, str]) -> StoreSnapshot:
        return StoreSnapshot(
            operations=self.operations(),
            entities=sorted(entities, key=lambda e: e.fqcn),
            tag_descriptions=self.tag_descriptions(),
            error_codes=dict(sorted(error_codes.items())),
            conflicts=self.conflicts(),
            coverage=self.coverage(),
            open_questions=self.open_questions(),
            lookup_types=self.lookup_types(),
        )


def _method_rank(method: str) -> int:
    return METHOD_ORDER.index(method) if method in METHOD_ORDER else len(METHOD_ORDER)


def _schema_refs(r: OperationRow) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    body = r.operation.request_body
    if body is not None and body.schema_ref:
        refs.append((body.schema_ref, "request"))
    for resp in r.operation.responses:
        if resp.schema_ref:
            refs.append((resp.schema_ref, f"response:{resp.status}"))
    return refs
