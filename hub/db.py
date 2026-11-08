"""SQLite 存储：设备、配对码、审计事件。

规则（CLOUD_AGENT §5.4）：

* WAL + `busy_timeout=5000` + 每线程独立连接；
* token / 连接码**只存 sha256**，不存明文；
* 配对码消费在 `BEGIN IMMEDIATE` 事务里完成，并发下只能成功一次；
* 所有时间用 Unix 秒。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 2

_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
      device_id     TEXT PRIMARY KEY,
      name          TEXT NOT NULL,
      token_hash    TEXT NOT NULL UNIQUE,
      token_prefix  TEXT NOT NULL,
      scopes        TEXT NOT NULL,
      created_at    INTEGER NOT NULL,
      last_seen_at  INTEGER,
      revoked_at    INTEGER,
      rotated_at    INTEGER
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS pairing_codes (
      code_hash       TEXT PRIMARY KEY,
      created_at      INTEGER NOT NULL,
      expires_at      INTEGER NOT NULL,
      used_at         INTEGER,
      used_by_device  TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      at        INTEGER NOT NULL,
      event     TEXT NOT NULL,
      device_id TEXT,
      ip        TEXT,
      path      TEXT,
      status    INTEGER,
      detail    TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_events (at);",
    # -- schema 2：工作区派活（H3）----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS device_workspaces (
      device_id    TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      label        TEXT NOT NULL,
      description  TEXT NOT NULL DEFAULT '',
      dispatchable INTEGER NOT NULL DEFAULT 0,
      updated_at   INTEGER NOT NULL,
      PRIMARY KEY (device_id, workspace_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dispatch_tasks (
      task_id          TEXT PRIMARY KEY,
      client_token     TEXT NOT NULL,
      context_id       TEXT NOT NULL DEFAULT '',
      device_id        TEXT NOT NULL,
      workspace_id     TEXT NOT NULL,
      prompt           TEXT NOT NULL,
      state            TEXT NOT NULL,
      question         TEXT,
      answer           TEXT,
      summary          TEXT,
      files_changed    TEXT,
      usage            TEXT,
      error            TEXT,
      local_session_id TEXT,
      attempt          INTEGER NOT NULL DEFAULT 0,
      created_at       INTEGER NOT NULL,
      updated_at       INTEGER NOT NULL,
      deadline_at      INTEGER NOT NULL,
      claimed_at       INTEGER,
      started_at       INTEGER,
      finished_at      INTEGER
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_dispatch_state ON dispatch_tasks (state, device_id);",
    "CREATE INDEX IF NOT EXISTS idx_dispatch_device ON dispatch_tasks (device_id, created_at);",
    # 幂等：同一个 client_token 只能对应一个任务
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_token ON dispatch_tasks (client_token);",
    """
    CREATE TABLE IF NOT EXISTS dispatch_events (
      id      INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id TEXT NOT NULL,
      at      INTEGER NOT NULL,
      kind    TEXT NOT NULL,
      payload TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_dispatch_events ON dispatch_events (task_id, id);",
)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.initialize()

    # -- 连接 ---------------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    def initialize(self) -> None:
        with self._lock:
            conn = self.connection()
            for statement in _MIGRATIONS:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) != SCHEMA_VERSION:
                conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- 设备 ---------------------------------------------------------------

    def upsert_device(
        self,
        *,
        device_id: str,
        name: str,
        token_hash: str,
        token_prefix: str,
        scopes: str,
        now: int | None = None,
    ) -> None:
        """同一 `device_id` 再配对 = 轮换 token，旧 token 立即失效。"""
        at = int(now if now is not None else time.time())
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT created_at FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO devices "
                    "(device_id, name, token_hash, token_prefix, scopes, created_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (device_id, name, token_hash, token_prefix, scopes, at, at),
                )
            else:
                conn.execute(
                    "UPDATE devices SET name = ?, token_hash = ?, token_prefix = ?, scopes = ?, "
                    "revoked_at = NULL, rotated_at = ?, last_seen_at = ? WHERE device_id = ?",
                    (name, token_hash, token_prefix, scopes, at, at, device_id),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def device_by_token_hash(self, token_hash: str) -> sqlite3.Row | None:
        return (
            self.connection()
            .execute("SELECT * FROM devices WHERE token_hash = ?", (token_hash,))
            .fetchone()
        )

    def device(self, device_id: str) -> sqlite3.Row | None:
        return (
            self.connection()
            .execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
            .fetchone()
        )

    def touch_device(self, device_id: str, now: int | None = None) -> None:
        at = int(now if now is not None else time.time())
        self.connection().execute(
            "UPDATE devices SET last_seen_at = ? WHERE device_id = ?", (at, device_id)
        )

    def revoke_device(self, device_id: str, now: int | None = None) -> bool:
        at = int(now if now is not None else time.time())
        cursor = self.connection().execute(
            "UPDATE devices SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
            (at, device_id),
        )
        return cursor.rowcount > 0

    def device_counts(self) -> dict[str, int]:
        row = self.connection().execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN revoked_at IS NULL THEN 0 ELSE 1 END) AS revoked "
            "FROM devices"
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "revoked": int(row["revoked"] or 0),
        }

    # -- 配对码 -------------------------------------------------------------

    def insert_pairing_code(self, *, code_hash: str, created_at: int, expires_at: int) -> None:
        self.connection().execute(
            "INSERT INTO pairing_codes (code_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (code_hash, created_at, expires_at),
        )

    def pairing_row(self, code_hash: str) -> sqlite3.Row | None:
        return (
            self.connection()
            .execute("SELECT * FROM pairing_codes WHERE code_hash = ?", (code_hash,))
            .fetchone()
        )

    def consume_pairing_code(self, code_hash: str, *, now: int, used_by_device: str) -> str:
        """在 `BEGIN IMMEDIATE` 里消费。

        返回 `ok` / `pairing_not_found` / `pairing_expired` / `pairing_used`。
        """
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM pairing_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return "pairing_not_found"
            if row["used_at"] is not None:
                conn.execute("ROLLBACK")
                return "pairing_used"
            if int(row["expires_at"]) <= now:
                conn.execute("ROLLBACK")
                return "pairing_expired"
            conn.execute(
                "UPDATE pairing_codes SET used_at = ?, used_by_device = ? WHERE code_hash = ?",
                (now, used_by_device, code_hash),
            )
            conn.execute("COMMIT")
            return "ok"
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def purge_pairing_codes(self, now: int | None = None) -> int:
        at = int(now if now is not None else time.time())
        cursor = self.connection().execute(
            "DELETE FROM pairing_codes WHERE expires_at <= ? AND used_at IS NULL", (at,)
        )
        return cursor.rowcount

    # -- 审计 ---------------------------------------------------------------

    def insert_audit(
        self,
        *,
        at: int,
        event: str,
        device_id: str | None,
        ip: str | None,
        path: str | None,
        status: int | None,
        detail: str | None,
    ) -> None:
        self.connection().execute(
            "INSERT INTO audit_events (at, event, device_id, ip, path, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, event, device_id, ip, path, status, detail),
        )

    def audit_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.connection()
            .execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (int(limit),))
            .fetchall()
        )
