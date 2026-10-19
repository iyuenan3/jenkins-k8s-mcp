from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """Only safe, deliberately constructed messages cross the protocol boundary."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class UncertainError(ToolError):
    def __init__(self, message: str = "The write may have reached the platform; do not resubmit blindly."):
        super().__init__("RESULT_UNKNOWN", message)
