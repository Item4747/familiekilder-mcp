"""Table for resumable discovery checkpoints, owned by the discovery readers.

`chat_discovery` and `spond_discovery` keep one checkpoint per source and
stream in `change_discovery`. They create the table when it is missing, so they
work on a plain `action_ledger.ActionLedger`. A ledger that extends it may
embed this statement in its own schema; SQLite stores the text in
`sqlite_master`, so the text must not change.
"""

SCHEMA = """CREATE TABLE IF NOT EXISTS change_discovery (
 source TEXT NOT NULL, stream TEXT NOT NULL, checkpoint_json TEXT NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(source,stream));
"""


def ensure_table(conn):
    """Create `change_discovery` if it does not exist yet."""
    conn.execute(SCHEMA)
