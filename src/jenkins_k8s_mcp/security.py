from __future__ import annotations

import re
from typing import Any

SENSITIVE = re.compile(
    r"password|passwd|token|secret|credential|authorization|private.?key|client.?key", re.I
)
ASSIGNMENT = re.compile(r"(?i)((?:password|token|secret|authorization|api[_-]?key)\s*[:=]\s*)([^\s,;]+)")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def redact_text(value: str) -> str:
    value = BEARER.sub("Bearer [REDACTED]", value)
    return ASSIGNMENT.sub(r"\1[REDACTED]", value)


def sanitize(value: Any, *, key: str = "") -> Any:
    """Best-effort summaries; raw platform content is never persisted even after this filter."""
    if SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        named_secret = SENSITIVE.search(str(value.get("name", ""))) or "PasswordParameter" in str(
            value.get("type", value.get("_class", ""))
        )
        if value.get("kind") == "Secret":
            return {
                k: ("[REDACTED]" if k in {"data", "stringData"} else sanitize(v, key=k))
                for k, v in value.items()
            }
        return {
            k: "[REDACTED]"
            if named_secret and k in {"value", "defaultParameterValue"}
            else sanitize(v, key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def resource_summary(obj: dict) -> dict:
    meta = obj.get("metadata", {})
    return {
        "apiVersion": obj.get("apiVersion"),
        "kind": obj.get("kind"),
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "uid": meta.get("uid"),
        "resourceVersion": meta.get("resourceVersion"),
        "generation": meta.get("generation"),
    }
