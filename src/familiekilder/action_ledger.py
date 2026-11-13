"""Local ledger for source write actions: operations and metadata only.

Source connectors and write actions use this ledger to make sends idempotent.
A program built on the connectors may extend it with its own tables in the
same sqlite file (`Runtime.ledger_factory`).
"""

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime

from .core import FamiliekilderError, _secure_private_file
from .private_state import ensure_private_directory

# SQLite stores this statement text in sqlite_master, so it must never change;
# an extending ledger may combine it with its own tables in one script.
OPERATIONS_SCHEMA = """\
        CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY, source TEXT NOT NULL, action TEXT NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL, receipt_json TEXT,
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """


def now():
    return datetime.now(UTC).isoformat()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class ActionLedger:
    """Operations and metadata in `.private/actions.sqlite3`.

    Subclasses extend the schema through `_initialize_schema` and add checks or
    history through `_check_operation_link` and `_record_operation_change`.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __init__(self, root):
        self.root = root
        private = root / ".private"
        root.mkdir(parents=True, exist_ok=True)
        ensure_private_directory(private)
        path = private / "actions.sqlite3"
        for candidate in (
            path,
            *(path.with_name(path.name + s) for s in ("-journal", "-wal", "-shm")),
        ):
            _secure_private_file(candidate)
        self.conn = sqlite3.connect(path, timeout=10)
        path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize_schema()

    def _initialize_schema(self):
        self.conn.executescript(OPERATIONS_SCHEMA)

    def close(self):
        self.conn.close()

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value))
            )

    def _check_operation_link(self, operation_id, source):
        """Reject a new operation that conflicts with linked local state."""

    def _record_operation_change(self, operation_id, state, receipt):
        """Record a changed operation state inside the open transaction."""

    def reserve_operation(self, operation_id, source, action, payload):
        try:
            uuid.UUID(operation_id)
        except (ValueError, TypeError):
            raise FamiliekilderError("operation_id må være en UUID") from None
        digest = fingerprint(payload)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row:
                if (row["source"], row["action"], row["fingerprint"]) != (
                    source,
                    action,
                    digest,
                ):
                    raise FamiliekilderError("Operasjons-ID er allerede brukt med annet innhold")
                return dict(row)
            self._check_operation_link(operation_id, source)
            unresolved = self.conn.execute(
                "SELECT operation_id FROM operations WHERE source=? AND action=? AND fingerprint=? AND state IN ('sending','unknown')",
                (source, action, digest),
            ).fetchone()
            if unresolved:
                raise FamiliekilderError(
                    "Uavklart handling finnes; gjenbruk operation_id " + unresolved[0]
                )
            self.conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                (operation_id, source, action, digest, "prepared", None, now()),
            )
        return None

    def operation_result(self, operation_id, state, receipt=None):
        if state not in {"prepared", "sending", "unknown", "verified"}:
            raise FamiliekilderError("Ugyldig handlingsstatus")
        if receipt is None:
            row = self.conn.execute(
                "SELECT receipt_json FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            receipt = json.loads(row[0]) if row and row[0] else None
        with self.conn:
            previous = self.conn.execute(
                "SELECT state FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            self.conn.execute(
                "UPDATE operations SET state=?,receipt_json=?,updated_at=? WHERE operation_id=?",
                (state, json.dumps(receipt) if receipt else None, now(), operation_id),
            )
            if previous and previous[0] != state:
                self._record_operation_change(operation_id, state, receipt)
        return {"operation_id": operation_id, "status": state, "receipt": receipt}
