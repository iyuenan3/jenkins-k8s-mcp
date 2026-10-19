from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from difflib import unified_diff
from urllib.parse import quote, urlsplit

import httpx
from lxml import etree

from ..config import Connection
from ..errors import ToolError, UncertainError
from ..security import SENSITIVE, redact_text, sanitize


def xml_tree(xml: str) -> etree._Element:
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
        root = etree.fromstring(xml.encode(), parser)
        if root.getroottree().docinfo.doctype:
            raise ValueError("DTD forbidden")
        return root
    except (ValueError, etree.XMLSyntaxError) as exc:
        raise ToolError("XML_INVALID", "Supply valid XML without DTD or external entities.") from exc


def xml_hash(xml: str) -> str:
    return hashlib.sha256(etree.tostring(xml_tree(xml), method="c14n")).hexdigest()


def job_route(job: str) -> str:
    parts = job.strip("/").split("/")
    if not job.strip("/") or any(not p or p in {".", ".."} or any(ord(c) < 32 for c in p) for p in parts):
        raise ToolError("TARGET_INVALID", "Use a complete slash-separated Jenkins item name.")
    return "/".join("job/" + quote(part, safe="") for part in parts) + "/"


def job_signature(info: dict) -> str:
    fields = {
        key: info.get(key)
        for key in ("fullName", "_class", "buildable", "disabled", "concurrentBuild", "property")
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pipeline_xml(original: str, changes: dict) -> str:
    root = xml_tree(original)
    if root.tag != "flow-definition":
        raise ToolError("UNSUPPORTED", "Structured Pipeline changes require a workflow-job Pipeline.")
    definition = root.find("definition")
    if definition is None:
        definition = etree.SubElement(root, "definition")
    mode = changes.get("mode")

    def put(parent, tag, value):
        node = parent.find(tag)
        if node is None:
            node = etree.SubElement(parent, tag)
        node.text = str(value)
        return node

    if mode == "inline":
        if not isinstance(changes.get("script"), str):
            raise ToolError("ARGUMENT_INVALID", "Inline mode requires script.")
        definition.set("class", "org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition")
        for name in ("scm", "scriptPath", "lightweight"):
            for child in definition.findall(name):
                definition.remove(child)
        put(definition, "script", changes["script"])
        put(definition, "sandbox", str(changes.get("sandbox", True)).lower())
    elif mode == "scm":
        if not all(isinstance(changes.get(k), str) and changes[k] for k in ("url", "branch", "script_path")):
            raise ToolError("ARGUMENT_INVALID", "SCM mode requires url, branch, and script_path.")
        definition.set("class", "org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition")
        for name in ("script", "sandbox"):
            for child in definition.findall(name):
                definition.remove(child)
        scm = definition.find("scm")
        if scm is None:
            scm = etree.SubElement(definition, "scm", {"class": "hudson.plugins.git.GitSCM"})
        if scm.get("class") != "hudson.plugins.git.GitSCM":
            raise ToolError(
                "UNSUPPORTED", "Structured SCM changes support GitSCM; use full native XML for others."
            )
        remotes = scm.find("userRemoteConfigs")
        if remotes is None:
            remotes = etree.SubElement(scm, "userRemoteConfigs")
        if len(remotes) > 1:
            raise ToolError("TARGET_AMBIGUOUS", "Multiple SCM remotes; edit native XML explicitly.")
        remote = (
            remotes[0] if len(remotes) else etree.SubElement(remotes, "hudson.plugins.git.UserRemoteConfig")
        )
        put(remote, "url", changes["url"])
        if "credentials_id" in changes:
            put(remote, "credentialsId", changes["credentials_id"])
        branches = scm.find("branches")
        if branches is None:
            branches = etree.SubElement(scm, "branches")
        if len(branches) > 1:
            raise ToolError("TARGET_AMBIGUOUS", "Multiple SCM branch rules; edit native XML explicitly.")
        branch = branches[0] if len(branches) else etree.SubElement(branches, "hudson.plugins.git.BranchSpec")
        put(branch, "name", changes["branch"])
        put(definition, "scriptPath", changes["script_path"])
        put(definition, "lightweight", str(changes.get("lightweight", True)).lower())
    else:
        raise ToolError("ARGUMENT_INVALID", "Pipeline mode must be inline or scm.")
    return etree.tostring(root, encoding="unicode")


class JenkinsAdapter:
    def __init__(self, connection: Connection, *, client: httpx.AsyncClient | None = None):
        self.connection = connection
        self.base = (connection.url or "").rstrip("/") + "/"
        self._external = client is not None
        if client is None:
            username, token = connection.credentials()
            client = httpx.AsyncClient(
                auth=httpx.BasicAuth(username, token),
                timeout=connection.timeout_seconds,
                verify=connection.verify_tls,
                trust_env=connection.trust_env,
                follow_redirects=False,
            )
        self.client = client
        self.semaphore = asyncio.Semaphore(connection.max_concurrency)

    async def close(self) -> None:
        if not self._external:
            await self.client.aclose()

    async def request(
        self, method: str, path: str, *, write: bool = False, max_bytes: int = 2097152, **kwargs
    ):
        if urlsplit(path).scheme or urlsplit(path).netloc or ".." in path.split("/"):
            raise ToolError("TARGET_INVALID", "Platform paths are server-owned relative paths.")
        async with self.semaphore:
            try:
                async with self.client.stream(method, self.base + path.lstrip("/"), **kwargs) as response:
                    status = response.status_code
                    if status >= 400:
                        if write and status >= 500:
                            raise UncertainError()
                        code = {
                            401: "AUTHENTICATION_FAILED",
                            403: "FORBIDDEN",
                            404: "NOT_FOUND",
                            409: "CONFLICT",
                        }.get(status, "PLATFORM_REJECTED")
                        raise ToolError(
                            code, "Jenkins rejected the request.", details={"http_status": status}
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > max_bytes:
                            if write:
                                raise UncertainError(
                                    "Write response exceeded the safe response budget; inspect operation state."
                                )
                            raise ToolError(
                                "OUTPUT_LIMIT",
                                "Response exceeds the configured read budget; narrow the query.",
                            )
                        body.extend(chunk)
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "")
                        if not write:
                            raise ToolError(
                                "AUTHENTICATION_FAILED",
                                "Jenkins redirected a read; check API URL and credentials.",
                            )
                        if "login" in urlsplit(location).path.lower():
                            raise ToolError("AUTHENTICATION_FAILED", "Jenkins redirected to login.")
                    return httpx.Response(status, headers=response.headers, content=bytes(body))
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise ToolError("CONNECTION_FAILED", "Cannot connect to Jenkins.") from exc
            except (httpx.TransportError, TimeoutError) as exc:
                if write:
                    raise UncertainError() from exc
                raise ToolError(
                    "WAIT_TIMEOUT", "Jenkins read did not complete; retrying this read is safe."
                ) from exc

    async def json(self, path: str, **kwargs) -> dict:
        response = await self.request("GET", path, **kwargs)
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError as exc:
            raise ToolError("PLATFORM_RESPONSE_INVALID", "Jenkins did not return a JSON object.") from exc

    async def crumb(self) -> dict:
        try:
            value = await self.json("crumbIssuer/api/json")
        except ToolError as exc:
            if exc.code in {"NOT_FOUND", "FORBIDDEN"}:
                return {}
            raise
        field = value.get("crumbRequestField", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", field) or field.lower() in {"authorization", "cookie", "host"}:
            raise ToolError("PLATFORM_RESPONSE_INVALID", "Invalid Jenkins crumb header.")
        return {field: value.get("crumb", "")}

    async def health(self) -> dict:
        response = await self.request("GET", "api/json", params={"tree": "mode,nodeDescription"})
        result = {"status": "reachable", "version": response.headers.get("X-Jenkins", "unknown"), "gaps": []}
        try:
            plugins = await self.json(
                "pluginManager/api/json", params={"tree": "plugins[shortName,version,active]"}
            )
            result["plugins"] = plugins.get("plugins", [])
        except ToolError as exc:
            result["gaps"].append({"source": "plugins", "code": exc.code})
        result["permissions"] = "enforced by Jenkins on each request; not inferred from discovery"
        return result

    async def jobs(self, folder: str = "", *, limit: int = 100, offset: int = 0) -> dict:
        limit = max(1, min(limit, 200))
        if offset < 0:
            raise ToolError("ARGUMENT_INVALID", "offset must be nonnegative.")
        route = job_route(folder) if folder else ""
        data = await self.json(
            route + "api/json",
            params={"tree": f"jobs[name,fullName,_class,color,url]{{{offset},{offset + limit + 1}}}"},
        )
        items = data.get("jobs", [])
        return {
            "items": [{k: v for k, v in item.items() if k != "url"} for item in items[:limit]],
            "next_offset": offset + limit if len(items) > limit else None,
            "folder": folder,
        }

    async def search(self, query: str, *, folder: str = "", limit: int = 100, max_depth: int = 4) -> dict:
        limit = max(1, min(limit, 200))
        pending = [(folder, 0)]
        matches, visited = [], set()
        truncated = False
        while pending and len(visited) < 100:
            current, depth = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            page = await self.jobs(current, limit=200)
            truncated |= page["next_offset"] is not None
            for item in page["items"]:
                full = item.get("fullName") or "/".join(filter(None, (current, item["name"])))
                item["fullName"] = full
                if query.casefold() in full.casefold():
                    matches.append(item)
                if any(term in item.get("_class", "") for term in ("Folder", "MultiBranch")):
                    if depth < min(max_depth, 8):
                        pending.append((full, depth + 1))
                    else:
                        truncated = True
                if len(matches) >= limit:
                    return {"items": matches[:limit], "truncated": True, "visited_folders": len(visited)}
        return {"items": matches, "truncated": truncated or bool(pending), "visited_folders": len(visited)}

    async def job(self, job: str) -> dict:
        return await self.json(
            job_route(job) + "api/json",
            params={
                "tree": (
                    "name,fullName,_class,buildable,inQueue,color,disabled,concurrentBuild,"
                    "property[parameterDefinitions[name,type,_class,description,choices,defaultParameterValue[value]]],"
                    "lastBuild[number],nextBuildNumber,actions[_class]"
                )
            },
        )

    async def config(self, job: str) -> str:
        return (await self.request("GET", job_route(job) + "config.xml")).text

    async def build(self, job: str, number: int) -> dict:
        if number < 1:
            raise ToolError(
                "ARGUMENT_INVALID", "Build number must be positive; latest is not an operation identity."
            )
        return await self.json(
            job_route(job) + f"{number}/api/json",
            params={
                "tree": (
                    "number,building,result,timestamp,duration,estimatedDuration,queueId,"
                    "actions[parameters[name,value],lastBuiltRevision[SHA1]],"
                    "changeSets[items[commitId,msg,author[fullName]]],artifacts[fileName,relativePath]"
                )
            },
        )

    async def queue(self, queue_id: int | None = None) -> dict:
        if queue_id is not None and queue_id < 1:
            raise ToolError("ARGUMENT_INVALID", "Queue id must be positive.")
        path = f"queue/item/{queue_id}/api/json" if queue_id is not None else "queue/api/json"
        fields = "id,cancelled,why,blocked,buildable,task[name,fullName,url],executable[number,url]"
        return await self.json(path, params={"tree": fields if queue_id is not None else f"items[{fields}]"})

    async def logs(self, job: str, number: int, cursor: int, max_bytes: int) -> dict:
        if number < 1 or cursor < 0:
            raise ToolError("ARGUMENT_INVALID", "Use a positive build number and nonnegative byte cursor.")
        budget = max(1, min(max_bytes, 1048576))
        route = job_route(job) + f"{number}/logText/progressiveText"
        async with self.semaphore:
            try:
                async with self.client.stream("GET", self.base + route, params={"start": cursor}) as response:
                    if response.status_code != 200:
                        raise ToolError(
                            "PLATFORM_REJECTED",
                            "Cannot read this build log.",
                            details={"http_status": response.status_code},
                        )
                    data = bytearray()
                    truncated = False
                    # Read at most budget + 4, enough to avoid splitting a UTF-8 code point.
                    async for chunk in response.aiter_bytes(chunk_size=min(budget + 4, 8192)):
                        take = min(len(chunk), budget + 4 - len(data))
                        data.extend(chunk[:take])
                        if take < len(chunk) or len(data) >= budget + 4:
                            truncated = True
                            break
                    visible = bytes(data[:budget])
                    try:
                        text = visible.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        if exc.reason == "unexpected end of data" and exc.start > 0:
                            visible = visible[: exc.start]
                            text = visible.decode("utf-8", errors="replace")
                        elif exc.reason == "unexpected end of data" and len(data) >= exc.end:
                            # A budget smaller than one code point still makes forward progress.
                            max_end = min(len(data), 4)
                            for end in range(1, max_end + 1):
                                try:
                                    text = bytes(data[:end]).decode("utf-8")
                                    visible = bytes(data[:end])
                                    break
                                except UnicodeDecodeError:
                                    continue
                            else:
                                text = visible.decode("utf-8", errors="replace")
                        else:
                            text = visible.decode("utf-8", errors="replace")
                    server_cursor = int(response.headers.get("X-Text-Size", cursor + len(data)))
                    next_cursor = cursor + len(visible)
                    more = (
                        truncated
                        or next_cursor < server_cursor
                        or response.headers.get("X-More-Data", "false").lower() == "true"
                    )
                    return {
                        "job": job,
                        "build_number": number,
                        "log": text,
                        "cursor": cursor,
                        "next_cursor": next_cursor,
                        "more_data": more,
                        "truncated": next_cursor < server_cursor,
                        "sensitive_output": True,
                        "encoding": "utf-8",
                    }
            except httpx.TransportError as exc:
                raise ToolError("WAIT_TIMEOUT", "Log read interrupted; retry the same cursor.") from exc

    async def reports(self, job: str, number: int) -> dict:
        build = await self.build(job, number)
        result = {"build": build, "reports": {}, "gaps": []}
        for name, suffix in (("tests", "testReport/api/json"), ("stages", "wfapi/describe")):
            try:
                result["reports"][name] = await self.json(job_route(job) + f"{number}/" + suffix)
            except ToolError as exc:
                result["gaps"].append({"source": name, "code": exc.code})
        return result

    async def prepare(self, action: str, payload: dict) -> dict:
        payload = copy.deepcopy(payload)
        job = payload.get("job", "")
        if job:
            job_route(job)
        preconditions: dict = {}
        original_xml = None
        defaults = {}
        if action in {"create", "copy"}:
            try:
                await self.job(job)
            except ToolError as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                raise ToolError("CONFLICT", "Destination job already exists.")
            preconditions["destination_absent"] = True
            if action == "create":
                xml_hash(payload.get("xml", ""))
            else:
                payload["source"] = str(payload.get("source", ""))
                preconditions["source_config_hash"] = xml_hash(await self.config(payload["source"]))
        elif action == "cancel_queue":
            queue = await self.queue(int(payload["queue_id"]))
            if queue.get("executable"):
                raise ToolError("CONFLICT", "Queue item has started; stop its exact build instead.")
        else:
            info = await self.job(job)
            try:
                original_xml = await self.config(job)
                preconditions["config_hash"] = xml_hash(original_xml)
            except ToolError as exc:
                if exc.code != "FORBIDDEN" or action not in {"build", "rebuild", "stop", "scan"}:
                    raise
                preconditions["job_signature"] = job_signature(info)
            if action == "pipeline":
                payload["xml"] = pipeline_xml(original_xml, payload["changes"])
            if action == "config":
                xml_hash(payload.get("xml", ""))
            if action in {"build", "rebuild"}:
                if not info.get("buildable", False):
                    raise ToolError("CONFLICT", "This job is not buildable.")
                if any(
                    term in info.get("_class", "") for term in ("MultiBranch", "OrganizationFolder", "Folder")
                ):
                    raise ToolError("UNSUPPORTED", "Select a leaf job; use scan for a multibranch container.")
                definitions = [d for p in info.get("property", []) for d in p.get("parameterDefinitions", [])]
                parameters = payload.get("parameters", {})
                if action == "rebuild":
                    prior = await self.build(job, int(payload["build_number"]))
                    parameters = {
                        p["name"]: p.get("value", "")
                        for a in prior.get("actions", [])
                        for p in a.get("parameters", [])
                    }
                    parameters.update(payload.get("parameters", {}))
                if not isinstance(parameters, dict) or any(
                    not isinstance(v, (str, int, float, bool)) for v in parameters.values()
                ):
                    raise ToolError(
                        "UNSUPPORTED_PARAMETER",
                        "Only scalar parameters are supported; file/plugin parameters need a verified extension.",
                    )
                names = {d["name"] for d in definitions}
                if set(parameters) - names:
                    raise ToolError(
                        "ARGUMENT_INVALID",
                        "Unknown build parameter names.",
                        details={"unknown": sorted(set(parameters) - names)},
                    )
                for definition in definitions:
                    name = definition["name"]
                    parameter_type = definition.get("type") or definition.get("_class", "").rsplit(".", 1)[-1]
                    if name in parameters and parameter_type not in {
                        "",
                        "StringParameterDefinition",
                        "TextParameterDefinition",
                        "BooleanParameterDefinition",
                        "ChoiceParameterDefinition",
                        "PasswordParameterDefinition",
                    }:
                        raise ToolError(
                            "UNSUPPORTED_PARAMETER",
                            "This parameter type requires a verified plugin extension.",
                        )
                    if name not in parameters and "defaultParameterValue" in definition:
                        defaults[name] = (
                            "[REDACTED]"
                            if SENSITIVE.search(name) or "Password" in parameter_type
                            else sanitize(definition["defaultParameterValue"].get("value"))
                        )
                    if (
                        name in parameters
                        and definition.get("choices")
                        and str(parameters[name]) not in definition["choices"]
                    ):
                        raise ToolError(
                            "ARGUMENT_INVALID",
                            "A build parameter is outside its discovered choices.",
                            details={"parameter": name},
                        )
                payload["parameters"] = parameters
                payload["parameterized"] = bool(definitions)
            if action == "stop":
                build = await self.build(job, int(payload["build_number"]))
                if not build.get("building"):
                    raise ToolError(
                        "CONFLICT", "This exact build has already finished; there is nothing to stop."
                    )
            if action == "scan" and not any(
                term in info.get("_class", "") for term in ("MultiBranch", "OrganizationFolder")
            ):
                raise ToolError("UNSUPPORTED", "Scan requires a multibranch project or organization folder.")
            if action in {"rename", "move"}:
                destination = payload.get("destination", "").strip("/")
                job_route(destination)
                payload["destination"] = destination
                if action == "rename" and destination.rpartition("/")[0] != job.rpartition("/")[0]:
                    raise ToolError(
                        "ARGUMENT_INVALID", "Rename stays within one folder; use move for relocation."
                    )
                try:
                    await self.job(destination)
                except ToolError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                else:
                    raise ToolError("CONFLICT", "Destination already exists.")
        return {
            "payload": payload,
            "target": {
                "connection_id": self.connection.id,
                "job": job,
                **({"queue_id": payload["queue_id"]} if action == "cancel_queue" else {}),
            },
            "preconditions": preconditions,
            "preview": {
                "action": action,
                "target": job or payload.get("queue_id"),
                "parameter_names": sorted(payload.get("parameters", {})),
                "parameters": sanitize(payload.get("parameters", {})),
                "platform_defaults": defaults,
                "xml_sha256": xml_hash(payload["xml"]) if "xml" in payload else None,
                "xml_diff": redact_text(
                    "\n".join(
                        unified_diff(
                            (original_xml or "").splitlines(),
                            payload["xml"].splitlines(),
                            fromfile="before.xml",
                            tofile="after.xml",
                            lineterm="",
                        )
                    )
                )
                if "xml" in payload
                else None,
                "destination": payload.get("destination"),
                "concurrency": "best_effort_job_metadata_check"
                if "job_signature" in preconditions
                else "best_effort_config_hash_check",
            },
        }

    async def execute(self, action: str, plan: dict) -> dict:
        payload, pre = plan["payload"], plan["preconditions"]
        job = payload.get("job", "")
        route = job_route(job) if job else ""
        if "config_hash" in pre and xml_hash(await self.config(job)) != pre["config_hash"]:
            raise ToolError("CONFLICT", "Job configuration changed; create a new plan.")
        if "job_signature" in pre and job_signature(await self.job(job)) != pre["job_signature"]:
            raise ToolError("CONFLICT", "Job parameters or buildability changed; create a new plan.")
        if (
            "source_config_hash" in pre
            and xml_hash(await self.config(payload["source"])) != pre["source_config_hash"]
        ):
            raise ToolError("CONFLICT", "Copy source changed; create a new plan.")
        if action in {"create", "copy", "rename", "move"}:
            destination = payload.get("destination", job)
            try:
                await self.job(destination)
            except ToolError as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                raise ToolError("CONFLICT", "Destination appeared after preview.")
        headers = await self.crumb()
        native = {"job": job, "action": action}
        if action in {"build", "rebuild"}:
            suffix = "buildWithParameters" if payload["parameterized"] else "build"
            parameters = {
                k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in payload["parameters"].items()
            }
            response = await self.request(
                "POST", route + suffix, write=True, data=parameters, headers=headers
            )
            location = response.headers.get("Location", "")
            match = re.search(r"/queue/item/(\d+)/?(?:\?.*)?$", urlsplit(location).path)
            if not match:
                raise UncertainError("Jenkins accepted a trigger without a usable Queue id.")
            native["queue_id"] = int(match.group(1))
            return {
                "status": "accepted",
                "native": native,
                "verification": {"platform": "accepted", "business": "not_checked"},
            }
        if action in {"create", "copy"}:
            parent, _, name = job.rpartition("/")
            path = (job_route(parent) if parent else "") + "createItem"
            if action == "copy":
                await self.request(
                    "POST",
                    path,
                    write=True,
                    params={"name": name, "mode": "copy", "from": payload["source"]},
                    headers=headers,
                )
            else:
                await self.request(
                    "POST",
                    path,
                    write=True,
                    params={"name": name},
                    content=payload["xml"].encode(),
                    headers={**headers, "Content-Type": "application/xml"},
                )
        elif action in {"config", "pipeline"}:
            await self.request(
                "POST",
                route + "config.xml",
                write=True,
                content=payload["xml"].encode(),
                headers={**headers, "Content-Type": "application/xml"},
            )
        elif action in {"enable", "disable", "delete", "scan"}:
            suffix = {"enable": "enable", "disable": "disable", "delete": "doDelete", "scan": "build"}[action]
            await self.request("POST", route + suffix, write=True, headers=headers)
        elif action == "rename":
            await self.request(
                "POST",
                route + "doRename",
                write=True,
                data={"newName": payload["destination"].rsplit("/", 1)[-1]},
                headers=headers,
            )
            native["job"] = payload["destination"]
        elif action == "move":
            if payload["destination"].rsplit("/", 1)[-1] != job.rsplit("/", 1)[-1]:
                raise ToolError("ARGUMENT_INVALID", "Move preserves the item name; rename separately.")
            destination_folder = payload["destination"].rpartition("/")[0]
            await self.request(
                "POST",
                route + "move/move",
                write=True,
                data={"destination": "/" + destination_folder},
                headers=headers,
            )
            native["job"] = payload["destination"]
        elif action == "cancel_queue":
            queue = await self.queue(int(payload["queue_id"]))
            if queue.get("executable"):
                raise ToolError("CONFLICT", "Queue item started; stop its exact build instead.")
            await self.request(
                "POST", "queue/cancelItem", write=True, params={"id": payload["queue_id"]}, headers=headers
            )
            native["queue_id"] = payload["queue_id"]
        elif action == "stop":
            number = int(payload["build_number"])
            if not (await self.build(job, number)).get("building"):
                raise ToolError(
                    "CONFLICT", "The exact build finished after preview; no stop request was sent."
                )
            await self.request("POST", route + f"{number}/stop", write=True, headers=headers)
            native["build_number"] = number
            return {
                "status": "accepted",
                "native": native,
                "verification": {"platform": "accepted", "cancellation": "pending"},
            }
        else:
            raise ToolError("UNSUPPORTED", "Unknown Jenkins action.")
        verification = {"platform": "accepted", "business": "not_checked"}
        if action in {
            "create",
            "copy",
            "config",
            "pipeline",
            "rename",
            "move",
            "enable",
            "disable",
            "delete",
        }:
            try:
                if action == "delete":
                    try:
                        await self.job(job)
                    except ToolError as exc:
                        if exc.code != "NOT_FOUND":
                            raise
                        verification["object"] = "absent"
                    else:
                        verification["object"] = "still_present"
                else:
                    info = await self.job(native["job"])
                    verification["object"] = "present"
                    if "xml" in payload:
                        verification["configuration"] = (
                            "matched"
                            if xml_hash(await self.config(native["job"])) == xml_hash(payload["xml"])
                            else "different_after_server_normalization"
                        )
                    if action in {"enable", "disable"}:
                        verification["disabled"] = info.get("disabled")
                        verification["enabled_state"] = (
                            "matched" if info.get("disabled") is (action == "disable") else "different"
                        )
                    if action == "copy":
                        verification["configuration"] = (
                            "matched"
                            if xml_hash(await self.config(native["job"])) == pre["source_config_hash"]
                            else "different_after_server_normalization"
                        )
            except ToolError:
                verification["readback"] = "unavailable"
        status = "accepted" if action in {"scan", "cancel_queue"} else "succeeded"
        if (
            verification.get("configuration") == "different_after_server_normalization"
            or verification.get("object") == "still_present"
            or verification.get("readback") == "unavailable"
            or verification.get("enabled_state") == "different"
        ):
            status = "partial"
        return {"status": status, "native": native, "verification": verification}

    async def refresh(self, native: dict) -> dict:
        native = copy.deepcopy(native)
        action = native.get("action")
        if action in {"build", "rebuild"}:
            if not native.get("build_number"):
                item = await self.queue(native["queue_id"])
                if item.get("cancelled"):
                    return {
                        "status": "cancelled",
                        "native": native,
                        "verification": {"platform": "cancelled"},
                    }
                executable = item.get("executable")
                if not executable:
                    return {"status": "accepted", "native": native, "verification": {"queue": "waiting"}}
                native["build_number"] = int(executable["number"])
                expected_path = urlsplit(
                    self.base + job_route(native["job"]) + str(native["build_number"]) + "/"
                ).path
                if executable.get("url") and urlsplit(executable["url"]).path.rstrip(
                    "/"
                ) != expected_path.rstrip("/"):
                    raise ToolError("TARGET_MISMATCH", "Queue executable does not belong to the planned job.")
            build = await self.build(native["job"], native["build_number"])
            if build.get("queueId") is not None and build["queueId"] != native["queue_id"]:
                raise ToolError("TARGET_MISMATCH", "Build Queue id differs from this operation.")
            state = (
                "running"
                if build.get("building")
                else {"SUCCESS": "succeeded", "ABORTED": "cancelled"}.get(
                    build.get("result"), "failed" if build.get("result") else "unknown"
                )
            )
            return {
                "status": state,
                "native": native,
                "verification": {"platform": build.get("result") or "running", "business": "not_checked"},
            }
        if action == "stop":
            build = await self.build(native["job"], native["build_number"])
            state = (
                "running"
                if build.get("building")
                else "cancelled"
                if build.get("result") == "ABORTED"
                else "partial"
            )
            return {
                "status": state,
                "native": native,
                "verification": {"platform_result": build.get("result"), "business": "not_checked"},
            }
        if action == "cancel_queue":
            item = await self.queue(native["queue_id"])
            state = (
                "cancelled" if item.get("cancelled") else "partial" if item.get("executable") else "accepted"
            )
            return {"status": state, "native": native, "verification": {"cancellation": state}}
        return {
            "status": "accepted",
            "native": native,
            "verification": {"platform": "scan_requested", "convergence": "not_observable"},
        }
