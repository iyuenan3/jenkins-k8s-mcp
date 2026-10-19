from __future__ import annotations

import pytest
from lxml import etree

from jenkins_k8s_mcp.adapters.jenkins import job_route, pipeline_xml, xml_hash
from jenkins_k8s_mcp.errors import ToolError


async def test_logs_resume_every_byte_across_utf8_boundaries(jenkins, jenkins_api):
    cursor = 0
    parts = []
    for _ in range(100):
        result = await jenkins.logs("Folder/Pipeline", 7, cursor, 4)
        assert result["next_cursor"] > cursor or not result["more_data"]
        parts.append(result["log"])
        cursor = result["next_cursor"]
        if not result["more_data"]:
            break
    assert "".join(parts).encode() == jenkins_api.log
    assert cursor == len(jenkins_api.log)


@pytest.mark.parametrize(
    "action,extra",
    [
        ("create", {"job": "Folder/New", "xml": "<project><unknown>keep</unknown></project>"}),
        ("copy", {"job": "Folder/Copy", "source": "Folder/Pipeline"}),
        ("rename", {"job": "Folder/Pipeline", "destination": "Folder/Renamed"}),
        ("move", {"job": "Folder/Pipeline", "destination": "Other/Pipeline"}),
        (
            "config",
            {"job": "Folder/Pipeline", "xml": "<flow-definition><unknown>keep</unknown></flow-definition>"},
        ),
        ("enable", {"job": "Folder/Pipeline"}),
        ("disable", {"job": "Folder/Pipeline"}),
        ("delete", {"job": "Folder/Pipeline"}),
        ("pipeline", {"job": "Folder/Pipeline", "changes": {"mode": "inline", "script": "echo new"}}),
    ],
)
async def test_native_item_actions_have_real_endpoints_and_readback(jenkins, jenkins_api, action, extra):
    plan = await jenkins.prepare(action, extra)
    assert not any(request.method == "POST" for request in jenkins_api.requests)
    result = await jenkins.execute(action, plan)
    assert result["status"] == "succeeded"
    assert any(request.method == "POST" for request in jenkins_api.requests)
    if action == "delete":
        assert result["verification"]["object"] == "absent"


async def test_pipeline_changes_preserve_unknown_plugin_nodes_and_comments(jenkins, jenkins_api):
    old = jenkins_api.jobs["Folder/Pipeline"]["xml"]
    modified = pipeline_xml(
        old,
        {
            "mode": "scm",
            "url": "https://git.example/repo",
            "branch": "feature/x",
            "script_path": "ci/Jenkinsfile",
        },
    )
    root = etree.fromstring(modified.encode())
    assert root.find("unknown").get("plugin") == "other@1"
    assert root.find("unknown/value").text == "keep"
    assert "<!--keep-comment-->" in modified
    assert (
        root.find("definition/scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/url").text
        == "https://git.example/repo"
    )
    assert root.find("definition/scm/branches/hudson.plugins.git.BranchSpec/name").text == "feature/x"
    assert xml_hash(old) != xml_hash(modified)


async def test_config_conflict_prevents_post(jenkins, jenkins_api):
    plan = await jenkins.prepare("disable", {"job": "Folder/Pipeline"})
    jenkins_api.jobs["Folder/Pipeline"]["xml"] = (
        "<flow-definition><description>someone else</description></flow-definition>"
    )
    with pytest.raises(ToolError) as error:
        await jenkins.execute("disable", plan)
    assert error.value.code == "CONFLICT"
    assert not any(request.method == "POST" for request in jenkins_api.requests)


async def test_build_permission_does_not_require_extended_config_read(jenkins, jenkins_api):
    jenkins_api.config_forbidden = True
    plan = await jenkins.prepare(
        "build", {"job": "Folder/Pipeline", "parameters": {"Branch": "feature", "Password": "private-value"}}
    )
    assert "job_signature" in plan["preconditions"]
    assert plan["preview"]["parameters"] == {"Branch": "feature", "Password": "[REDACTED]"}
    assert (await jenkins.execute("build", plan))["native"]["queue_id"] == 42
    with pytest.raises(ToolError) as error:
        await jenkins.prepare("disable", {"job": "Folder/Pipeline"})
    assert error.value.code == "FORBIDDEN"


async def test_buildability_change_blocks_metadata_fallback_plan(jenkins, jenkins_api):
    jenkins_api.config_forbidden = True
    plan = await jenkins.prepare("build", {"job": "Folder/Pipeline", "parameters": {}})
    jenkins_api.jobs["Folder/Pipeline"]["disabled"] = True
    with pytest.raises(ToolError) as error:
        await jenkins.execute("build", plan)
    assert error.value.code == "CONFLICT" and jenkins_api.triggered == 0


async def test_finished_build_is_never_stopped(jenkins, jenkins_api):
    with pytest.raises(ToolError) as error:
        await jenkins.prepare("stop", {"job": "Folder/Pipeline", "build_number": 7})
    assert error.value.code == "CONFLICT"
    jenkins_api.building = True
    plan = await jenkins.prepare("stop", {"job": "Folder/Pipeline", "build_number": 7})
    jenkins_api.building = False
    with pytest.raises(ToolError):
        await jenkins.execute("stop", plan)
    assert not any(request.url.path.endswith("/stop") for request in jenkins_api.requests)


async def test_file_parameter_requires_verified_extension(jenkins, monkeypatch):
    original = jenkins.job

    async def job(name):
        info = await original(name)
        info["property"][0]["parameterDefinitions"].append(
            {"name": "Upload", "type": "FileParameterDefinition"}
        )
        return info

    monkeypatch.setattr(jenkins, "job", job)
    with pytest.raises(ToolError) as error:
        await jenkins.prepare("build", {"job": "Folder/Pipeline", "parameters": {"Upload": "file.txt"}})
    assert error.value.code == "UNSUPPORTED_PARAMETER"


async def test_parameter_validation_and_repeat_semantics(jenkins, jenkins_api):
    with pytest.raises(ToolError):
        await jenkins.prepare("build", {"job": "Folder/Pipeline", "parameters": {"Unknown": "x"}})
    with pytest.raises(ToolError):
        await jenkins.prepare("build", {"job": "Folder/Pipeline", "parameters": {"Mode": "invalid"}})
    plan = await jenkins.prepare(
        "rebuild", {"job": "Folder/Pipeline", "build_number": 7, "parameters": {"Mode": "safe"}}
    )
    assert plan["payload"]["parameters"] == {"Branch": "previous", "Mode": "safe"}
    result = await jenkins.execute("rebuild", plan)
    assert result["native"]["queue_id"] == 42


async def test_queue_cancel_build_stop_and_optional_reports(jenkins, jenkins_api):
    jenkins_api.executing = False
    result = await jenkins.execute("cancel_queue", await jenkins.prepare("cancel_queue", {"queue_id": 42}))
    assert (await jenkins.refresh(result["native"]))["status"] == "cancelled"
    jenkins_api.building = True
    stopped = await jenkins.execute(
        "stop", await jenkins.prepare("stop", {"job": "Folder/Pipeline", "build_number": 7})
    )
    assert (await jenkins.refresh(stopped["native"]))["status"] == "cancelled"
    jenkins_api.report_missing = True
    reports = await jenkins.reports("Folder/Pipeline", 7)
    assert reports["gaps"] == [{"source": "tests", "code": "NOT_FOUND"}]
    assert reports["reports"]["stages"]["stages"][0]["status"] == "SUCCESS"


async def test_job_search_health_and_multibranch_scan(jenkins, jenkins_api):
    assert (await jenkins.health())["version"] == "test-version"
    result = await jenkins.search("pipeline")
    assert result["items"][0]["fullName"] == "Folder/Pipeline"
    jenkins_api.jobs["Folder/Pipeline"]["class"] = (
        "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"
    )
    with pytest.raises(ToolError):
        await jenkins.prepare("build", {"job": "Folder/Pipeline", "parameters": {}})
    plan = await jenkins.prepare("scan", {"job": "Folder/Pipeline"})
    assert (await jenkins.execute("scan", plan))["status"] == "accepted"


@pytest.mark.parametrize("job", ["../x", "A/../B", "A//B", "A/\nx"])
def test_unsafe_job_paths_rejected(job):
    with pytest.raises(ToolError):
        job_route(job)


def test_xml_external_entities_are_rejected():
    with pytest.raises(ToolError):
        xml_hash('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')
