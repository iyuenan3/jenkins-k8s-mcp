from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import ToolError


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class OperationStore:
    """Durable metadata only. Request payloads, command output, and diffs never enter SQLite."""

    def __init__(self, state_dir: str):
        self.directory = Path(state_dir).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "operations.sqlite3"
        self._owner_file = None
        key_path = self.directory / "fingerprint.key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(os.urandom(32))
        self._key = key_path.read_bytes()
        if len(self._key) != 32:
            raise ToolError("STATE_INVALID", "Invalid operation fingerprint key; preserve and repair state.")
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, connection_id TEXT NOT NULL,
                action TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, expires REAL NOT NULL, metadata TEXT NOT NULL,
                native TEXT NOT NULL DEFAULT '{}', verification TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '{}', request_key TEXT UNIQUE,
                fingerprint TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS operations_principal ON operations(principal,created,id)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            with db:
                yield db
        finally:
            db.close()

    def acquire_owner(self) -> None:
        file = (self.directory / "server.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                file.seek(0)
                file.write(b"0")
                file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise ToolError(
                "STATE_IN_USE", "One server process may own a state directory; use separate directories."
            ) from exc
        self._owner_file = file
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET status='expired',updated=? WHERE status='planned'", (time.time(),)
            )
            db.execute(
                "UPDATE operations SET status='unknown',updated=?,error=? WHERE status='submitting'",
                (
                    time.time(),
                    json.dumps({"code": "RESULT_UNKNOWN", "message": "Server restarted during submission."}),
                ),
            )

    def close(self) -> None:
        if self._owner_file:
            self._owner_file.close()
            self._owner_file = None

    def digest(self, value: Any) -> str:
        return hmac.new(self._key, canonical(value), hashlib.sha256).hexdigest()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        value = dict(row)
        for field in ("metadata", "native", "verification", "error"):
            value[field] = json.loads(value[field])
        value.pop("request_key", None)
        value.pop("fingerprint", None)
        return value

    def create(
        self,
        *,
        principal: str,
        connection_id: str,
        action: str,
        target: dict,
        request: Any,
        request_id: str | None,
        ttl: int,
        preconditions: dict | None = None,
    ) -> tuple[dict, bool]:
        now = time.time()
        operation_id = str(uuid.uuid4())
        fingerprint = self.digest(request)
        request_key = self.digest([principal, request_id]) if request_id else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                prior = db.execute("SELECT * FROM operations WHERE request_key=?", (request_key,)).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise ToolError(
                            "REQUEST_CONFLICT", "request_id was already used with different arguments."
                        )
                    return self._decode(prior), False
            metadata = {"target": target, "plan_hash": fingerprint, "preconditions": preconditions or {}}
            db.execute(
                "INSERT INTO operations (id,principal,connection_id,action,status,created,updated,expires,"
                "metadata,request_key,fingerprint) VALUES (?,?,?,?,'planned',?,?,?,?,?,?)",
                (
                    operation_id,
                    principal,
                    connection_id,
                    action,
                    now,
                    now,
                    now + ttl,
                    canonical(metadata).decode(),
                    request_key,
                    fingerprint,
                ),
            )
        return self.get(operation_id, principal), True

    def find_request(self, principal: str, request_id: str, request: Any) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE request_key=?", (self.digest([principal, request_id]),)
            ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != self.digest(request):
            raise ToolError("REQUEST_CONFLICT", "request_id was already used with different arguments.")
        return self._decode(row)

    def get(self, operation_id: str, principal: str | None = None) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None or (principal is not None and row["principal"] != principal):
            raise ToolError("OPERATION_NOT_FOUND", "Operation is unavailable to this identity.")
        return self._decode(row)

    def approve(self, operation_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise ToolError("OPERATION_NOT_FOUND", "No such operation.")
            if row["status"] != "planned" or row["expires"] <= time.time():
                raise ToolError("PLAN_EXPIRED", "Plan is expired or already consumed.")
            db.execute("UPDATE operations SET approved=1,updated=? WHERE id=?", (time.time(), operation_id))
        return self.get(operation_id)

    def claim(self, operation_id: str, principal: str, *, require_approval: bool) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row["principal"] != principal:
                raise ToolError("OPERATION_NOT_FOUND", "Operation is unavailable to this identity.")
            if row["status"] != "planned":
                raise ToolError("PLAN_CONSUMED", "Plan was already consumed; inspect the existing operation.")
            if row["expires"] <= time.time():
                raise ToolError("PLAN_EXPIRED", "Plan expired; create a new preview.")
            if require_approval and not row["approved"]:
                raise ToolError(
                    "CONFIRMATION_REQUIRED", "Approve this plan using the trusted local admin CLI."
                )
            db.execute(
                "UPDATE operations SET status='submitting',approved=0,updated=? WHERE id=?",
                (time.time(), operation_id),
            )
        return self.get(operation_id, principal)

    def update(
        self,
        operation_id: str,
        *,
        status: str | None = None,
        native: dict | None = None,
        verification: dict | None = None,
        error: dict | None = None,
    ) -> dict:
        values: dict[str, Any] = {"updated": time.time()}
        if status is not None:
            values["status"] = status
        for name, value in (("native", native), ("verification", verification), ("error", error)):
            if value is not None:
                values[name] = canonical(value).decode()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if prior is None:
                raise ToolError("OPERATION_NOT_FOUND", "No such operation.")
            if (
                status is not None
                and prior["status"] in {"succeeded", "failed", "partial", "cancelled", "expired"}
                and status != prior["status"]
            ):
                return self._decode(prior)
            db.execute(
                "UPDATE operations SET " + ",".join(f"{name}=?" for name in values) + " WHERE id=?",
                (*values.values(), operation_id),
            )
        return self.get(operation_id)

    def list(self, principal: str, *, limit: int = 50, cursor: str | None = None) -> dict:
        limit = max(1, min(limit, 200))
        where = "principal=?"
        params: list = [principal]
        if cursor:
            marker = self.get(cursor, principal)
            where += " AND (created<? OR (created=? AND id<?))"
            params.extend([marker["created"], marker["created"], marker["id"]])
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM operations WHERE {where} ORDER BY created DESC,id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        return {
            "items": [self._decode(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }
