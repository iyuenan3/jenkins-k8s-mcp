from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from jenkins_k8s_mcp.adapters.kubernetes import KubernetesAdapter
from jenkins_k8s_mcp.config import Connection, Settings
from jenkins_k8s_mcp.core import Engine
from jenkins_k8s_mcp.server import build_server
from jenkins_k8s_mcp.store import OperationStore

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"


def kubeconfig(path, server):
    value = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "wrong",
        "contexts": [
            {"name": "chosen", "context": {"cluster": "chosen", "user": "chosen"}},
            {"name": "wrong", "context": {"cluster": "wrong", "user": "chosen"}},
        ],
        "clusters": [
            {"name": "chosen", "cluster": {"server": server}},
            {"name": "wrong", "cluster": {"server": "http://127.0.0.1:9"}},
        ],
        "users": [{"name": "chosen", "user": {"token": "synthetic-cluster-token"}}],
    }
    path.write_text(yaml.safe_dump(value))
    return str(path)


async def test_real_official_kubernetes_transport_discovery_crd_secret_logs_and_websocket(
    tmp_path, platform_server
):
    config_path = tmp_path / "kubeconfig.yaml"
    kubeconfig(config_path, platform_server)
    original = config_path.read_bytes()
    adapter = KubernetesAdapter(
        Connection(
            id="cluster",
            kind="kubernetes",
            kubeconfig=str(config_path),
            context="chosen",
            namespace="test",
            timeout_seconds=3,
        ),
        cache_dir=str(tmp_path / "cache"),
    )
    try:
        found = await adapter.discover()
        assert any(item["kind"] == "Widget" for item in found["items"])
        resource = await adapter.get("apps/v1", "Deployment", "app", "test")
        assert resource["metadata"]["uid"] == "uid-app"
        result = await adapter.execute(
            "scale",
            await adapter.prepare(
                "scale",
                {
                    "api_version": "apps/v1",
                    "kind": "Deployment",
                    "name": "app",
                    "namespace": "test",
                    "replicas": 0,
                },
            ),
        )
        assert result["native"]["uid"] == "uid-app"
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "created", "namespace": "test"},
            "data": {"value": "c3ludGhldGlj"},
        }
        created = await adapter.execute(
            "create",
            await adapter.prepare(
                "create", {"api_version": "v1", "kind": "Secret", "namespace": "test", "manifest": secret}
            ),
        )
        assert created["result"]["data"] == "[REDACTED]"
        logs = await adapter.logs("test", "pod", "app", since_time="2026-10-19T00:00:00Z")
        assert "wire-log" in logs["log"]
        plan = await adapter.prepare(
            "exec",
            {
                "namespace": "test",
                "pod": "pod",
                "container": "app",
                "argv": ["printf", "test"],
                "timeout_seconds": 3,
            },
        )
        execution = await adapter.execute("exec", plan)
        await asyncio.wait_for(asyncio.gather(*list(adapter.exec_tasks)), 5)
        output = adapter.exec_output(execution["native"]["session_id"], None, 65536)
        assert output["status"] == "succeeded", output
        assert (
            output["exit_code"] == 0
            and output["stdout"] == "wire-stdout\n"
            and output["stderr"] == "wire-stderr\n"
        )
        assert config_path.read_bytes() == original
    finally:
        await adapter.close()


@pytest.mark.parametrize("kinds", [[], ["jenkins"], ["kubernetes"], ["jenkins", "kubernetes"]])
async def test_registered_tools_and_capabilities_match_enabled_modules(tmp_path, kinds):
    connections = []
    if "jenkins" in kinds:
        connections.append(Connection(id="j", kind="jenkins", url="https://jenkins.example"))
    if "kubernetes" in kinds:
        connections.append(Connection(id="k", kind="kubernetes", kubeconfig="/not-loaded", context="one"))
    engine = Engine(Settings(state_dir=str(tmp_path / "state"), connections=connections))
    try:
        server = build_server(engine)
        tools = await server.list_tools()
        names = {tool.name for tool in tools}
        assert any(name.startswith("jenkins_") for name in names) == ("jenkins" in kinds)
        assert any(name.startswith("k8s_") for name in names) == ("kubernetes" in kinds)
        assert all("ctx" not in tool.inputSchema.get("properties", {}) for tool in tools)
        assert all(tool.outputSchema and tool.outputSchema.get("type") == "object" for tool in tools)
        for capability in engine.info()["capabilities"]:
            assert set(capability["tools"]) <= names
        if "kubernetes" in kinds:
            assert next(tool for tool in tools if tool.name == "k8s_exec").annotations.readOnlyHint is False
        if "jenkins" in kinds:
            action = next(tool for tool in tools if tool.name == "jenkins_job_change").inputSchema[
                "properties"
            ]["action"]["enum"]
            assert {"create", "copy", "rename", "move", "config", "enable", "disable", "delete"} == set(
                action
            )
    finally:
        await engine.close()


async def test_real_stdio_subprocess_uses_sdk_and_keeps_stdout_protocol_clean(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(tmp_path / "state"),
                "connections": [{"id": "j", "kind": "jenkins", "url": "https://jenkins.example"}],
            }
        )
    )
    params = StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "jenkins_k8s_mcp", "--config", str(config_path), "serve", "--transport", "stdio"],
    )
    async with asyncio.timeout(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert "jenkins_build_start" in {tool.name for tool in listed.tools}
                result = await session.call_tool("server_info", {})
                assert not result.isError
                assert result.structuredContent["version"] == "0.1.0"
                assert len(result.structuredContent["content_id"]) == 64
                second = await session.call_tool("connections_list", {})
                assert not second.isError and second.structuredContent["connections"][0]["id"] == "j"


async def test_real_http_auth_origin_identity_lifecycle_and_build_chain(tmp_path, platform_server):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(tmp_path / "state"),
                "http": {"port": port},
                "connections": [
                    {
                        "id": "j",
                        "kind": "jenkins",
                        "url": platform_server + "/jenkins/",
                        "username_env": "TEST_JENKINS_USER",
                        "token_env": "TEST_JENKINS_TOKEN",
                        "timeout_seconds": 3,
                    }
                ],
            }
        )
    )
    env = dict(
        os.environ,
        MCP_AUTH_TOKEN="synthetic-http-auth-token",
        TEST_JENKINS_USER="synthetic",
        TEST_JENKINS_TOKEN="synthetic-platform-token",
        PYTHONDONTWRITEBYTECODE="1",
    )
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(name, None)
    process = await asyncio.to_thread(
        subprocess.Popen,
        [
            str(PYTHON),
            "-m",
            "jenkins_k8s_mcp",
            "--config",
            str(config_path),
            "serve",
            "--transport",
            "streamable-http",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": "Bearer synthetic-http-auth-token"}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as probe:
            for _ in range(100):
                if process.poll() is not None:
                    stdout, stderr = await asyncio.to_thread(process.communicate)
                    pytest.fail("server exited: " + stderr.decode()[-3000:])
                try:
                    response = await probe.get(url)
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("HTTP server did not listen")
            assert response.status_code == 401
            assert (
                await probe.get(url, headers={**headers, "Origin": "https://untrusted.example"})
            ).status_code == 403
            assert (await probe.get(url, headers={**headers, "Host": "untrusted.example"})).status_code == 403
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=10) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        info = await session.call_tool("server_info", {})
                        assert not info.isError, info
                        started = await session.call_tool(
                            "jenkins_build_start",
                            {
                                "job": "Folder/Pipeline",
                                "parameters": {"Branch": "main"},
                                "request_id": "http-one",
                            },
                        )
                        assert not started.isError, started
                        operation = started.structuredContent
                        assert operation["native"]["queue_id"] == 42
                        watched = await session.call_tool(
                            "operation_wait", {"operation_id": operation["operation_id"], "wait_seconds": 1}
                        )
                        assert not watched.isError, watched
                        assert (
                            watched.structuredContent["native"]["build_number"] == 7
                            and watched.structuredContent["status"] == "succeeded"
                        )
                        logs = await session.call_tool(
                            "operation_output", {"operation_id": operation["operation_id"]}
                        )
                        assert not logs.isError and "尾部" in logs.structuredContent["log"]
                        assert (
                            OperationStore(str(tmp_path / "state")).get(operation["operation_id"])[
                                "principal"
                            ]
                            == "owner"
                        )
                        repeat = await session.call_tool("connections_list", {})
                        assert not repeat.isError
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.to_thread(process.communicate, timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            await asyncio.to_thread(process.communicate, timeout=5)
