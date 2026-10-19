from __future__ import annotations

import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

import jenkins_k8s_mcp.adapters.kubernetes as module
from jenkins_k8s_mcp.config import Settings
from jenkins_k8s_mcp.core import Engine
from jenkins_k8s_mcp.errors import ToolError, UncertainError
from jenkins_k8s_mcp.security import sanitize

PATH = "/apis/apps/v1/namespaces/test/deployments/app"
TARGET = {"api_version": "apps/v1", "kind": "Deployment", "namespace": "test", "name": "app"}


@pytest.mark.parametrize(
    "action,extra",
    [
        ("scale", {"replicas": 0}),
        ("restart", {}),
        (
            "patch",
            {
                "patch": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "app",
                                        "image": "example/new",
                                        "resources": {
                                            "requests": {"cpu": "100m"},
                                            "limits": {"memory": "128Mi"},
                                        },
                                        "env": [
                                            {
                                                "name": "PASSWORD",
                                                "valueFrom": {
                                                    "secretKeyRef": {"name": "secret", "key": "key"}
                                                },
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        ),
        ("patch", {"patch_type": "json", "patch": [{"op": "replace", "path": "/spec/replicas", "value": 3}]}),
        ("update", {}),
        ("apply", {}),
        ("delete", {}),
    ],
)
async def test_resource_actions_use_server_dry_run_and_versioned_real_writes(
    kube, dynamic_api, action, extra
):
    payload = {**TARGET, **extra}
    if action in {"update", "apply"}:
        payload["manifest"] = copy.deepcopy(dynamic_api.objects[PATH])
        payload["manifest"]["spec"]["replicas"] = 3
    original = copy.deepcopy(dynamic_api.objects[PATH])
    plan = await kube.prepare(action, payload)
    assert dynamic_api.objects[PATH] == original
    dry_calls = [c for c in dynamic_api.calls if c[3].get("dry_run") == "All"]
    assert len(dry_calls) == 1
    result = await kube.execute(action, plan)
    writes = [c for c in dynamic_api.calls if c[0] != "get" and c[3].get("dry_run") != "All"]
    assert len(writes) == 1
    body = writes[0][2]
    if action == "delete":
        assert body["preconditions"] == {"uid": "uid-app", "resourceVersion": "10"}
    elif isinstance(body, list):
        assert body[:2] == [
            {"op": "test", "path": "/metadata/uid", "value": "uid-app"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "10"},
        ]
    else:
        assert body["metadata"]["uid"] == "uid-app" and body["metadata"]["resourceVersion"] == "10"
    assert result["verification"]["business"] == "not_checked"


@pytest.mark.parametrize(
    "kind,version,body",
    [
        ("Secret", "v1", {"type": "Opaque", "data": {"password": "Q0FOQVJZLXNlY3JldA=="}}),
        ("Widget", "example.com/v1", {"spec": {"arbitrary": {"nested": ["unknown", "preserved"]}}}),
    ],
)
async def test_secret_and_crd_creates_are_not_silently_excluded(kube, dynamic_api, kind, version, body):
    resource = {"apiVersion": version, "kind": kind, "metadata": {"name": "new", "namespace": "test"}, **body}
    plan = await kube.prepare(
        "create", {"api_version": version, "kind": kind, "manifest": resource, "namespace": "test"}
    )
    result = await kube.execute("create", plan)
    assert result["native"]["uid"] == "uid-created"
    if kind == "Secret":
        assert plan["preview"]["after_dry_run"]["data"] == "[REDACTED]"
        assert result["result"]["data"] == "[REDACTED]"
    else:
        assert result["result"]["spec"] == body["spec"]


async def test_conflict_and_uid_replacement_prevent_write(kube, dynamic_api):
    plan = await kube.prepare("scale", {**TARGET, "replicas": 3})
    dynamic_api.objects[PATH]["metadata"]["uid"] = "replacement"
    with pytest.raises(ToolError) as error:
        await kube.execute("scale", plan)
    assert error.value.code == "CONFLICT"
    assert not any(c[0] != "get" and c[3].get("dry_run") != "All" for c in dynamic_api.calls)


async def test_apply_of_absent_object_uses_atomic_create_and_never_overwrites_racing_object(
    kube, dynamic_api, monkeypatch
):
    target = {"api_version": "v1", "kind": "ConfigMap", "namespace": "test", "name": "new"}
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "new", "namespace": "test"},
        "data": {"key": "planned"},
    }
    plan = await kube.prepare("apply", {**target, "manifest": body})
    assert plan["payload"]["method"] == "post"
    original = dynamic_api.request
    path = "/api/v1/namespaces/test/configmaps/new"

    def racing_request(method, route, **kwargs):
        if method == "post" and not kwargs.get("dry_run"):
            dynamic_api.objects[path] = {**copy.deepcopy(body), "data": {"key": "other-operator"}}
        return original(method, route, **kwargs)

    monkeypatch.setattr(dynamic_api, "request", racing_request)
    with pytest.raises(ToolError) as error:
        await kube.execute("apply", plan)
    assert error.value.code == "CONFLICT"
    assert dynamic_api.objects[path]["data"]["key"] == "other-operator"


@pytest.mark.parametrize("name", ["..", "../pod", "pod?x=1", "pod%2fexec", "pod\\exec", "pod\n"])
async def test_object_path_injection_is_rejected_before_network(kube, dynamic_api, name):
    with pytest.raises(ToolError) as error:
        await kube.get("v1", "Pod", name, "test")
    assert error.value.code == "TARGET_INVALID" and not dynamic_api.calls


async def test_apply_force_is_explicit_and_never_implicitly_retried(kube, dynamic_api):
    body = copy.deepcopy(dynamic_api.objects[PATH])
    plan = await kube.prepare("apply", {**TARGET, "manifest": body, "force_conflicts": False})
    dynamic_api.fail_write = True
    with pytest.raises(UncertainError):
        await kube.execute("apply", plan)
    writes = [c for c in dynamic_api.calls if c[0] != "get" and c[3].get("dry_run") != "All"]
    assert len(writes) == 1 and writes[0][3]["force_conflicts"] is False


async def test_scale_zero_converges_and_other_updates_are_superseded(kube, dynamic_api):
    result = await kube.execute("scale", await kube.prepare("scale", {**TARGET, "replicas": 0}))
    obj = dynamic_api.objects[PATH]
    obj["status"] = {
        "replicas": 0,
        "readyReplicas": 0,
        "updatedReplicas": 0,
        "observedGeneration": obj["metadata"]["generation"],
    }
    assert (await kube.refresh(result["native"]))["status"] == "succeeded"
    obj["metadata"]["generation"] += 1
    obj["spec"]["replicas"] = 5
    changed = await kube.refresh(result["native"])
    assert changed["status"] == "partial" and changed["verification"]["resource"] == "superseded_generation"


async def test_namespace_scope_discovery_pagination_and_unsupported_api(kube):
    found = await kube.discover(limit=2)
    assert len(found["items"]) == 2 and found["next_offset"] == 2
    with pytest.raises(ToolError) as error:
        await kube.get("v1", "Namespace", "test", "test")
    assert error.value.code == "TARGET_INVALID"
    with pytest.raises(ToolError) as error:
        await kube.get("other/v1", "Unknown", "name", "test")
    assert error.value.code == "UNSUPPORTED"


async def test_manifest_target_mismatch_rejected_before_mutation(kube, dynamic_api):
    body = copy.deepcopy(dynamic_api.objects[PATH])
    body["metadata"]["namespace"] = "wrong"
    with pytest.raises(ToolError) as error:
        await kube.prepare("update", {**TARGET, "manifest": body})
    assert error.value.code == "TARGET_MISMATCH"
    assert not dynamic_api.calls


async def test_diagnostics_preserve_api_gaps_and_detect_unready_resources(kube, dynamic_api):
    dynamic_api.objects[PATH]["status"]["readyReplicas"] = 0
    result = await kube.diagnostics("apps/v1", "Deployment", "app", "test")
    assert result["findings"][0]["reason"] == "replicas_unavailable"
    assert any(gap["source"] == "metrics" for gap in result["gaps"])
    assert not result["automatic_fix_performed"] and result["business"] == "not_checked"


class FakeWS:
    def __init__(self, *, running=False):
        self.steps = 0
        self.running = running
        self.closed = False
        self.returncode = 0
        self.out = "stdout-CANARY🙂\n"
        self.err = "stderr-line\n"

    def is_open(self):
        return not self.closed and (self.running or self.steps < 1)

    def update(self, timeout):
        self.steps += 1
        if self.running:
            time.sleep(0.01)

    def peek_stdout(self):
        return bool(self.out)

    def peek_stderr(self):
        return bool(self.err)

    def read_stdout(self):
        out, self.out = self.out, ""
        return out

    def read_stderr(self):
        out, self.err = self.err, ""
        return out

    def close(self):
        self.closed = True


async def test_exec_exact_argv_identity_output_and_exit_without_persistent_content(
    kube, dynamic_api, tmp_path, monkeypatch
):
    seen = []
    ws = FakeWS()
    monkeypatch.setattr(
        module.client, "CoreV1Api", lambda _: SimpleNamespace(connect_get_namespaced_pod_exec=object())
    )

    def fake_stream(method, *args, **kwargs):
        seen.append((args, kwargs))
        return ws

    monkeypatch.setattr(module, "stream", fake_stream)
    config = Settings(state_dir=str(tmp_path / "state"), connections=[kube.connection])
    engine = Engine(config, adapters={"k": kube})
    try:
        argv = ["printf", "%s", "text; remains one argv"]
        result = await engine.change(
            "alice",
            "kubernetes",
            "k",
            "exec",
            {
                "namespace": "test",
                "pod": "pod",
                "argv": argv,
                "container": "app",
                "timeout_seconds": 2,
                "max_output_bytes": 65536,
            },
        )
        await asyncio.gather(*list(kube.exec_tasks))
        await asyncio.gather(*list(engine.background))
        stored = await engine.get("alice", result["operation_id"], refresh=False)
        assert stored["status"] == "succeeded" and stored["verification"]["exit_code"] == 0
        output = await engine.operation_output("alice", result["operation_id"])
        assert output["stdout"] == "stdout-CANARY🙂\n" and output["stderr"] == "stderr-line\n"
        assert output["exit_code"] == 0 and output["remote_termination_confirmed"]
        assert seen[0][1]["command"] == argv and seen[0][1]["stdin"] is False and seen[0][1]["tty"] is False
        for path in engine.store.directory.glob("*"):
            if path.is_file():
                assert b"stdout-CANARY" not in path.read_bytes()
                assert b"text; remains one argv" not in path.read_bytes()
    finally:
        await engine.close()


async def test_exec_cancellation_is_channel_close_and_not_claimed_remote_kill(kube, tmp_path, monkeypatch):
    ws = FakeWS(running=True)
    monkeypatch.setattr(
        module.client, "CoreV1Api", lambda _: SimpleNamespace(connect_get_namespaced_pod_exec=object())
    )
    monkeypatch.setattr(module, "stream", lambda *args, **kwargs: ws)
    engine = Engine(
        Settings(state_dir=str(tmp_path / "state"), connections=[kube.connection]), adapters={"k": kube}
    )
    try:
        result = await engine.change(
            "alice",
            "kubernetes",
            "k",
            "exec",
            {
                "namespace": "test",
                "pod": "pod",
                "argv": ["sleep", "5"],
                "container": "app",
                "timeout_seconds": 3,
            },
        )
        cancelled = await engine.cancel("alice", result["operation_id"])
        assert cancelled["verification"]["remote_termination_confirmed"] is False
        await asyncio.gather(*list(kube.exec_tasks))
        refreshed = await engine.get("alice", result["operation_id"])
        assert refreshed["status"] == "unknown" and refreshed["verification"]["exit_code"] is None
        assert ws.closed
    finally:
        await engine.close()


async def test_exec_pod_replacement_does_not_start_command(kube, dynamic_api, monkeypatch):
    called = []
    monkeypatch.setattr(module, "stream", lambda *a, **kw: called.append(True))
    plan = await kube.prepare(
        "exec", {"namespace": "test", "pod": "pod", "argv": ["true"], "container": "app"}
    )
    dynamic_api.objects["/api/v1/namespaces/test/pods/pod"]["metadata"]["uid"] = "someone-else"
    with pytest.raises(ToolError) as error:
        await kube.execute("exec", plan)
    assert error.value.code == "CONFLICT" and not called


def test_sensitive_mask_and_exec_ring_buffer_are_explicit():
    hidden = sanitize({"kind": "Secret", "data": {"key": "raw"}})
    assert hidden["data"] == "[REDACTED]"
    session = module.ExecSession(10)
    session.append("stdout", "0123456789ABCDEFGHIJ")
    output = session.output(None, 100)
    assert output["stdout"] == "ABCDEFGHIJ" and output["lost_bytes"]["stdout"] == 10 and output["truncated"]
    assert session.output(output["next_cursor"], 100)["stdout"] == ""
    with pytest.raises(ToolError):
        session.output("bad-cursor", 100)
