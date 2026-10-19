from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ToolError


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    kind: Literal["jenkins", "kubernetes"]
    enabled: bool = True
    default: bool = False
    url: str | None = None
    username: str | None = None
    username_env: str | None = None
    token_env: str | None = None
    token_file: str | None = None
    kubeconfig: str | None = None
    context: str | None = None
    in_cluster: bool = False
    namespace: str | None = None
    verify_tls: bool | str = True
    trust_env: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    extra_confirmation: bool = False
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_target(self) -> Connection:
        if self.kind == "jenkins":
            if not self.url:
                raise ValueError("Jenkins connections require url")
            parts = urlsplit(self.url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("url must be an HTTP(S) URL")
            if parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("url cannot contain credentials, a query, or a fragment")
        elif not self.in_cluster and (not self.kubeconfig or not self.context):
            raise ValueError(
                "Kubernetes connections require kubeconfig and an explicit context, or in_cluster"
            )
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    def public(self) -> dict:
        return self.model_dump(exclude={"username", "username_env", "token_env", "token_file", "kubeconfig"})

    def credentials(self) -> tuple[str, str]:
        username = os.environ.get(self.username_env, "") if self.username_env else self.username or ""
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        if self.token_file:
            try:
                token = Path(self.token_file).expanduser().read_text().strip()
            except OSError as exc:
                raise ToolError(
                    "CREDENTIAL_UNAVAILABLE", "Cannot read the configured credential file."
                ) from exc
        if not username or not token:
            raise ToolError(
                "CREDENTIAL_UNAVAILABLE", "Set the configured Jenkins username and token references."
            )
        return username, token


class HTTPSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    token_env: str = "MCP_AUTH_TOKEN"
    principal: str = "owner"
    allowed_origins: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "[::1]"])


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state_dir: str = "~/.local/state/jenkins-k8s-mcp"
    plan_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    max_output_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_items: int = Field(default=200, ge=1, le=1000)
    http: HTTPSettings = Field(default_factory=HTTPSettings)
    connections: list[Connection] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_connections(self) -> Settings:
        ids = [c.id for c in self.connections]
        if len(ids) != len(set(ids)):
            raise ValueError("connection ids must be unique")
        for kind in ("jenkins", "kubernetes"):
            if sum(c.default and c.enabled and c.kind == kind for c in self.connections) > 1:
                raise ValueError("at most one default connection per platform")
        return self

    @classmethod
    def load(cls, path: str | None = None) -> Settings:
        source = path or os.environ.get("JENKINS_K8S_MCP_CONFIG")
        if not source:
            return cls()
        try:
            data = yaml.safe_load(Path(source).expanduser().read_text(encoding="utf-8"))
            return cls.model_validate(data or {})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # Pydantic errors can contain supplied secrets, so never echo their raw input.
            raise ToolError(
                "CONFIG_INVALID", "Configuration is unreadable or invalid; check the documented schema."
            ) from exc

    def resolve(self, connection_id: str | None, kind: str | None = None) -> Connection:
        candidates = [c for c in self.connections if c.enabled and (kind is None or c.kind == kind)]
        if connection_id:
            match = next((c for c in candidates if c.id == connection_id), None)
            if match:
                return match
            raise ToolError(
                "CONNECTION_NOT_FOUND", "Connection is missing, disabled, or belongs to another platform."
            )
        defaults = [c for c in candidates if c.default]
        if len(defaults) == 1:
            return defaults[0]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ToolError("CONNECTION_NOT_FOUND", "No enabled connection for the requested platform.")
        raise ToolError(
            "TARGET_AMBIGUOUS", "Specify connection_id.", details={"candidates": [c.id for c in candidates]}
        )
