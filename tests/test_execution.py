from __future__ import annotations

import asyncio
import json

import pytest

from jenkins_k8s_mcp.config import Connection, Settings
from jenkins_k8s_mcp.core import Engine
from jenkins_k8s_mcp.errors import ToolError
from jenkins_k8s_mcp.store import OperationStore


def settings(tmp_path, *, confirmation=False, ttl=300):
    return Settings(
        state_dir=str(tmp_path / "state"),
        plan_ttl_seconds=ttl,
        connections=[
            Connection(
                id="j",
                kind="jenkins",
                url="https://jenkins.example/jenkins/",
                extra_confirmation=confirmation,
            )
        ],
    )


async def test_build_is_bound_to_this_queue_and_exact_build(tmp_path, jenkins, jenkins_api):
    engine = Engine(settings(tmp_path), adapters={"j": jenkins})
    try:
        started = await engine.change(
            "alice", "jenkins", "j", "build", {"job": "Folder/Pipeline", "parameters": {"Branch": "feature"}}
        )
        assert started["status"] == "accepted" and started["native"]["queue_id"] == 42
        final = await engine.wait("alice", started["operation_id"], 1)
        assert final["status"] == "succeeded" and final["native"]["build_number"] == 7
        assert all("/lastBuild/" not in request.url.path for request in jenkins_api.requests)
        assert final["verification"]["business"] == "not_checked"
    finally:
        await engine.close()


async def test_response_loss_is_unknown_and_idempotency_does_not_retrigger(tmp_path, jenkins, jenkins_api):
    engine = Engine(settings(tmp_path), adapters={"j": jenkins})
    jenkins_api.lose_write_response = True
    try:
        payload = {"job": "Folder/Pipeline", "parameters": {"Branch": "main"}}
        result = await engine.change("alice", "jenkins", "j", "build", payload, request_id="one")
        assert result["status"] == "unknown"
        replay = await engine.change("alice", "jenkins", "j", "build", payload, request_id="one")
        assert replay["operation_id"] == result["operation_id"] and replay["duplicate_request"]
        assert jenkins_api.triggered == 1
        with pytest.raises(ToolError, match="different arguments"):
            await engine.change(
                "alice",
                "jenkins",
                "j",
                "build",
                {**payload, "parameters": {"Branch": "other"}},
                request_id="one",
            )
        assert jenkins_api.triggered == 1
    finally:
        await engine.close()


async def test_terminal_state_cannot_regress_from_stale_parallel_observation(tmp_path, jenkins):
    engine = Engine(settings(tmp_path), adapters={"j": jenkins})
    try:
        started = await engine.change(
            "alice", "jenkins", "j", "build", {"job": "Folder/Pipeline", "parameters": {}}
        )
        final = await engine.wait("alice", started["operation_id"], 1)
        assert final["status"] == "succeeded"
        engine.store.update(started["operation_id"], status="running", verification={"platform": "stale"})
        stored = await engine.get("alice", started["operation_id"], refresh=False)
        assert stored["status"] == "succeeded" and stored["verification"]["platform"] == "SUCCESS"
    finally:
        await engine.close()


async def test_preview_and_admin_confirmation_cannot_be_forged_by_identity_arguments(
    tmp_path, jenkins, jenkins_api
):
    engine = Engine(settings(tmp_path, confirmation=True), adapters={"j": jenkins})
    try:
        result = await engine.change(
            "alice", "jenkins", "j", "build", {"job": "Folder/Pipeline", "parameters": {}}
        )
        assert result["status"] == "planned" and jenkins_api.triggered == 0
        with pytest.raises(ToolError) as rejected:
            await engine.execute("bob", result["operation_id"])
        assert rejected.value.code == "OPERATION_NOT_FOUND"
        with pytest.raises(ToolError) as rejected:
            await engine.execute("alice", result["operation_id"])
        assert rejected.value.code == "CONFIRMATION_REQUIRED"
        engine.store.approve(result["operation_id"])
        results = await asyncio.gather(
            *(engine.execute("alice", result["operation_id"]) for _ in range(2)), return_exceptions=True
        )
        assert sum(isinstance(value, dict) for value in results) == 1
        assert jenkins_api.triggered == 1
    finally:
        await engine.close()


async def test_audit_does_not_persist_parameters_xml_output_or_sensitive_preview(tmp_path, jenkins):
    engine = Engine(settings(tmp_path), adapters={"j": jenkins})
    secret = "CANARY-sensitive-value-must-not-persist"
    try:
        plan = await engine.change(
            "alice",
            "jenkins",
            "j",
            "build",
            {"job": "Folder/Pipeline", "parameters": {"Password": secret}},
            preview_only=True,
        )
        await engine.execute("alice", plan["operation_id"])
        for path in engine.store.directory.glob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
        assert secret not in json.dumps(engine.list("alice"))
    finally:
        await engine.close()


async def test_recovery_preserves_unknown_without_automatic_write(tmp_path, jenkins, jenkins_api):
    config = settings(tmp_path)
    engine = Engine(config, adapters={"j": jenkins})
    payload = {"job": "Folder/Pipeline", "parameters": {}}
    result = await engine.change(
        "alice", "jenkins", "j", "build", payload, preview_only=True, request_id="recover"
    )
    engine.store.claim(result["operation_id"], "alice", require_approval=False)
    await engine.close()
    restored = Engine(config, adapters={"j": jenkins})
    try:
        previous = await restored.change("alice", "jenkins", "j", "build", payload, request_id="recover")
        assert previous["status"] == "unknown" and jenkins_api.triggered == 0
        assert "No attributable" in (await restored.get("alice", previous["operation_id"]))["recovery"]
    finally:
        await restored.close()


async def test_wait_timeout_does_not_change_running_operation_to_failure(tmp_path, jenkins, jenkins_api):
    jenkins_api.building = True
    engine = Engine(settings(tmp_path), adapters={"j": jenkins})
    try:
        started = await engine.change(
            "alice", "jenkins", "j", "build", {"job": "Folder/Pipeline", "parameters": {}}
        )
        result = await engine.wait("alice", started["operation_id"], 0.05)
        assert result["wait_timeout"] and result["status"] == "running"
    finally:
        await engine.close()


def test_one_server_owner_and_separate_admin_store(tmp_path):
    owner = OperationStore(str(tmp_path / "state"))
    owner.acquire_owner()
    admin = OperationStore(str(tmp_path / "state"))
    with pytest.raises(ToolError) as rejected:
        admin.acquire_owner()
    assert rejected.value.code == "STATE_IN_USE"
    owner.close()


async def test_large_output_is_principal_bound_resumable_and_volatile(tmp_path, jenkins):
    config = settings(tmp_path).model_copy(update={"max_output_bytes": 1024})
    engine = Engine(config, adapters={"j": jenkins})
    try:
        original = {"unicode": "中文🙂" * 3000}
        result = engine.bounded("alice", original)
        text = result["chunk"]
        with pytest.raises(ToolError):
            engine.output_read("bob", result["output_id"], 0, 1000)
        while result["next_cursor"] is not None:
            result = engine.output_read("alice", result["output_id"], result["next_cursor"], 1024)
            text += result["chunk"]
        assert json.loads(text) == original
    finally:
        await engine.close()


def test_connection_ambiguity_and_platform_modules_are_independent(tmp_path):
    config = Settings(
        connections=[
            Connection(id="a", kind="jenkins", url="https://a.example"),
            Connection(id="b", kind="jenkins", url="https://b.example"),
        ]
    )
    with pytest.raises(ToolError) as error:
        config.resolve(None, "jenkins")
    assert error.value.code == "TARGET_AMBIGUOUS" and error.value.details["candidates"] == ["a", "b"]
    assert config.resolve("b", "jenkins").url == "https://b.example"
