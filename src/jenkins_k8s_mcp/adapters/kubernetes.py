from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import ResourceNotFoundError, ResourceNotUniqueError
from kubernetes.stream import stream

from ..config import Connection
from ..errors import ToolError, UncertainError
from ..security import resource_summary, sanitize


def plain(value: Any) -> Any:
    return value.to_dict() if hasattr(value, "to_dict") else value


def manifest(value: dict | str) -> dict:
    try:
        obj = yaml.safe_load(value) if isinstance(value, str) else copy.deepcopy(value)
    except yaml.YAMLError as exc:
        raise ToolError("MANIFEST_INVALID", "Supply one valid YAML or JSON object.") from exc
    if not isinstance(obj, dict):
        raise ToolError("MANIFEST_INVALID", "Supply a single resource object.")
    return obj


def object_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 1024
        or name in {".", ".."}
        or any(c in name for c in "/\\%?#")
        or any(ord(c) < 32 for c in name)
    ):
        raise ToolError("TARGET_INVALID", "Name must be one native object path segment without URL syntax.")
    return name


class TimedApiClient(client.ApiClient):
    def __init__(self, configuration, timeout):
        super().__init__(configuration=configuration)
        self.request_deadline = (min(timeout, 10), timeout)

    def call_api(self, *args, **kwargs):
        if not kwargs.get("_request_timeout"):
            kwargs["_request_timeout"] = self.request_deadline
        return super().call_api(*args, **kwargs)


class ExecSession:
    """Bounded, volatile output. Closing the channel never promises process termination."""

    def __init__(self, budget: int):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.streams = {"stdout": bytearray(), "stderr": bytearray()}
        self.offsets = {"stdout": 0, "stderr": 0}
        self.budget = budget
        self.status = "running"
        self.exit_code: int | None = None
        self.reason: str | None = None
        self.finished: float | None = None

    def append(self, channel: str, text: str) -> None:
        with self.lock:
            data = self.streams[channel]
            data.extend(text.encode("utf-8", errors="replace"))
            excess = max(0, len(data) - self.budget)
            if excess:
                del data[:excess]
                self.offsets[channel] += excess

    def finish(self, status: str, exit_code: int | None, reason: str | None = None) -> None:
        with self.lock:
            self.status, self.exit_code, self.reason, self.finished = (
                status,
                exit_code,
                reason,
                time.monotonic(),
            )

    def output(self, cursor: str | None, limit: int) -> dict:
        positions = {"stdout": 0, "stderr": 0}
        if cursor:
            try:
                parsed = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if set(parsed) != set(positions) or any(type(v) is not int or v < 0 for v in parsed.values()):
                    raise ValueError()
                positions = parsed
            except (ValueError, TypeError, KeyError) as exc:
                raise ToolError("CURSOR_INVALID", "Invalid exec output cursor.") from exc
        result = {}
        gaps, more = {}, False
        with self.lock:
            for channel in positions:
                original = positions[channel]
                start = max(original, self.offsets[channel])
                end_of_stream = self.offsets[channel] + len(self.streams[channel])
                if original > end_of_stream:
                    raise ToolError("CURSOR_INVALID", "Exec cursor is beyond the captured output.")
                gaps[channel] = max(0, start - original)
                offset = start - self.offsets[channel]
                data = bytes(self.streams[channel][offset : offset + max(4, limit // 2)])
                # Any bytes already evicted are reported explicitly; retained data remains resumable.
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    if exc.reason == "unexpected end of data" and exc.start:
                        data = data[: exc.start]
                    text = data.decode("utf-8", errors="replace")
                result[channel] = text
                positions[channel] = start + len(data)
                more |= positions[channel] < end_of_stream
            result.update(
                {
                    "status": self.status,
                    "exit_code": self.exit_code,
                    "reason": self.reason,
                    "remote_termination_confirmed": self.exit_code is not None,
                    "lost_bytes": gaps,
                    "truncated": any(gaps.values()),
                    "more_data": more,
                    "next_cursor": base64.urlsafe_b64encode(json.dumps(positions).encode()).decode(),
                    "sensitive_output": True,
                }
            )
        return result


class KubernetesAdapter:
    def __init__(self, connection: Connection, *, cache_dir: str, api_client=None, dynamic=None):
        self.connection = connection
        self.api = api_client
        self.dynamic = dynamic
        self.cache_dir = Path(cache_dir)
        self._initialization = threading.Lock()
        self._external = api_client is not None
        self.semaphore = asyncio.Semaphore(connection.max_concurrency)
        self.exec_sessions: dict[str, ExecSession] = {}
        self.exec_tasks: set[asyncio.Task] = set()
        self.exec_handles: dict[str, asyncio.Task] = {}

    def _ensure(self) -> None:
        with self._initialization:
            if self.dynamic is not None:
                return
            if self.api is None:
                cfg = client.Configuration()
                cfg.retries = 0
                cfg.connection_pool_maxsize = self.connection.max_concurrency
                try:
                    if self.connection.in_cluster:
                        config.load_incluster_config(client_configuration=cfg)
                    else:
                        config.load_kube_config(
                            config_file=str(Path(self.connection.kubeconfig or "").expanduser()),
                            context=self.connection.context,
                            client_configuration=cfg,
                            persist_config=False,
                        )
                except Exception as exc:
                    raise ToolError(
                        "CREDENTIAL_UNAVAILABLE", "Cannot load the selected Kubernetes credential/context."
                    ) from exc
                self.api = TimedApiClient(cfg, self.connection.timeout_seconds)
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.dynamic = DynamicClient(
                self.api, cache_file=str(self.cache_dir / (self.connection.id + ".json"))
            )

    async def _run(self, function, *, write: bool = False):
        async with self.semaphore:
            try:
                async with asyncio.timeout(self.connection.timeout_seconds + 1):

                    def invoke():
                        self._ensure()
                        return function()

                    return await asyncio.to_thread(invoke)
            except ToolError:
                raise
            except (ResourceNotFoundError, ResourceNotUniqueError) as exc:
                code = "RESOURCE_AMBIGUOUS" if isinstance(exc, ResourceNotUniqueError) else "UNSUPPORTED"
                raise ToolError(
                    code, "Resource API is unavailable or ambiguous; specify its exact API version and kind."
                ) from exc
            except ApiException as exc:
                status = getattr(exc, "status", 0)
                if write and (not status or status >= 500):
                    raise UncertainError() from exc
                code = {
                    401: "AUTHENTICATION_FAILED",
                    403: "FORBIDDEN",
                    404: "NOT_FOUND",
                    409: "CONFLICT",
                    410: "CURSOR_EXPIRED",
                    415: "UNSUPPORTED",
                    422: "VALIDATION_FAILED",
                }.get(status, "PLATFORM_REJECTED")
                raise ToolError(
                    code, "Kubernetes rejected the request.", details={"http_status": status}
                ) from exc
            except (TimeoutError, OSError) as exc:
                if write:
                    raise UncertainError() from exc
                raise ToolError("WAIT_TIMEOUT", "Kubernetes read did not finish.") from exc
            except Exception as exc:
                # urllib3, authentication plugins and extension transports have distinct exception types.
                if write:
                    raise UncertainError() from exc
                raise ToolError(
                    "CONNECTION_FAILED", "Kubernetes transport or credential provider failed."
                ) from exc

    def _resource(self, api_version: str, kind: str, subresource: str | None = None):
        resource = self.dynamic.resources.get(api_version=api_version, kind=kind)
        if subresource:
            child = resource.subresources.get(subresource)
            if child is None:
                raise ToolError("UNSUPPORTED", "The API does not advertise this subresource.")
            return child
        return resource

    def _namespace(self, resource, namespace: str | None) -> str | None:
        if resource.namespaced:
            namespace = namespace or self.connection.namespace
            if not namespace:
                raise ToolError(
                    "TARGET_AMBIGUOUS", "This resource requires an explicit or configured namespace."
                )
        elif namespace:
            raise ToolError("TARGET_INVALID", "Do not provide namespace for a cluster-scoped resource.")
        if namespace is not None and not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace):
            raise ToolError("TARGET_INVALID", "namespace must be a native DNS label.")
        return namespace

    def _request(
        self,
        method: str,
        resource,
        *,
        name: str | None = None,
        namespace: str | None = None,
        body=None,
        **kwargs,
    ):
        return plain(
            self.dynamic.request(
                method,
                resource.path(name=name, namespace=namespace),
                body=body,
                _request_timeout=(min(self.connection.timeout_seconds, 10), self.connection.timeout_seconds),
                **kwargs,
            )
        )

    async def discover(
        self, *, api_version: str | None = None, kind: str | None = None, limit: int = 200, offset: int = 0
    ) -> dict:
        def discover():
            found = self.dynamic.resources.search(
                **({"api_version": api_version} if api_version else {}), **({"kind": kind} if kind else {})
            )
            items = []
            for r in found:
                if getattr(r, "name", None):
                    items.append(
                        {
                            "api_version": r.group_version,
                            "kind": r.kind,
                            "resource": r.name,
                            "namespaced": r.namespaced,
                            "verbs": r.verbs,
                            "subresources": {key: value.verbs for key, value in r.subresources.items()},
                        }
                    )
            items.sort(key=lambda v: (v["api_version"], v["resource"]))
            return items

        items = await self._run(discover)
        limit = max(1, min(limit, 1000))
        if offset < 0:
            raise ToolError("ARGUMENT_INVALID", "offset must be nonnegative.")
        return {
            "items": items[offset : offset + limit],
            "next_offset": offset + limit if len(items) > offset + limit else None,
            "permissions": "discovery is not an RBAC grant; each action is authorized by the API server",
        }

    async def health(self) -> dict:
        def health():
            version = plain(
                self.dynamic.request("get", "/version", _request_timeout=self.connection.timeout_seconds)
            )
            return {
                "status": "reachable",
                "version": version,
                "context": self.connection.context,
                "server": self.api.configuration.host,
            }

        return await self._run(health)

    async def get(
        self,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        *,
        subresource: str | None = None,
    ) -> dict:
        object_name(name)

        def get():
            r = self._resource(api_version, kind, subresource)
            ns = self._namespace(r, namespace)
            return self._request("get", r, name=name, namespace=ns)

        return await self._run(get)

    async def list(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        *,
        selector: str | None = None,
        field_selector: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        all_namespaces: bool = False,
    ) -> dict:
        def read():
            r = self._resource(api_version, kind)
            ns = None if r.namespaced and all_namespaces else self._namespace(r, namespace)
            data = self._request(
                "get",
                r,
                namespace=ns,
                label_selector=selector,
                field_selector=field_selector,
                limit=max(1, min(limit, 1000)),
                _continue=cursor,
            )
            return {
                "items": data.get("items", []),
                "next_cursor": data.get("metadata", {}).get("continue") or None,
                "resource_version": data.get("metadata", {}).get("resourceVersion"),
            }

        return await self._run(read)

    async def prepare(self, action: str, payload: dict) -> dict:
        payload = copy.deepcopy(payload)
        if action in {"exec", "exec_close"}:
            return await self.prepare_exec(action, payload)
        api_version, kind = payload["api_version"], payload["kind"]
        name, namespace = payload.get("name"), payload.get("namespace")
        subresource = payload.get("subresource")
        body = manifest(payload["manifest"]) if "manifest" in payload else None
        if action in {"create", "update", "apply"} and body is None:
            raise ToolError("ARGUMENT_INVALID", "This action requires one manifest.")
        if body is not None:
            meta = body.setdefault("metadata", {})
            if name and meta.get("name") and meta["name"] != name:
                raise ToolError("TARGET_MISMATCH", "Manifest name differs from the selected target.")
            if namespace and meta.get("namespace") and meta["namespace"] != namespace:
                raise ToolError("TARGET_MISMATCH", "Manifest namespace differs from the selected target.")
            name = name or meta.get("name")
            namespace = namespace or meta.get("namespace")
            if not subresource and (body.get("apiVersion") != api_version or body.get("kind") != kind):
                raise ToolError("TARGET_MISMATCH", "Manifest kind/apiVersion differs from the selected API.")

        def discover():
            r = self._resource(api_version, kind, subresource)
            return self._namespace(r, namespace), list(r.verbs or [])

        namespace, verbs = await self._run(discover)
        required = {
            "create": "create",
            "update": "update",
            "patch": "patch",
            "apply": "patch",
            "delete": "delete",
            "restart": "patch",
            "scale": "patch",
        }.get(action)
        if required is None or required not in verbs:
            raise ToolError("UNSUPPORTED", "The discovered API does not advertise the requested verb.")
        if not name and not (action == "create" and body and body.get("metadata", {}).get("generateName")):
            raise ToolError("TARGET_INVALID", "Provide name, or generateName for create.")
        if name:
            object_name(name)
        before = None
        if name:
            try:
                before = await self.get(api_version, kind, name, namespace, subresource=subresource)
            except ToolError as exc:
                if action not in {"create", "apply"} or exc.code != "NOT_FOUND":
                    raise
        if action == "create" and before is not None:
            raise ToolError("CONFLICT", "Resource already exists.")
        pre = resource_summary(before) if before else {"absent": True}
        if before and (not pre["uid"] or not pre["resourceVersion"]):
            raise ToolError("UNSUPPORTED", "This API does not expose UID/resourceVersion preconditions.")
        if body:
            if name:
                body["metadata"]["name"] = name
            if namespace:
                body["metadata"]["namespace"] = namespace
            if before:
                body["metadata"].update({"uid": pre["uid"], "resourceVersion": pre["resourceVersion"]})
        method, content_type, request_body = "patch", "application/merge-patch+json", body
        if action == "create":
            method = "post"
            content_type = "application/json"
        elif action == "update":
            method = "put"
            content_type = "application/json"
        elif action == "delete":
            method, content_type = "delete", "application/json"
            request_body = {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": pre["uid"], "resourceVersion": pre["resourceVersion"]},
                "propagationPolicy": payload.get("propagation_policy", "Background"),
            }
            if payload.get("grace_period_seconds") is not None:
                request_body["gracePeriodSeconds"] = payload["grace_period_seconds"]
        elif action == "apply":
            if before is None:
                if "create" not in verbs:
                    raise ToolError("UNSUPPORTED", "Safe apply of an absent object requires the create verb.")
                # SSA has no atomic absence condition. POST preserves create-if-absent under races.
                method, content_type = "post", "application/json"
            else:
                content_type = "application/apply-patch+yaml"
        elif action == "patch":
            patch = payload["patch"]
            patch_type = payload.get("patch_type", "merge")
            content_type = {
                "merge": "application/merge-patch+json",
                "json": "application/json-patch+json",
                "strategic": "application/strategic-merge-patch+json",
            }.get(patch_type)
            if content_type is None:
                raise ToolError("ARGUMENT_INVALID", "Patch type must be merge, json, or strategic.")
            if patch_type == "json":
                if not isinstance(patch, list):
                    raise ToolError("ARGUMENT_INVALID", "JSON patch requires an operation array.")
                request_body = [
                    {"op": "test", "path": "/metadata/uid", "value": pre["uid"]},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": pre["resourceVersion"]},
                    *patch,
                ]
            else:
                request_body = manifest(patch)
                request_body.setdefault("metadata", {}).update(
                    {"uid": pre["uid"], "resourceVersion": pre["resourceVersion"]}
                )
        elif action == "restart":
            if kind not in {"Deployment", "StatefulSet", "DaemonSet"} or api_version != "apps/v1":
                raise ToolError(
                    "UNSUPPORTED", "Restart supports apps/v1 Deployment, StatefulSet, and DaemonSet."
                )
            marker = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="microseconds")
            payload["restart_marker"] = marker
            request_body = {
                "metadata": {"uid": pre["uid"], "resourceVersion": pre["resourceVersion"]},
                "spec": {
                    "template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": marker}}}
                },
            }
        elif action == "scale":
            replicas = payload.get("replicas")
            if (
                kind not in {"Deployment", "StatefulSet", "ReplicaSet", "ReplicationController"}
                or type(replicas) is not int
                or replicas < 0
            ):
                raise ToolError(
                    "ARGUMENT_INVALID", "Scale requires a supported replica controller and replicas >= 0."
                )
            request_body = {
                "metadata": {"uid": pre["uid"], "resourceVersion": pre["resourceVersion"]},
                "spec": {"replicas": replicas},
            }
        payload.update(
            {
                "name": name,
                "namespace": namespace,
                "body": request_body,
                "method": method,
                "content_type": content_type,
            }
        )
        options = {"field_manager": "jenkins-k8s-mcp"}
        if action == "apply" and method == "patch":
            options["force_conflicts"] = bool(payload.get("force_conflicts", False))
        payload["options"] = options

        def dry_run():
            r = self._resource(api_version, kind, subresource)
            return self._request(
                method,
                r,
                name=None if method == "post" else name,
                namespace=namespace,
                body=request_body,
                content_type=content_type,
                dry_run="All",
                **options,
            )

        after = await self._run(dry_run)
        target = {
            "connection_id": self.connection.id,
            "api_version": api_version,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "uid": pre.get("uid"),
            "subresource": subresource,
        }
        return {
            "payload": payload,
            "target": target,
            "preconditions": pre,
            "preview": {
                "action": action,
                "before": sanitize(before),
                "after_dry_run": sanitize(after),
                "dry_run": "passed",
                "generated_values_are_provisional": True,
                "write_method": method,
            },
        }

    async def execute(self, action: str, plan: dict) -> dict:
        if action in {"exec", "exec_close"}:
            return await self.execute_exec(action, plan)
        p, pre = plan["payload"], plan["preconditions"]
        if p.get("name"):
            try:
                current = await self.get(
                    p["api_version"],
                    p["kind"],
                    p["name"],
                    p.get("namespace"),
                    subresource=p.get("subresource"),
                )
            except ToolError as exc:
                if not pre.get("absent") or exc.code != "NOT_FOUND":
                    raise
            else:
                if pre.get("absent") or any(
                    current.get("metadata", {}).get(k) != pre.get(k) for k in ("uid", "resourceVersion")
                ):
                    raise ToolError("CONFLICT", "Resource changed after preview; create a new plan.")

        def write():
            r = self._resource(p["api_version"], p["kind"], p.get("subresource"))
            return self._request(
                p["method"],
                r,
                name=None if p["method"] == "post" else p.get("name"),
                namespace=p.get("namespace"),
                body=p["body"],
                content_type=p["content_type"],
                **p["options"],
            )

        result = await self._run(write, write=True)
        summary = resource_summary(result)
        native = {
            "api_version": p["api_version"],
            "kind": p["kind"],
            "name": summary.get("name") or p.get("name"),
            "namespace": p.get("namespace"),
            "uid": summary.get("uid") or pre.get("uid"),
            "resourceVersion": summary.get("resourceVersion"),
            "generation": summary.get("generation"),
            "action": action,
            "subresource": p.get("subresource"),
        }
        if action == "scale":
            native["replicas"] = p["replicas"]
        if action == "restart":
            native["restart_marker"] = p["restart_marker"]
        return {
            "status": "accepted" if action in {"restart", "scale", "delete"} else "succeeded",
            "native": native,
            "verification": {
                "platform": "accepted" if action == "delete" else "applied",
                "resource": "pending" if action in {"restart", "scale", "delete"} else "not_checked",
                "business": "not_checked",
            },
            "result": sanitize(result),
        }

    async def refresh(self, native: dict) -> dict:
        if native.get("action") == "exec":
            session = self.exec_sessions.get(native["session_id"])
            if not session:
                return {
                    "status": "unknown",
                    "native": native,
                    "verification": {
                        "remote_termination_confirmed": False,
                        "reason": "volatile_session_unavailable",
                    },
                }
            return {
                "status": session.status,
                "native": native,
                "verification": {
                    "exit_code": session.exit_code,
                    "remote_termination_confirmed": session.exit_code is not None,
                    "reason": session.reason,
                },
            }
        try:
            obj = await self.get(
                native["api_version"],
                native["kind"],
                native["name"],
                native.get("namespace"),
                subresource=native.get("subresource"),
            )
        except ToolError as exc:
            if exc.code == "NOT_FOUND" and native["action"] == "delete":
                return {
                    "status": "succeeded",
                    "native": native,
                    "verification": {"resource": "absent", "business": "not_checked"},
                }
            if exc.code == "NOT_FOUND":
                return {
                    "status": "partial",
                    "native": native,
                    "verification": {"resource": "absent_after_write", "business": "not_checked"},
                }
            raise
        meta, spec, status = obj.get("metadata", {}), obj.get("spec", {}), obj.get("status", {})
        if meta.get("uid") != native["uid"]:
            return {
                "status": "partial",
                "native": native,
                "verification": {"resource": "superseded_uid", "business": "not_checked"},
            }
        if native["action"] == "delete":
            return {
                "status": "running",
                "native": native,
                "verification": {"resource": "deleting", "finalizers": meta.get("finalizers", [])},
            }
        if native.get("generation") is not None and meta.get("generation") != native["generation"]:
            return {
                "status": "partial",
                "native": native,
                "verification": {"resource": "superseded_generation", "business": "not_checked"},
            }
        if native["action"] == "scale" and spec.get("replicas") != native["replicas"]:
            return {
                "status": "partial",
                "native": native,
                "verification": {"resource": "desired_replicas_changed", "business": "not_checked"},
            }
        if (
            native["action"] == "restart"
            and spec.get("template", {})
            .get("metadata", {})
            .get("annotations", {})
            .get("kubectl.kubernetes.io/restartedAt")
            != native["restart_marker"]
        ):
            return {
                "status": "partial",
                "native": native,
                "verification": {"resource": "restart_marker_changed"},
            }
        generation = meta.get("generation", 0)
        observed = status.get("observedGeneration", 0)
        kind = native["kind"]
        if kind == "DaemonSet":
            desired = status.get("desiredNumberScheduled", 0)
            ready = status.get("numberReady", 0)
            updated = status.get("updatedNumberScheduled", 0)
            converged = (
                observed >= generation
                and ready == desired
                and updated == desired
                and status.get("numberUnavailable", 0) == 0
            )
        else:
            desired = spec.get("replicas", 1)
            ready = status.get("readyReplicas", 0)
            total = status.get("replicas", 0)
            updated = status.get(
                "updatedReplicas", total if kind in {"ReplicaSet", "ReplicationController"} else 0
            )
            converged = (
                observed >= generation and total == desired and ready == desired and updated == desired
            )
            if kind == "StatefulSet" and desired and native["action"] == "restart":
                strategy = spec.get("updateStrategy", {})
                partition = strategy.get("rollingUpdate", {}).get("partition", 0)
                if strategy.get("type", "RollingUpdate") == "OnDelete" or partition:
                    converged = False
                else:
                    converged &= status.get("currentRevision") == status.get("updateRevision")
        failed = any(
            c.get("type") == "Progressing"
            and c.get("status") == "False"
            and c.get("reason") == "ProgressDeadlineExceeded"
            for c in status.get("conditions", [])
        )
        return {
            "status": "failed" if failed else "succeeded" if converged else "running",
            "native": native,
            "verification": {
                "platform": "applied",
                "resource": "converged" if converged else "pending",
                "desired": desired,
                "ready": ready,
                "observed_generation": observed,
                "business": "not_checked",
                "update_strategy": spec.get("updateStrategy") or spec.get("strategy"),
            },
        }

    async def pod(self, namespace: str, name: str, container: str | None) -> tuple[dict, str]:
        pod = await self.get("v1", "Pod", name, namespace)
        containers = [
            c["name"]
            for key in ("containers", "initContainers", "ephemeralContainers")
            for c in pod.get("spec", {}).get(key, [])
        ]
        if container is None and len(containers) == 1:
            container = containers[0]
        if container not in containers:
            raise ToolError(
                "TARGET_AMBIGUOUS", "Select an exact Pod container.", details={"containers": containers}
            )
        return pod, container

    async def logs(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        *,
        previous: bool = False,
        tail_lines: int = 200,
        since_time: str | None = None,
        max_bytes: int = 65536,
    ) -> dict:
        obj, selected = await self.pod(namespace, pod, container)

        def read():
            query = [
                ("container", selected),
                ("previous", str(previous).lower()),
                ("timestamps", "true"),
                ("tailLines", max(1, min(tail_lines, 10000))),
                ("limitBytes", max_bytes + 1),
            ]
            if since_time:
                try:
                    parsed = datetime.fromisoformat(since_time.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError()
                except ValueError as exc:
                    raise ToolError("ARGUMENT_INVALID", "since_time must be an RFC3339 timestamp.") from exc
                query.append(("sinceTime", since_time))
            path = self._resource("v1", "Pod").path(name=pod, namespace=namespace) + "/log"
            response = self.dynamic.request(
                "get",
                path,
                query_params=query,
                serialize=False,
                header_params={"Accept": "text/plain"},
                _request_timeout=self.connection.timeout_seconds,
            )
            try:
                return response.read(max_bytes + 1)
            finally:
                response.close()
                response.release_conn()

        data = await self._run(read)
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", errors="replace")
        lines = text.splitlines()
        next_time = (
            lines[-1].split(" ", 1)[0]
            if lines and not truncated
            else (lines[-2].split(" ", 1)[0] if len(lines) > 1 else since_time)
        )
        return {
            "target": {**resource_summary(obj), "container": selected},
            "log": text,
            "truncated": truncated,
            "next_since_time": next_time,
            "continuation_note": "Timestamp continuation can overlap; deduplicate boundary lines. Rotation may lose historical logs.",
            "previous": previous,
            "sensitive_output": True,
        }

    async def events(
        self, namespace: str, *, uid: str | None = None, limit: int = 100, cursor: str | None = None
    ) -> dict:
        return await self.list(
            "v1",
            "Event",
            namespace,
            field_selector="involvedObject.uid=" + uid if uid else None,
            limit=limit,
            cursor=cursor,
        )

    async def history(self, api_version: str, kind: str, name: str, namespace: str) -> dict:
        obj = await self.get(api_version, kind, name, namespace)
        child_kind = (
            "ReplicaSet"
            if kind == "Deployment"
            else "ControllerRevision"
            if kind in {"StatefulSet", "DaemonSet"}
            else None
        )
        if not child_kind:
            raise ToolError(
                "UNSUPPORTED", "History supports Deployment, StatefulSet, and DaemonSet controllers."
            )
        page = await self.list("apps/v1", child_kind, namespace, limit=200)
        uid = obj["metadata"]["uid"]
        items = [
            child
            for child in page["items"]
            if any(owner.get("uid") == uid for owner in child.get("metadata", {}).get("ownerReferences", []))
        ]
        return {
            "target": resource_summary(obj),
            "items": [
                {
                    **resource_summary(item),
                    "revision": item.get("revision")
                    or item.get("metadata", {})
                    .get("annotations", {})
                    .get("deployment.kubernetes.io/revision"),
                }
                for item in items
            ],
            "truncated": bool(page["next_cursor"]),
            "next_cursor": page["next_cursor"],
        }

    async def diagnostics(self, api_version: str, kind: str, name: str, namespace: str | None) -> dict:
        obj = await self.get(api_version, kind, name, namespace)
        evidence = {"resource": sanitize(obj)}
        gaps, findings = [], []
        status = obj.get("status", {})
        desired = obj.get("spec", {}).get("replicas")
        if isinstance(desired, int) and status.get("readyReplicas", 0) < desired:
            findings.append(
                {
                    "severity": "high",
                    "reason": "replicas_unavailable",
                    "desired": desired,
                    "ready": status.get("readyReplicas", 0),
                }
            )
        for condition in status.get("conditions", []):
            if condition.get("reason") in {"ProgressDeadlineExceeded", "FailedCreate", "Unschedulable"}:
                findings.append(
                    {"severity": "high", "reason": condition["reason"], "type": condition.get("type")}
                )
        pods = [obj] if kind == "Pod" else []
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "ReplicationController"}:
            selector = obj.get("spec", {}).get("selector", {}).get("matchLabels", {})
            if selector:
                page = await self.list(
                    "v1",
                    "Pod",
                    namespace,
                    selector=",".join(f"{k}={v}" for k, v in selector.items()),
                    limit=200,
                )
                pods = page["items"]
                if page["next_cursor"]:
                    gaps.append(
                        {"source": "pods", "reason": "page_limit", "next_cursor": page["next_cursor"]}
                    )
            else:
                gaps.append({"source": "pods", "reason": "no_simple_matchLabels_selector"})
        evidence["pods"] = [sanitize(pod) for pod in pods]
        for pod in pods:
            statuses = [
                *pod.get("status", {}).get("containerStatuses", []),
                *pod.get("status", {}).get("initContainerStatuses", []),
            ]
            for container in statuses:
                for state in (container.get("state", {}), container.get("lastState", {})):
                    detail = state.get("waiting") or state.get("terminated") or {}
                    reason = detail.get("reason")
                    if reason in {
                        "CrashLoopBackOff",
                        "ErrImagePull",
                        "ImagePullBackOff",
                        "OOMKilled",
                        "CreateContainerConfigError",
                        "RunContainerError",
                    }:
                        findings.append(
                            {
                                "severity": "high",
                                "reason": reason,
                                "pod": pod.get("metadata", {}).get("name"),
                                "container": container.get("name"),
                            }
                        )
        if namespace:
            try:
                event_page = await self.events(namespace, uid=obj.get("metadata", {}).get("uid"))
                evidence["events"] = sanitize(event_page)
                for event in event_page["items"]:
                    if event.get("type") == "Warning":
                        findings.append(
                            {
                                "severity": "warning",
                                "reason": event.get("reason"),
                                "message": sanitize(event.get("message", "")),
                            }
                        )
            except ToolError as exc:
                gaps.append({"source": "events", "code": exc.code})
            try:
                evidence["metrics"] = await self.list(
                    "metrics.k8s.io/v1beta1", "PodMetrics", namespace, limit=100
                )
            except ToolError as exc:
                gaps.append({"source": "metrics", "code": exc.code})
        if kind == "Service" and namespace:
            try:
                evidence["endpoint_slices"] = await self.list(
                    "discovery.k8s.io/v1",
                    "EndpointSlice",
                    namespace,
                    selector="kubernetes.io/service-name=" + name,
                    limit=100,
                )
            except ToolError as exc:
                gaps.append({"source": "endpoint_slices", "code": exc.code})
        return {
            "target": resource_summary(obj),
            "evidence": evidence,
            "findings": findings,
            "gaps": gaps,
            "relationships": obj.get("metadata", {}).get("ownerReferences", []),
            "assessment": "issues_detected" if findings else "no_deterministic_findings",
            "business": "not_checked",
            "automatic_fix_performed": False,
        }

    async def prepare_exec(self, action: str, payload: dict) -> dict:
        if action == "exec_close":
            if payload["session_id"] not in self.exec_sessions:
                raise ToolError(
                    "SESSION_UNAVAILABLE", "Exec output/session is no longer held by this process."
                )
            return {
                "payload": payload,
                "target": {"connection_id": self.connection.id, "session_id": payload["session_id"]},
                "preconditions": {},
                "preview": {"effect": "close transport; remote termination is not guaranteed"},
            }
        argv = payload.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        ):
            raise ToolError("ARGUMENT_INVALID", "argv must be a nonempty array of strings without NUL bytes.")
        if not 1 <= payload.get("timeout_seconds", 30) <= 600:
            raise ToolError("ARGUMENT_INVALID", "exec timeout must be between 1 and 600 seconds.")
        pod, selected = await self.pod(payload["namespace"], payload["pod"], payload.get("container"))
        payload["container"] = selected
        statuses = [
            *pod.get("status", {}).get("containerStatuses", []),
            *pod.get("status", {}).get("initContainerStatuses", []),
            *pod.get("status", {}).get("ephemeralContainerStatuses", []),
        ]
        state = next((s for s in statuses if s.get("name") == selected), {})
        if not state.get("state", {}).get("running"):
            raise ToolError("CONFLICT", "Selected container is not running.")
        return {
            "payload": payload,
            "target": {"connection_id": self.connection.id, **resource_summary(pod), "container": selected},
            "preconditions": {"uid": pod["metadata"]["uid"], "container_id": state.get("containerID")},
            "preview": {
                "argv": list(argv),
                "container": selected,
                "timeout_seconds": payload.get("timeout_seconds", 30),
                "dry_run": "not_applicable",
                "side_effects": "arbitrary remote command",
                "sensitive_output": True,
            },
        }

    async def execute_exec(self, action: str, plan: dict) -> dict:
        payload, pre = plan["payload"], plan["preconditions"]
        if action == "exec_close":
            self.exec_sessions[payload["session_id"]].cancel.set()
            return {
                "status": "succeeded",
                "native": {"action": action, "session_id": payload["session_id"]},
                "verification": {"channel_close_requested": True, "remote_termination_confirmed": False},
            }
        latest = await self.prepare_exec("exec", payload)
        if latest["preconditions"] != pre:
            raise ToolError("CONFLICT", "Pod/container identity changed after preview.")
        if sum(s.status == "running" for s in self.exec_sessions.values()) >= self.connection.max_concurrency:
            raise ToolError("CAPACITY_EXCEEDED", "Too many active exec sessions for this connection.")
        session_id = str(uuid.uuid4())
        session = ExecSession(payload.get("max_output_bytes", 65536))
        self.exec_sessions[session_id] = session

        def run():
            ws = None
            exec_api = None
            try:
                # The SDK stream helper temporarily replaces its client's transport.
                # A dedicated client prevents concurrent REST requests from observing that replacement.
                if self.api is not None:
                    exec_api = TimedApiClient(
                        copy.deepcopy(self.api.configuration), self.connection.timeout_seconds
                    )
                api = client.CoreV1Api(exec_api)
                ws = stream(
                    api.connect_get_namespaced_pod_exec,
                    payload["pod"],
                    payload["namespace"],
                    container=payload["container"],
                    command=payload["argv"],
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                    _preload_content=False,
                    _request_timeout=self.connection.timeout_seconds,
                )
                deadline = time.monotonic() + payload.get("timeout_seconds", 30)
                while ws.is_open():
                    if session.cancel.is_set() or time.monotonic() >= deadline:
                        session.finish(
                            "unknown",
                            None,
                            "channel_cancelled" if session.cancel.is_set() else "wait_timeout",
                        )
                        return
                    ws.update(timeout=0.25)
                    if ws.peek_stdout():
                        session.append("stdout", ws.read_stdout())
                    if ws.peek_stderr():
                        session.append("stderr", ws.read_stderr())
                if ws.peek_stdout():
                    session.append("stdout", ws.read_stdout())
                if ws.peek_stderr():
                    session.append("stderr", ws.read_stderr())
                code = ws.returncode
                session.finish("unknown" if code is None else "succeeded" if code == 0 else "failed", code)
            except Exception:
                session.finish("unknown", None, "exec_transport_interrupted")
            finally:
                if ws:
                    ws.close()
                    # websocket-client.close() can return early after a peer close frame.
                    # Explicit shutdown releases the socket even when connected is already false.
                    socket = getattr(ws, "sock", None)
                    if socket is not None:
                        socket.shutdown()
                if exec_api:
                    exec_api.close()

        task = asyncio.create_task(asyncio.to_thread(run))
        self.exec_tasks.add(task)
        self.exec_handles[session_id] = task
        task.add_done_callback(self.exec_tasks.discard)
        return {
            "status": "running",
            "native": {
                "action": "exec",
                "session_id": session_id,
                "pod": payload["pod"],
                "namespace": payload["namespace"],
                "container": payload["container"],
                "pod_uid": pre["uid"],
            },
            "verification": {"remote_termination_confirmed": False, "business": "not_checked"},
        }

    def exec_output(self, session_id: str, cursor: str | None, limit: int) -> dict:
        session = self.exec_sessions.get(session_id)
        if session is None:
            raise ToolError(
                "SESSION_UNAVAILABLE", "Exec output is volatile and is unavailable after restart or expiry."
            )
        return session.output(cursor, max(8, min(limit, 1048576)))

    async def wait_exec(self, session_id: str) -> None:
        task = self.exec_handles.get(session_id)
        if task:
            await asyncio.shield(task)

    async def close(self) -> None:
        for session in self.exec_sessions.values():
            session.cancel.set()
        if self.exec_tasks:
            await asyncio.gather(*self.exec_tasks, return_exceptions=True)
        if self.api and not self._external:
            await asyncio.to_thread(self.api.close)
