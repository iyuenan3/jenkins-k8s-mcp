from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError as MCPToolError
from mcp.server.fastmcp.server import Settings as MCPSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from .core import Engine
from .errors import ToolError
from .security import sanitize

# SDK 1.29 declares a lifespan type before FastMCP exists. Complete it before settings introspection.
MCPSettings.model_rebuild()

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def build_server(engine: Engine, *, http: bool = False) -> FastMCP:
    settings = engine.settings
    mcp = FastMCP(
        "jenkins-k8s-mcp",
        instructions=(
            "Resolve an explicit connection and native object. All mutations create a frozen plan. "
            "preview_only returns that plan; otherwise default policy executes within this request. "
            "Extra confirmation, when enabled, uses the separate trusted admin CLI. "
            "Use operation IDs and returned Queue/Build/UID receipts, never latest, to track an operation. "
            "A timeout or closed exec channel does not prove remote termination. "
            "Platform success, resource convergence and business acceptance are distinct. "
            "Sensitive output is returned only for explicit requests and must not be copied to logs or persistent notes."
        ),
        host=settings.http.host,
        port=settings.http.port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        max_request_body_size=2097152,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host + ":*" for host in settings.http.allowed_hosts] + settings.http.allowed_hosts,
            allowed_origins=settings.http.allowed_origins,
        ),
    )

    def principal(ctx: Context) -> str:
        if not http:
            return "local"
        request = ctx.request_context.request
        if request is None or not request.scope.get("state", {}).get("mcp_principal"):
            raise ToolError("AUTHENTICATION_FAILED", "Missing authenticated transport identity.")
        return request.scope["state"]["mcp_principal"]

    async def invoke(ctx: Context, function):
        try:
            actor = principal(ctx)
            result = await function(actor)
            return engine.bounded(actor, result)
        except ToolError as exc:
            raise MCPToolError(json.dumps({"error": exc.as_dict()}, ensure_ascii=False)) from None
        except Exception:
            raise MCPToolError(
                json.dumps(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "Request failed; no raw platform payload is logged.",
                        }
                    }
                )
            ) from None

    def register(*, read: bool):
        def decorate(function):
            engine.registered_tools.append(function.__name__)
            return mcp.tool(annotations=READ if read else WRITE, structured_output=True)(function)

        return decorate

    @register(read=True)
    async def server_info(ctx: Context) -> dict[str, Any]:
        """Return the application version, actual registered tools, capabilities and verification limits."""

        async def run(_):
            return engine.info()

        return await invoke(ctx, run)

    @register(read=True)
    async def connections_list(ctx: Context) -> dict[str, Any]:
        """List enabled connection IDs, public target information and optional grouping labels, without credentials."""

        async def run(_):
            return {"connections": [c.public() for c in settings.connections if c.enabled]}

        return await invoke(ctx, run)

    @register(read=True)
    async def connection_check(ctx: Context, connection_id: str) -> dict[str, Any]:
        """Read platform version/discovery; permission gaps never imply a grant to mutate."""

        async def run(_):
            connection = settings.resolve(connection_id)
            adapter = await engine.adapter(connection)
            return {"connection_id": connection.id, "kind": connection.kind, **await adapter.health()}

        return await invoke(ctx, run)

    @register(read=False)
    async def operation_execute(ctx: Context, operation_id: str) -> dict[str, Any]:
        """Execute one frozen plan for the authenticated identity. The ID is not an identity credential."""
        return await invoke(ctx, lambda actor: engine.execute(actor, operation_id))

    @register(read=True)
    async def operation_get(ctx: Context, operation_id: str, refresh: bool = True) -> dict[str, Any]:
        """Inspect this operation and optionally refresh its exact native receipt; never infer from latest."""
        return await invoke(ctx, lambda actor: engine.get(actor, operation_id, refresh=refresh))

    @register(read=True)
    async def operation_list(ctx: Context, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """Page the current transport identity's durable operation metadata without raw payloads or output."""

        async def run(actor):
            return engine.list(actor, limit, cursor)

        return await invoke(ctx, run)

    @register(read=True)
    async def operation_wait(ctx: Context, operation_id: str, wait_seconds: float = 10) -> dict[str, Any]:
        """Wait up to 30 seconds. wait_timeout is an observation flag, not an operation failure."""
        return await invoke(ctx, lambda actor: engine.wait(actor, operation_id, wait_seconds))

    @register(read=True)
    async def operation_output(
        ctx: Context, operation_id: str, cursor: str | None = None, max_bytes: int = 65536
    ) -> dict[str, Any]:
        """Read bounded resumable build or exec output. May contain secrets; output is volatile and never audited."""
        return await invoke(
            ctx,
            lambda actor: engine.operation_output(actor, operation_id, cursor=cursor, max_bytes=max_bytes),
        )

    @register(read=False)
    async def operation_cancel(ctx: Context, operation_id: str, preview_only: bool = False) -> dict[str, Any]:
        """Cancel this Queue/Build or close this exec channel. Does not roll back persisted K8s changes."""
        return await invoke(ctx, lambda actor: engine.cancel(actor, operation_id, preview_only=preview_only))

    @register(read=True)
    async def output_read(
        ctx: Context, output_id: str, cursor: int = 0, max_bytes: int = 65536
    ) -> dict[str, Any]:
        """Continue a large JSON result snapshot by returned UTF-8 byte boundary; snapshots expire after 60 seconds."""

        async def run(actor):
            return engine.output_read(actor, output_id, cursor, max_bytes)

        return await invoke(ctx, run)

    kinds = {connection.kind for connection in settings.connections if connection.enabled}
    if "jenkins" in kinds:

        async def jread(connection_id, method, *args, **kwargs):
            connection = settings.resolve(connection_id, "jenkins")
            adapter = await engine.adapter(connection)
            result = await getattr(adapter, method)(*args, **kwargs)
            sensitive = method in {"config", "build", "logs", "reports"}
            return {
                "connection_id": connection.id,
                "result": result if sensitive else sanitize(result),
                "sensitive_output": sensitive,
            }

        @register(read=True)
        async def jenkins_jobs_list(
            ctx: Context,
            connection_id: str | None = None,
            folder: str = "",
            limit: int = 100,
            offset: int = 0,
        ) -> dict[str, Any]:
            """Page the immediate children of a Folder or root; returned full paths are exact targets."""
            return await invoke(
                ctx, lambda _: jread(connection_id, "jobs", folder, limit=limit, offset=offset)
            )

        @register(read=True)
        async def jenkins_jobs_search(
            ctx: Context,
            query: str,
            connection_id: str | None = None,
            folder: str = "",
            limit: int = 100,
            max_depth: int = 4,
        ) -> dict[str, Any]:
            """Bounded Folder traversal and substring search; reports incomplete traversal explicitly."""
            return await invoke(
                ctx,
                lambda _: jread(
                    connection_id, "search", query, folder=folder, limit=limit, max_depth=max_depth
                ),
            )

        @register(read=True)
        async def jenkins_job_get(ctx: Context, job: str, connection_id: str | None = None) -> dict[str, Any]:
            """Read native job class, parameter definitions, buildability and state."""
            return await invoke(ctx, lambda _: jread(connection_id, "job", job))

        @register(read=True)
        async def jenkins_job_config(
            ctx: Context, job: str, connection_id: str | None = None
        ) -> dict[str, Any]:
            """Explicit sensitive read of native config.xml, including unknown plugin configuration; never persisted."""
            return await invoke(ctx, lambda _: jread(connection_id, "config", job))

        @register(read=False)
        async def jenkins_job_change(
            ctx: Context,
            action: Literal["create", "copy", "rename", "move", "config", "enable", "disable", "delete"],
            job: str,
            connection_id: str | None = None,
            xml: str | None = None,
            source: str | None = None,
            destination: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Plan/execute native item management. job is the destination for create/copy; XML writes preserve supplied native fields."""
            payload = {"job": job}
            payload.update(
                {
                    key: value
                    for key, value in {"xml": xml, "source": source, "destination": destination}.items()
                    if value is not None
                }
            )
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    action,
                    payload,
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def jenkins_pipeline_change(
            ctx: Context,
            job: str,
            changes: dict[str, Any],
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Change inline script/sandbox or GitSCM URL/branch/script_path/credentials_id/lightweight, preserving other XML fields."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    "pipeline",
                    {"job": job, "changes": changes},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def jenkins_multibranch_scan(
            ctx: Context,
            job: str,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Request indexing of a discovered multibranch or organization folder; receipt alone does not verify indexing completion."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    "scan",
                    {"job": job},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def jenkins_build_start(
            ctx: Context,
            job: str,
            parameters: dict[str, Any] | None = None,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Trigger with discovered scalar parameter names/choices; track the returned exact Queue and Build."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    "build",
                    {"job": job, "parameters": parameters or {}},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def jenkins_build_repeat(
            ctx: Context,
            job: str,
            build_number: int,
            parameters: dict[str, Any] | None = None,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Repeat a build's recorded scalar parameters against CURRENT job configuration; does not guarantee the same source revision."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    "rebuild",
                    {"job": job, "build_number": build_number, "parameters": parameters or {}},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=True)
        async def jenkins_build_get(
            ctx: Context, job: str, build_number: int, connection_id: str | None = None
        ) -> dict[str, Any]:
            """Get one exact build, parameters, change sets and artifact metadata. No latest fallback."""
            return await invoke(ctx, lambda _: jread(connection_id, "build", job, build_number))

        @register(read=True)
        async def jenkins_queue_get(
            ctx: Context, connection_id: str | None = None, queue_id: int | None = None
        ) -> dict[str, Any]:
            """Read the bounded queue or one native Queue item; IDs remain connection-specific."""
            return await invoke(ctx, lambda _: jread(connection_id, "queue", queue_id))

        @register(read=False)
        async def jenkins_queue_cancel(
            ctx: Context,
            queue_id: int,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Cancel an exact waiting Queue item; a race into a running Build returns a conflict."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    "cancel_queue",
                    {"queue_id": queue_id},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def jenkins_build_stop(
            ctx: Context,
            job: str,
            build_number: int,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Request stopping one exact Build; observe its final result before claiming cancellation."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "jenkins",
                    connection_id,
                    "stop",
                    {"job": job, "build_number": build_number},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=True)
        async def jenkins_build_log(
            ctx: Context,
            job: str,
            build_number: int,
            connection_id: str | None = None,
            cursor: int = 0,
            max_bytes: int = 65536,
        ) -> dict[str, Any]:
            """Read raw, sensitive progressive output by native byte cursor without skipping undisplayed bytes."""
            return await invoke(
                ctx,
                lambda _: jread(
                    connection_id,
                    "logs",
                    job,
                    build_number,
                    cursor,
                    min(max_bytes, settings.max_output_bytes),
                ),
            )

        @register(read=True)
        async def jenkins_build_reports(
            ctx: Context, job: str, build_number: int, connection_id: str | None = None
        ) -> dict[str, Any]:
            """Read artifacts, changes and available JUnit/Pipeline stage reports; missing plugins are explicit gaps."""
            return await invoke(ctx, lambda _: jread(connection_id, "reports", job, build_number))

    if "kubernetes" in kinds:

        async def kread(connection_id, method, *args, sensitive=False, **kwargs):
            connection = settings.resolve(connection_id, "kubernetes")
            adapter = await engine.adapter(connection)
            result = await getattr(adapter, method)(*args, **kwargs)
            return {
                "connection_id": connection.id,
                "result": result if sensitive else sanitize(result),
                "sensitive_output": sensitive,
            }

        @register(read=True)
        async def k8s_discover(
            ctx: Context,
            connection_id: str | None = None,
            api_version: str | None = None,
            kind: str | None = None,
            limit: int = 200,
            offset: int = 0,
        ) -> dict[str, Any]:
            """Discover actual APIs, CRDs, namespaced scope, verbs and subresources. Discovery is not an RBAC grant."""
            return await invoke(
                ctx,
                lambda _: kread(
                    connection_id, "discover", api_version=api_version, kind=kind, limit=limit, offset=offset
                ),
            )

        @register(read=True)
        async def k8s_resources_list(
            ctx: Context,
            api_version: str,
            kind: str,
            connection_id: str | None = None,
            namespace: str | None = None,
            selector: str | None = None,
            field_selector: str | None = None,
            limit: int = 100,
            cursor: str | None = None,
            all_namespaces: bool = False,
        ) -> dict[str, Any]:
            """List one discovered resource type with native pagination/selectors; Secret values are masked."""
            return await invoke(
                ctx,
                lambda _: kread(
                    connection_id,
                    "list",
                    api_version,
                    kind,
                    namespace,
                    selector=selector,
                    field_selector=field_selector,
                    limit=min(limit, settings.max_items),
                    cursor=cursor,
                    all_namespaces=all_namespaces,
                ),
            )

        @register(read=True)
        async def k8s_resource_get(
            ctx: Context,
            api_version: str,
            kind: str,
            name: str,
            connection_id: str | None = None,
            namespace: str | None = None,
            subresource: str | None = None,
        ) -> dict[str, Any]:
            """Read one exact native object or supported subresource; use k8s_secret_get for explicit Secret values."""
            return await invoke(
                ctx,
                lambda _: kread(
                    connection_id, "get", api_version, kind, name, namespace, subresource=subresource
                ),
            )

        @register(read=True)
        async def k8s_secret_get(
            ctx: Context, name: str, namespace: str, connection_id: str | None = None, key: str | None = None
        ) -> dict[str, Any]:
            """Explicit sensitive read of one Secret, optionally one key. Values remain base64 and are never persisted."""

            async def run(_):
                connection = settings.resolve(connection_id, "kubernetes")
                adapter = await engine.adapter(connection)
                obj = await adapter.get("v1", "Secret", name, namespace)
                if key is not None:
                    if key not in obj.get("data", {}):
                        raise ToolError("NOT_FOUND", "Secret key is absent.")
                    obj = {
                        "metadata": {
                            k: obj.get("metadata", {}).get(k)
                            for k in ("name", "namespace", "uid", "resourceVersion")
                        },
                        "data": {key: obj["data"][key]},
                    }
                return {
                    "connection_id": connection.id,
                    "result": obj,
                    "sensitive_output": True,
                    "encoding": "base64",
                }

            return await invoke(ctx, run)

        @register(read=False)
        async def k8s_resource_change(
            ctx: Context,
            action: Literal["create", "update", "patch", "apply", "delete"],
            api_version: str,
            kind: str,
            connection_id: str | None = None,
            name: str | None = None,
            namespace: str | None = None,
            manifest: dict[str, Any] | str | None = None,
            patch: dict[str, Any] | list[dict[str, Any]] | None = None,
            patch_type: Literal["merge", "json", "strategic"] = "merge",
            subresource: str | None = None,
            force_conflicts: bool = False,
            propagation_policy: Literal["Background", "Foreground", "Orphan"] = "Background",
            grace_period_seconds: int | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Plan/execute arbitrary supported resource fields/YAML, including Secret/valueFrom. Server dry-run and UID/version conditions apply."""
            payload = {
                "api_version": api_version,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "patch_type": patch_type,
                "subresource": subresource,
                "force_conflicts": force_conflicts,
                "propagation_policy": propagation_policy,
                "grace_period_seconds": grace_period_seconds,
            }
            if manifest is not None:
                payload["manifest"] = manifest
            if patch is not None:
                payload["patch"] = patch
            if (
                action in {"create", "update", "apply"}
                and manifest is None
                or action == "patch"
                and patch is None
            ):

                async def invalid(_):
                    raise ToolError("ARGUMENT_INVALID", "This action requires its manifest or patch.")

                return await invoke(ctx, invalid)
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "kubernetes",
                    connection_id,
                    action,
                    payload,
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def k8s_workload_restart(
            ctx: Context,
            kind: Literal["Deployment", "StatefulSet", "DaemonSet"],
            name: str,
            namespace: str,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Change the Pod template restart marker, then track the same UID/generation and controller strategy."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "kubernetes",
                    connection_id,
                    "restart",
                    {"api_version": "apps/v1", "kind": kind, "name": name, "namespace": namespace},
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=False)
        async def k8s_workload_scale(
            ctx: Context,
            kind: Literal["Deployment", "StatefulSet", "ReplicaSet", "ReplicationController"],
            name: str,
            namespace: str,
            replicas: int,
            connection_id: str | None = None,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Scale one replica controller, including to zero; detect later HPA/spec changes as superseded."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "kubernetes",
                    connection_id,
                    "scale",
                    {
                        "api_version": "v1" if kind == "ReplicationController" else "apps/v1",
                        "kind": kind,
                        "name": name,
                        "namespace": namespace,
                        "replicas": replicas,
                    },
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

        @register(read=True)
        async def k8s_rollout_history(
            ctx: Context,
            kind: Literal["Deployment", "StatefulSet", "DaemonSet"],
            name: str,
            namespace: str,
            connection_id: str | None = None,
        ) -> dict[str, Any]:
            """Read owner-UID-bound ReplicaSet or ControllerRevision history, with incomplete-page markers."""
            return await invoke(
                ctx, lambda _: kread(connection_id, "history", "apps/v1", kind, name, namespace)
            )

        @register(read=True)
        async def k8s_pod_logs(
            ctx: Context,
            pod: str,
            namespace: str,
            connection_id: str | None = None,
            container: str | None = None,
            previous: bool = False,
            tail_lines: int = 200,
            since_time: str | None = None,
            max_bytes: int = 65536,
        ) -> dict[str, Any]:
            """Read bounded current/previous logs from an exact Pod/container; timestamp continuation can overlap."""
            return await invoke(
                ctx,
                lambda _: kread(
                    connection_id,
                    "logs",
                    namespace,
                    pod,
                    container,
                    previous=previous,
                    tail_lines=tail_lines,
                    since_time=since_time,
                    max_bytes=max(1, min(max_bytes, settings.max_output_bytes)),
                    sensitive=True,
                ),
            )

        @register(read=True)
        async def k8s_events(
            ctx: Context,
            namespace: str,
            connection_id: str | None = None,
            uid: str | None = None,
            limit: int = 100,
            cursor: str | None = None,
        ) -> dict[str, Any]:
            """Read namespace events, optionally filtered by exact involvedObject UID."""
            return await invoke(
                ctx,
                lambda _: kread(
                    connection_id,
                    "events",
                    namespace,
                    uid=uid,
                    limit=min(limit, settings.max_items),
                    cursor=cursor,
                ),
            )

        @register(read=True)
        async def k8s_diagnose(
            ctx: Context,
            api_version: str,
            kind: str,
            name: str,
            connection_id: str | None = None,
            namespace: str | None = None,
        ) -> dict[str, Any]:
            """Aggregate resource/Pod state, owner relationships, events, optional metrics and endpoints; no automatic repair."""
            return await invoke(
                ctx, lambda _: kread(connection_id, "diagnostics", api_version, kind, name, namespace)
            )

        @register(read=False)
        async def k8s_exec(
            ctx: Context,
            pod: str,
            namespace: str,
            argv: list[str],
            connection_id: str | None = None,
            container: str | None = None,
            timeout_seconds: int = 30,
            preview_only: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Start noninteractive exec with an exact argv and container identity. Arbitrary side effects; output is volatile. Closing is not kill."""
            return await invoke(
                ctx,
                lambda actor: engine.change(
                    actor,
                    "kubernetes",
                    connection_id,
                    "exec",
                    {
                        "pod": pod,
                        "namespace": namespace,
                        "argv": argv,
                        "container": container,
                        "timeout_seconds": timeout_seconds,
                        "max_output_bytes": settings.max_output_bytes,
                    },
                    preview_only=preview_only,
                    request_id=request_id,
                ),
            )

    engine.registered_tools.sort()
    return mcp


class AuthBoundary:
    def __init__(self, app, engine: Engine):
        self.app = app
        self.settings = engine.settings.http
        self.token = os.environ.get(self.settings.token_env, "").encode()
        if len(self.token) < 16:
            raise ToolError(
                "AUTH_CONFIG_INVALID",
                "HTTP requires a bearer token of at least 16 bytes in the configured environment variable.",
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("latin1") not in self.settings.allowed_origins:
            return await JSONResponse({"error": "origin_forbidden"}, status_code=403)(scope, receive, send)
        authority = headers.get(b"host", b"").decode("latin1")
        hostname = urlsplit("http://" + authority).hostname
        if hostname not in {name.strip("[]") for name in self.settings.allowed_hosts}:
            return await JSONResponse({"error": "host_forbidden"}, status_code=403)(scope, receive, send)
        supplied = headers.get(b"authorization", b"")
        if not supplied.startswith(b"Bearer ") or not hmac.compare_digest(supplied[7:], self.token):
            return await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="jenkins-k8s-mcp"'},
            )(scope, receive, send)
        scope.setdefault("state", {})["mcp_principal"] = self.settings.principal
        return await self.app(scope, receive, send)


def create_app(engine: Engine):
    server = build_server(engine, http=True)
    app = server.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application) as state:
                yield state
        finally:
            await engine.close()

    app.router.lifespan_context = lifespan
    return AuthBoundary(app, engine)
