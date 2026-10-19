from __future__ import annotations

import asyncio
import copy
import hashlib
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .adapters.jenkins import JenkinsAdapter
from .adapters.kubernetes import KubernetesAdapter
from .catalog import directory
from .config import Connection, Settings
from .errors import ToolError, UncertainError
from .store import OperationStore, canonical

ACTIVE = {"accepted", "running", "submitting", "unknown"}


def display_time(value: float) -> str:
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


class Engine:
    def __init__(self, settings: Settings, *, adapters: dict | None = None):
        self.settings = settings
        self.store = OperationStore(settings.state_dir)
        self.store.acquire_owner()
        self.adapters = adapters or {}
        self.plans: dict[str, dict] = {}
        self.outputs: dict[str, tuple[str, float, bytes]] = {}
        self.registered_tools: list[str] = []
        self._adapter_lock = asyncio.Lock()
        self.background: set[asyncio.Task] = set()
        root = Path(__file__).parent
        self.content_id = hashlib.sha256(
            b"".join(
                p.relative_to(root).as_posix().encode() + p.read_bytes() for p in sorted(root.rglob("*.py"))
            )
        ).hexdigest()

    async def adapter(self, connection: Connection):
        async with self._adapter_lock:
            if connection.id not in self.adapters:
                if connection.kind == "jenkins":
                    self.adapters[connection.id] = JenkinsAdapter(connection)
                else:
                    self.adapters[connection.id] = KubernetesAdapter(
                        connection, cache_dir=str(self.store.directory / "discovery")
                    )
            return self.adapters[connection.id]

    def info(self) -> dict:
        return {
            "name": "jenkins-k8s-mcp",
            "version": __version__,
            "content_id": self.content_id,
            "transports": ["stdio", "streamable-http"],
            "tools": self.registered_tools,
            "capabilities": directory(self.registered_tools),
            "acceptance": "automated tests and real platform acceptance are separate",
            "observed_at": display_time(time.time()),
        }

    def _sweep(self) -> None:
        now = time.time()
        for operation_id, plan in list(self.plans.items()):
            if plan["expires"] <= now:
                self.plans.pop(operation_id, None)
                record = self.store.get(operation_id)
                if record["status"] == "planned":
                    self.store.update(operation_id, status="expired")
        self.outputs = {key: value for key, value in self.outputs.items() if value[1] > now}
        for adapter in self.adapters.values():
            if isinstance(adapter, KubernetesAdapter):
                for key, session in list(adapter.exec_sessions.items()):
                    if (
                        session.finished is not None
                        and time.monotonic() - session.finished > self.settings.plan_ttl_seconds
                    ):
                        adapter.exec_sessions.pop(key, None)
                        adapter.exec_handles.pop(key, None)

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "operation_id": record["id"],
            "connection_id": record["connection_id"],
            "action": record["action"],
            "target": record["metadata"]["target"],
            "preconditions": record["metadata"].get("preconditions", {}),
            "plan_hash": record["metadata"]["plan_hash"],
            "status": record["status"],
            "created_at": display_time(record["created"]),
            "updated_at": display_time(record["updated"]),
            "expires_at": display_time(record["expires"]),
            "native": record["native"],
            "verification": record["verification"],
            "error": record["error"],
        }

    def bounded(self, principal: str, result) -> dict:
        self._sweep()
        try:
            encoded = canonical(result)
        except (TypeError, ValueError) as exc:
            raise ToolError("RESULT_INVALID", "Platform result is not JSON serializable.") from exc
        if len(encoded) <= self.settings.max_output_bytes:
            return result
        if len(encoded) > 8388608:
            raise ToolError("OUTPUT_LIMIT", "Result exceeds the snapshot limit; narrow the requested scope.")
        while (
            len(self.outputs) >= 32
            or sum(len(value[2]) for value in self.outputs.values()) + len(encoded) > 16777216
        ):
            self.outputs.pop(next(iter(self.outputs)))
        output_id = str(uuid.uuid4())
        self.outputs[output_id] = (principal, time.time() + 60, encoded)
        return self.output_read(principal, output_id, 0, self.settings.max_output_bytes)

    def output_read(self, principal: str, output_id: str, cursor: int, max_bytes: int) -> dict:
        self._sweep()
        value = self.outputs.get(output_id)
        if not value or value[0] != principal:
            raise ToolError(
                "OUTPUT_EXPIRED", "Output snapshot is unavailable to this identity or has expired."
            )
        data = value[2]
        if cursor < 0 or cursor > len(data):
            raise ToolError("CURSOR_INVALID", "Cursor is outside this output snapshot.")
        chunk = data[cursor : cursor + max(4, min(max_bytes, self.settings.max_output_bytes))]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.start == 0:
                raise ToolError("CURSOR_INVALID", "Cursor must be a returned UTF-8 boundary.") from exc
            chunk = chunk[: exc.start]
            text = chunk.decode("utf-8")
        next_cursor = cursor + len(chunk)
        return {
            "output_id": output_id,
            "encoding": "json/utf-8",
            "chunk": text,
            "cursor": cursor,
            "next_cursor": next_cursor if next_cursor < len(data) else None,
            "total_bytes": len(data),
            "truncated": next_cursor < len(data),
            "expires_at": display_time(value[1]),
            "volatile": True,
        }

    async def change(
        self,
        principal: str,
        kind: str,
        connection_id: str | None,
        action: str,
        payload: dict,
        *,
        preview_only: bool = False,
        request_id: str | None = None,
    ) -> dict:
        self._sweep()
        connection = self.settings.resolve(connection_id, kind)
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ToolError("ARGUMENT_INVALID", "request_id must contain 1 to 128 characters.")
        try:
            request = {"connection": connection.fingerprint(), "action": action, "payload": payload}
            if len(canonical(request)) > 1048576:
                raise ToolError("INPUT_LIMIT", "Request exceeds the 1 MiB input limit.")
        except (TypeError, ValueError) as exc:
            raise ToolError("ARGUMENT_INVALID", "Request must contain finite JSON values.") from exc
        if request_id:
            previous = self.store.find_request(principal, request_id, request)
            if previous:
                return {**self.public(previous), "duplicate_request": True}
        if len(self.plans) >= 128:
            raise ToolError(
                "CAPACITY_EXCEEDED", "Pending plan capacity reached; execute or let existing plans expire."
            )
        adapter = await self.adapter(connection)
        plan = await adapter.prepare(action, copy.deepcopy(payload))
        record, fresh = self.store.create(
            principal=principal,
            connection_id=connection.id,
            action=action,
            target=plan["target"],
            request=request,
            request_id=request_id,
            ttl=self.settings.plan_ttl_seconds,
            preconditions=plan["preconditions"],
        )
        if not fresh:
            return {**self.public(record), "duplicate_request": True}
        self.plans[record["id"]] = {
            **copy.deepcopy(plan),
            "action": action,
            "principal": principal,
            "connection_id": connection.id,
            "connection_fingerprint": connection.fingerprint(),
            "expires": record["expires"],
        }
        if preview_only or connection.extra_confirmation:
            return {
                **self.public(record),
                "preview": plan["preview"],
                "preconditions": plan["preconditions"],
                "extra_confirmation_required": connection.extra_confirmation,
                "approval_channel": "admin CLI: jenkins-k8s-mcp --config CONFIG approve PLAN_ID --yes"
                if connection.extra_confirmation
                else None,
            }
        return await self.execute(principal, record["id"])

    async def execute(self, principal: str, operation_id: str) -> dict:
        self._sweep()
        record = self.store.get(operation_id, principal)
        plan = self.plans.get(operation_id)
        if not plan:
            raise ToolError(
                "PLAN_UNAVAILABLE",
                "Plan payload expired or was lost on restart; inspect operation state before replanning.",
            )
        connection = self.settings.resolve(record["connection_id"])
        if connection.fingerprint() != plan["connection_fingerprint"]:
            raise ToolError("TARGET_MISMATCH", "Connection changed after preview.")
        self.store.claim(operation_id, principal, require_approval=connection.extra_confirmation)
        self.plans.pop(operation_id, None)
        adapter = await self.adapter(connection)
        try:
            result = await adapter.execute(plan["action"], plan)
        except asyncio.CancelledError:
            self.store.update(
                operation_id,
                status="unknown",
                error={
                    "code": "RESULT_UNKNOWN",
                    "message": "Local wait cancelled; remote execution may continue.",
                },
            )
            raise
        except UncertainError as exc:
            record = self.store.update(operation_id, status="unknown", error=exc.as_dict())
            return self.public(record)
        except ToolError as exc:
            record = self.store.update(
                operation_id, status="failed", error={"code": exc.code, "message": exc.message}
            )
            return self.public(record)
        except Exception:
            record = self.store.update(
                operation_id,
                status="unknown",
                error={
                    "code": "INTERNAL_ERROR",
                    "message": "Execution interrupted; inspect state before any resubmission.",
                },
            )
            return self.public(record)
        record = self.store.update(
            operation_id,
            status=result["status"],
            native=result.get("native", {}),
            verification=result.get("verification", {}),
        )
        if plan["action"] == "exec" and isinstance(adapter, KubernetesAdapter):

            async def monitor():
                await adapter.wait_exec(record["native"]["session_id"])
                observed = await adapter.refresh(record["native"])
                self.store.update(
                    operation_id, status=observed["status"], verification=observed["verification"]
                )

            task = asyncio.create_task(monitor())
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        return {**self.public(record), **({"result": result["result"]} if "result" in result else {})}

    async def get(self, principal: str, operation_id: str, *, refresh: bool = True) -> dict:
        self._sweep()
        record = self.store.get(operation_id, principal)
        if refresh and record["status"] in ACTIVE and record["native"]:
            connection = self.settings.resolve(record["connection_id"])
            adapter = await self.adapter(connection)
            try:
                result = await adapter.refresh(record["native"])
            except ToolError as exc:
                if connection.kind == "jenkins" and exc.code in {"NOT_FOUND", "TARGET_MISMATCH"}:
                    record = self.store.update(
                        operation_id,
                        status="unknown",
                        error={
                            "code": exc.code,
                            "message": "Native receipt is unavailable or mismatched; never substitute latest.",
                        },
                    )
                return {**self.public(record), "observation_error": exc.as_dict()}
            record = self.store.update(
                operation_id,
                status=result["status"],
                native=result.get("native", record["native"]),
                verification=result.get("verification", {}),
            )
        result = self.public(record)
        if record["status"] == "planned" and operation_id in self.plans:
            result.update(
                {
                    "preview": self.plans[operation_id]["preview"],
                    "preconditions": self.plans[operation_id]["preconditions"],
                }
            )
        if record["status"] == "unknown" and not record["native"]:
            result["recovery"] = (
                "No attributable platform receipt; automatic resubmission is disabled. Verify the platform externally."
            )
        return result

    async def wait(self, principal: str, operation_id: str, wait_seconds: float = 10) -> dict:
        if not 0 <= wait_seconds <= 30:
            raise ToolError("ARGUMENT_INVALID", "wait_seconds must be between 0 and 30.")
        deadline = asyncio.get_running_loop().time() + wait_seconds
        if wait_seconds == 0:
            return await self.get(principal, operation_id)
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    result = await self.get(principal, operation_id)
                    if result["status"] not in {"accepted", "running", "submitting"}:
                        return {**result, "wait_timeout": False}
                    await asyncio.sleep(0.5)
        except TimeoutError:
            return {**self.public(self.store.get(operation_id, principal)), "wait_timeout": True}

    async def operation_output(
        self, principal: str, operation_id: str, *, cursor: str | None = None, max_bytes: int = 65536
    ) -> dict:
        record = self.store.get(operation_id, principal)
        connection = self.settings.resolve(record["connection_id"])
        adapter = await self.adapter(connection)
        native = record["native"]
        limit = max(8, min(max_bytes, self.settings.max_output_bytes))
        if connection.kind == "kubernetes" and native.get("action") == "exec":
            result = adapter.exec_output(native["session_id"], cursor, limit)
            self.store.update(
                operation_id,
                status=result["status"],
                verification={
                    "exit_code": result["exit_code"],
                    "remote_termination_confirmed": result["remote_termination_confirmed"],
                    "reason": result["reason"],
                },
            )
            return {"operation_id": operation_id, "target": record["metadata"]["target"], **result}
        if connection.kind == "jenkins":
            await self.get(principal, operation_id)
            native = self.store.get(operation_id, principal)["native"]
            if not native.get("build_number"):
                return {
                    "operation_id": operation_id,
                    "status": record["status"],
                    "log": "",
                    "next_cursor": cursor or "0",
                }
            try:
                position = int(cursor or "0")
            except ValueError as exc:
                raise ToolError("CURSOR_INVALID", "Jenkins log cursor is a byte offset.") from exc
            return {
                "operation_id": operation_id,
                **await adapter.logs(native["job"], native["build_number"], position, limit),
            }
        raise ToolError("UNSUPPORTED", "This operation has no captured stream.")

    async def cancel(self, principal: str, operation_id: str, *, preview_only: bool = False) -> dict:
        await self.get(principal, operation_id)
        record = self.store.get(operation_id, principal)
        connection = self.settings.resolve(record["connection_id"])
        native = record["native"]
        if connection.kind == "jenkins" and native.get("action") in {"build", "rebuild"}:
            if native.get("build_number"):
                action, payload = "stop", {"job": native["job"], "build_number": native["build_number"]}
            elif native.get("queue_id"):
                action, payload = "cancel_queue", {"queue_id": native["queue_id"]}
            else:
                raise ToolError("RESULT_UNKNOWN", "No Queue/Build receipt to cancel safely.")
        elif connection.kind == "kubernetes" and native.get("action") == "exec":
            action, payload = "exec_close", {"session_id": native["session_id"]}
        else:
            raise ToolError("UNSUPPORTED", "Cancellation cannot undo a persisted resource change.")
        return await self.change(
            principal, connection.kind, connection.id, action, payload, preview_only=preview_only
        )

    def list(self, principal: str, limit: int = 50, cursor: str | None = None) -> dict:
        page = self.store.list(principal, limit=limit, cursor=cursor)
        return {**page, "items": [self.public(row) for row in page["items"]]}

    async def close(self) -> None:
        try:
            await asyncio.gather(
                *(adapter.close() for adapter in self.adapters.values()), return_exceptions=True
            )
            if self.background:
                await asyncio.gather(*self.background, return_exceptions=True)
        finally:
            self.plans.clear()
            self.outputs.clear()
            self.store.close()
