"""The capability directory points to real tool names, never placeholder registrations."""

CAPABILITIES = [
    {
        "id": "connections",
        "tools": ["connections_list", "connection_check", "server_info"],
        "objects": "enabled Jenkins/Kubernetes connections",
        "requires": "configured credential references",
    },
    {
        "id": "jenkins.jobs",
        "tools": [
            "jenkins_jobs_list",
            "jenkins_jobs_search",
            "jenkins_job_get",
            "jenkins_job_config",
            "jenkins_job_change",
        ],
        "objects": "native Jenkins items and Folder paths",
        "requires": "Remote Access API; item-type permissions; move requires Folder relocation",
    },
    {
        "id": "jenkins.pipeline",
        "tools": [
            "jenkins_pipeline_change",
            "jenkins_multibranch_scan",
            "jenkins_job_config",
            "jenkins_job_change",
        ],
        "objects": "workflow-job, GitSCM Pipeline, multibranch/organization folder; native XML for other plugins",
        "requires": "workflow-job/workflow-cps/git or corresponding multibranch plugins",
    },
    {
        "id": "jenkins.builds",
        "tools": [
            "jenkins_build_start",
            "jenkins_build_repeat",
            "jenkins_build_get",
            "jenkins_queue_get",
            "jenkins_queue_cancel",
            "jenkins_build_stop",
            "operation_get",
            "operation_wait",
        ],
        "objects": "leaf jobs, exact queue IDs and build numbers",
        "requires": "scalar parameter definitions; plugin-specific/file parameters are reported unsupported",
    },
    {
        "id": "jenkins.evidence",
        "tools": ["jenkins_build_log", "jenkins_build_reports", "operation_output"],
        "objects": "exact build; logs, changes, parameters, artifacts, test/stage reports",
        "requires": "JUnit/Pipeline REST reports when installed",
    },
    {
        "id": "kubernetes.discovery",
        "tools": ["k8s_discover", "k8s_resources_list", "k8s_resource_get"],
        "objects": "discovered built-in APIs and CRDs, namespaced or cluster-scoped",
        "requires": "API discovery and per-request RBAC",
    },
    {
        "id": "kubernetes.change",
        "tools": ["k8s_resource_change"],
        "objects": "create/update/patch/apply/delete of discovered resources and supported subresources",
        "requires": "advertised API verb, server dry-run, UID/resourceVersion preconditions; patch type varies by API",
    },
    {
        "id": "kubernetes.workloads",
        "tools": [
            "k8s_workload_restart",
            "k8s_workload_scale",
            "k8s_rollout_history",
            "operation_get",
            "operation_wait",
        ],
        "objects": "restart: Deployment/StatefulSet/DaemonSet; scale: replica controllers",
        "requires": "controller-specific update strategy and observation; scale-to-zero supported",
    },
    {
        "id": "kubernetes.configuration",
        "tools": ["k8s_resource_change", "k8s_resource_get", "k8s_secret_get"],
        "objects": "ConfigMap, Secret, env/valueFrom, image, requests/limits and arbitrary supported fields",
        "requires": "explicit target, valid manifest/patch, native permissions; sensitive inputs stay volatile",
    },
    {
        "id": "kubernetes.diagnostics",
        "tools": ["k8s_diagnose", "k8s_pod_logs", "k8s_events", "k8s_resources_list"],
        "objects": "Pod/container state, current/previous logs, events, metrics, EndpointSlices and owner references",
        "requires": "optional APIs are reported as gaps; deterministic findings never imply business acceptance",
    },
    {
        "id": "kubernetes.exec",
        "tools": ["k8s_exec", "operation_output", "operation_cancel"],
        "objects": "noninteractive argv in one exact running Pod/container",
        "requires": "pods/exec permission and streaming transport; timeout/close does not prove remote termination",
        "limitations": ["interactive stdin/TTY is not implemented; clients can use noninteractive exec"],
    },
    {
        "id": "operations",
        "tools": [
            "operation_get",
            "operation_list",
            "operation_execute",
            "operation_wait",
            "operation_output",
            "operation_cancel",
            "output_read",
        ],
        "objects": "principal-bound frozen plans and durable operation metadata",
        "requires": "one server per state directory; optional admin CLI approval; no cross-platform exactly-once guarantee",
    },
]


def directory(registered: list[str]) -> list[dict]:
    result = []
    for capability in CAPABILITIES:
        prefix = (
            "jenkins_"
            if capability["id"].startswith("jenkins.")
            else "k8s_"
            if capability["id"].startswith("kubernetes.")
            else None
        )
        enabled = prefix is None or any(name.startswith(prefix) for name in registered)
        tools = [tool for tool in capability["tools"] if tool in registered] if enabled else []
        result.append(
            {
                **capability,
                "tools": tools,
                "implementation": "implemented" if tools else "module_disabled",
                "platform_verification": "requires_target_acceptance",
            }
        )
    return result
