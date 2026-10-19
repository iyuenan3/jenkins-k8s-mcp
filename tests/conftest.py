from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from jenkins_k8s_mcp.adapters.jenkins import JenkinsAdapter
from jenkins_k8s_mcp.config import Connection

XML = '<flow-definition><description>original</description><unknown plugin="other@1"><value>keep</value></unknown><!--keep-comment--><definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition"><script>echo old</script><sandbox>true</sandbox></definition></flow-definition>'


class JenkinsAPI:
    def __init__(self):
        self.jobs = {
            "Folder/Pipeline": {"xml": XML, "disabled": False},
            "Folder": {"xml": "<com.cloudbees.hudson.plugins.folder.Folder/>", "disabled": False},
        }
        self.requests = []
        self.triggered = 0
        self.executing = True
        self.building = False
        self.cancelled = False
        self.result = "SUCCESS"
        self.lose_write_response = False
        self.log = "开头\nabcdef\n尾部🙂\n".encode()
        self.report_missing = False
        self.config_forbidden = False

    @staticmethod
    def split(path):
        parts = path.strip("/").split("/")
        if parts and parts[0] == "jenkins":
            parts.pop(0)
        names = []
        while len(parts) >= 2 and parts[0] == "job":
            names.append(unquote(parts[1]))
            parts = parts[2:]
        return "/".join(names), "/".join(parts)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        job, suffix = self.split(request.url.path)
        method = request.method
        query = dict(request.url.params)
        if suffix == "crumbIssuer/api/json":
            return httpx.Response(404)
        if suffix == "pluginManager/api/json":
            return httpx.Response(
                200, json={"plugins": [{"shortName": "workflow-job", "version": "test", "active": True}]}
            )
        if suffix.startswith("queue/item/"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "cancelled": self.cancelled,
                    "executable": {
                        "number": 7,
                        "url": "https://internal.example/jenkins/job/Folder/job/Pipeline/7/",
                    }
                    if self.executing
                    else None,
                },
            )
        if suffix == "queue/api/json":
            return httpx.Response(200, json={"items": [{"id": 42}]})
        if suffix == "queue/cancelItem":
            self.cancelled = True
            return httpx.Response(200)
        if suffix == "createItem":
            dest = "/".join(filter(None, (job, query["name"])))
            xml = self.jobs[query["from"]]["xml"] if query.get("mode") == "copy" else request.content.decode()
            self.jobs[dest] = {"xml": xml, "disabled": False}
            return httpx.Response(200)
        if not job and suffix == "api/json":
            return httpx.Response(
                200,
                headers={"X-Jenkins": "test-version"},
                json={
                    "jobs": [
                        {
                            "name": "Folder",
                            "fullName": "Folder",
                            "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                        }
                    ]
                },
            )
        if job not in self.jobs:
            return httpx.Response(404)
        entry = self.jobs[job]
        if suffix == "config.xml":
            if method == "GET" and self.config_forbidden:
                return httpx.Response(403)
            if method == "POST":
                entry["xml"] = request.content.decode()
                return httpx.Response(200)
            return httpx.Response(200, text=entry["xml"])
        if suffix == "api/json":
            if "jobs[" in query.get("tree", ""):
                return httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "name": "Pipeline",
                                "fullName": "Folder/Pipeline",
                                "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "name": job.rsplit("/", 1)[-1],
                    "fullName": job,
                    "_class": entry.get("class", "org.jenkinsci.plugins.workflow.job.WorkflowJob"),
                    "buildable": not entry["disabled"],
                    "disabled": entry["disabled"],
                    "lastBuild": {"number": 999},
                    "property": [
                        {
                            "parameterDefinitions": [
                                {
                                    "name": "Branch",
                                    "type": "StringParameterDefinition",
                                    "defaultParameterValue": {"value": "main"},
                                },
                                {"name": "Mode", "choices": ["safe", "fast"]},
                                {"name": "Password", "type": "PasswordParameterDefinition"},
                            ]
                        }
                    ],
                },
            )
        if suffix in {"buildWithParameters", "build"}:
            self.triggered += 1
            if self.lose_write_response:
                raise httpx.ReadTimeout("synthetic response lost", request=request)
            return httpx.Response(
                201, headers={"Location": "https://internal.example/jenkins/queue/item/42/"}
            )
        if suffix == "7/api/json":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "queueId": 42,
                    "building": self.building,
                    "result": self.result,
                    "actions": [{"parameters": [{"name": "Branch", "value": "previous"}]}],
                    "artifacts": [{"fileName": "artifact.zip"}],
                },
            )
        if suffix == "7/logText/progressiveText":
            start = int(query.get("start", 0))
            return httpx.Response(
                200,
                content=self.log[start:],
                headers={"X-Text-Size": str(len(self.log)), "X-More-Data": "false"},
            )
        if suffix == "7/testReport/api/json":
            return httpx.Response(404) if self.report_missing else httpx.Response(200, json={"passCount": 2})
        if suffix == "7/wfapi/describe":
            return httpx.Response(200, json={"stages": [{"name": "test", "status": "SUCCESS"}]})
        if suffix == "7/stop":
            self.building, self.result = False, "ABORTED"
            return httpx.Response(200)
        if suffix in {"enable", "disable"}:
            entry["disabled"] = suffix == "disable"
            return httpx.Response(200)
        if suffix == "doDelete":
            del self.jobs[job]
            return httpx.Response(200)
        if suffix == "doRename":
            form = parse_qs(request.content.decode())
            dest = "/".join(filter(None, (job.rpartition("/")[0], form["newName"][0])))
            self.jobs[dest] = self.jobs.pop(job)
            return httpx.Response(302, headers={"Location": "/jenkins/"})
        if suffix == "move/move":
            form = parse_qs(request.content.decode())
            dest = "/".join(filter(None, (form["destination"][0].strip("/"), job.rsplit("/", 1)[-1])))
            self.jobs[dest] = self.jobs.pop(job)
            return httpx.Response(302, headers={"Location": "/jenkins/"})
        return httpx.Response(404)


@pytest.fixture
def jenkins_api():
    return JenkinsAPI()


@pytest.fixture
async def jenkins(jenkins_api):
    transport = httpx.MockTransport(jenkins_api)
    async with httpx.AsyncClient(transport=transport) as http:
        yield JenkinsAdapter(
            Connection(id="j", kind="jenkins", url="https://jenkins.example/jenkins/"), client=http
        )


class FakeResource:
    def __init__(self, api_version, kind, *, namespaced=True):
        self.group_version = api_version
        self.kind = kind
        self.namespaced = namespaced
        self.verbs = ["get", "list", "create", "update", "patch", "delete"]
        self.name = {
            "Deployment": "deployments",
            "Secret": "secrets",
            "Pod": "pods",
            "ConfigMap": "configmaps",
            "Widget": "widgets",
            "Namespace": "namespaces",
        }.get(kind, kind.lower() + "s")
        self.subresources = {}

    def path(self, name=None, namespace=None):
        prefix = (
            "/api/" + self.group_version if "/" not in self.group_version else "/apis/" + self.group_version
        )
        if namespace:
            prefix += "/namespaces/" + namespace
        return prefix + "/" + self.name + ("/" + name if name else "")


class DynamicAPI:
    def __init__(self):
        self.resources = self
        self.objects = {}
        self.calls = []
        self.fail_write = False
        self.available = {
            ("apps/v1", "Deployment"): FakeResource("apps/v1", "Deployment"),
            ("v1", "Pod"): FakeResource("v1", "Pod"),
            ("v1", "ConfigMap"): FakeResource("v1", "ConfigMap"),
            ("v1", "Secret"): FakeResource("v1", "Secret"),
            ("v1", "Namespace"): FakeResource("v1", "Namespace", namespaced=False),
            ("example.com/v1", "Widget"): FakeResource("example.com/v1", "Widget"),
        }

    def get(self, *, api_version, kind):
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        if (api_version, kind) not in self.available:
            raise ResourceNotFoundError("not installed")
        return self.available[api_version, kind]

    def search(self, **kwargs):
        return [
            r
            for (version, kind), r in self.available.items()
            if (not kwargs.get("api_version") or kwargs["api_version"] == version)
            and (not kwargs.get("kind") or kwargs["kind"] == kind)
        ]

    def request(self, method, path, body=None, **kwargs):
        from kubernetes.client.exceptions import ApiException

        self.calls.append((method, path, copy.deepcopy(body), copy.deepcopy(kwargs)))
        method = method.lower()
        if path == "/version":
            return {"gitVersion": "v1.test"}
        if method == "get":
            if path in self.objects:
                return copy.deepcopy(self.objects[path])
            if any(path.endswith("/" + r.name) for r in self.available.values()):
                items = [
                    copy.deepcopy(obj) for key, obj in self.objects.items() if key.rpartition("/")[0] == path
                ]
                return {"items": items, "metadata": {"resourceVersion": "10", "continue": ""}}
            raise ApiException(status=404)
        dry = kwargs.get("dry_run") == "All"
        if not dry and self.fail_write:
            raise TimeoutError("synthetic response loss")
        if method == "delete":
            if not dry:
                self.objects.pop(path)
            return {"kind": "Status", "status": "Success"}
        target_path = path
        if method == "post":
            name = body.get("metadata", {}).get("name") or body["metadata"]["generateName"] + "test"
            target_path += "/" + name
            if target_path in self.objects:
                raise ApiException(status=409)
            result = copy.deepcopy(body)
            result.setdefault("metadata", {})["name"] = name
        elif method == "put":
            result = copy.deepcopy(body)
        else:
            result = copy.deepcopy(self.objects.get(path, {}))
            if isinstance(body, list):
                for op in body:
                    parts = op["path"].strip("/").split("/")
                    node = result
                    for part in parts[:-1]:
                        node = node.setdefault(part, {})
                    if op["op"] == "test":
                        if node.get(parts[-1]) != op["value"]:
                            raise ApiException(status=409)
                    elif op["op"] in {"add", "replace"}:
                        node[parts[-1]] = op["value"]
                    elif op["op"] == "remove":
                        node.pop(parts[-1], None)
            else:

                def merge(target, patch):
                    for key, value in patch.items():
                        if isinstance(value, dict):
                            merge(target.setdefault(key, {}), value)
                        elif value is None:
                            target.pop(key, None)
                        else:
                            target[key] = copy.deepcopy(value)

                merge(result, body)
        old = self.objects.get(target_path, {})
        meta = result.setdefault("metadata", {})
        meta.setdefault("uid", old.get("metadata", {}).get("uid", "uid-created"))
        meta["resourceVersion"] = str(int(old.get("metadata", {}).get("resourceVersion", "0")) + 1)
        meta["generation"] = old.get("metadata", {}).get("generation", 0) + 1
        if not dry:
            self.objects[target_path] = copy.deepcopy(result)
        return result


@pytest.fixture
def dynamic_api():
    api = DynamicAPI()
    obj = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "app",
            "namespace": "test",
            "uid": "uid-app",
            "resourceVersion": "10",
            "generation": 1,
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "test"}},
            "template": {"spec": {"containers": [{"name": "app", "image": "example/image:old"}]}},
        },
        "status": {"replicas": 2, "readyReplicas": 2, "updatedReplicas": 2, "observedGeneration": 1},
    }
    api.objects["/apis/apps/v1/namespaces/test/deployments/app"] = obj
    api.objects["/api/v1/namespaces/test/pods/pod"] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "pod", "namespace": "test", "uid": "pod-uid", "resourceVersion": "4"},
        "spec": {"containers": [{"name": "app", "image": "example/image"}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "app",
                    "containerID": "runtime://old",
                    "state": {"running": {"startedAt": "2026-10-19T00:00:00Z"}},
                }
            ]
        },
    }
    return api


@pytest.fixture
def kube(dynamic_api, tmp_path):
    from jenkins_k8s_mcp.adapters.kubernetes import KubernetesAdapter

    connection = Connection(
        id="k", kind="kubernetes", kubeconfig="/not-read", context="one", namespace="test"
    )
    return KubernetesAdapter(connection, cache_dir=str(tmp_path / "discovery"), dynamic=dynamic_api)


@pytest.fixture
def platform_server(jenkins_api, dynamic_api):
    """Real loopback HTTP + WebSocket, with synthetic platform state only."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_PATCH(self):
            self.handle_request()

        def do_PUT(self):
            self.handle_request()

        def do_DELETE(self):
            self.handle_request()

        def reply(self, status, body, headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type", "application/json" if not isinstance(body, bytes) else "text/plain"
            )
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                if key.lower() not in {"content-length", "content-type"}:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            from urllib.parse import urlsplit

            from kubernetes.client.exceptions import ApiException

            url = urlsplit(self.path)
            path = url.path
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if path.startswith("/jenkins/"):
                request = httpx.Request(
                    self.command,
                    "http://" + self.headers["Host"] + self.path,
                    headers=dict(self.headers),
                    content=content,
                )
                try:
                    response = jenkins_api(request)
                except httpx.TransportError:
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                self.reply(response.status_code, response.content, dict(response.headers))
                return
            if path.endswith("/exec"):
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.send_header("Sec-WebSocket-Protocol", "v4.channel.k8s.io")
                self.end_headers()
                for channel, data in [
                    (1, b"wire-stdout\n"),
                    (2, b"wire-stderr\n"),
                    (3, b'{"status":"Success"}'),
                ]:
                    payload = bytes([channel]) + data
                    header = (
                        bytes([0x82, len(payload)])
                        if len(payload) < 126
                        else b"\x82\x7e" + struct.pack("!H", len(payload))
                    )
                    self.wfile.write(header + payload)
                    self.wfile.flush()
                self.wfile.write(b"\x88\x00")
                self.wfile.flush()
                self.close_connection = True
                return
            if path.endswith("/log"):
                self.reply(200, b"2026-10-19T00:00:00Z wire-log\n")
                return
            if path == "/version":
                self.reply(200, {"major": "1", "minor": "37", "gitVersion": "v1.test"})
                return
            if path == "/api":
                self.reply(200, {"kind": "APIVersions", "versions": ["v1"]})
                return
            versions = sorted({version for version, kind in dynamic_api.available if "/" in version})
            if path == "/apis":
                groups = [
                    {
                        "name": version.rsplit("/", 1)[0],
                        "versions": [{"groupVersion": version, "version": version.rsplit("/", 1)[-1]}],
                        "preferredVersion": {"groupVersion": version, "version": version.rsplit("/", 1)[-1]},
                    }
                    for version in versions
                ]
                self.reply(200, {"kind": "APIGroupList", "groups": groups})
                return
            for version in ["v1", *versions]:
                discovery_path = ("/api/" if version == "v1" else "/apis/") + version
                if path == discovery_path:
                    resources = [
                        {
                            "name": r.name,
                            "singularName": "",
                            "kind": r.kind,
                            "namespaced": r.namespaced,
                            "verbs": r.verbs,
                        }
                        for (v, k), r in dynamic_api.available.items()
                        if v == version
                    ]
                    if version == "v1":
                        resources.extend(
                            [
                                {
                                    "name": "pods/" + name,
                                    "kind": "Pod",
                                    "namespaced": True,
                                    "verbs": ["get", "create"],
                                }
                                for name in ["exec", "log"]
                            ]
                        )
                    self.reply(
                        200, {"kind": "APIResourceList", "groupVersion": version, "resources": resources}
                    )
                    return
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            options = {}
            if "dryRun" in query:
                options["dry_run"] = query["dryRun"]
            if "fieldManager" in query:
                options["field_manager"] = query["fieldManager"]
            if "force" in query:
                options["force_conflicts"] = query["force"] == "true"
            try:
                body = json.loads(content) if content else None
                result = dynamic_api.request(self.command.lower(), path, body=body, **options)
                self.reply(201 if self.command == "POST" else 200, result)
            except ApiException as exc:
                self.reply(
                    exc.status,
                    {
                        "kind": "Status",
                        "apiVersion": "v1",
                        "status": "Failure",
                        "message": "synthetic platform rejection",
                        "reason": "NotFound",
                        "code": exc.status,
                    },
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
