"""Bedrock endpoint configuration and error classification (ARCHITECTURE §6.2, §6.3).

Shared by scripts/probe_models.py and (later) llm.get_llm(). Holds no model IDs or regions;
everything comes from config/models.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "models.yaml"
MODEL_STATUS_PATH = PROJECT_ROOT / "data" / "model_status.json"


@dataclass(frozen=True)
class Endpoint:
    name: str
    region: str
    prefix: str

    def model_id(self, base_model: str) -> str:
        return f"{self.prefix}.{base_model}"


@dataclass(frozen=True)
class ModelConfig:
    endpoints: list[Endpoint]
    availability: dict[str, list[str]]
    cooldown_seconds: int
    roles: dict[str, dict[str, Any]]

    def is_available(self, endpoint: Endpoint, base_model: str) -> bool:
        return base_model in self.availability.get(endpoint.name, [])

    def role_models(self) -> list[str]:
        """Distinct base models used by any role (primary first, then fallbacks), in config order."""
        seen: list[str] = []
        for role in self.roles.values():
            for key in ("model", "fallback_model"):
                m = role.get(key)
                if m and m not in seen:
                    seen.append(m)
        return seen


def load_model_config(path: Path = DEFAULT_CONFIG_PATH) -> ModelConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    endpoints = [Endpoint(**e) for e in raw["endpoints"]]
    names = {e.name for e in endpoints}
    unknown = set(raw.get("availability", {})) - names
    if unknown:
        raise ValueError(f"availability references unknown endpoints: {sorted(unknown)}")
    return ModelConfig(
        endpoints=endpoints,
        availability={k: list(v) for k, v in raw.get("availability", {}).items()},
        cooldown_seconds=int(raw.get("cooldown_seconds", 900)),
        roles=raw.get("roles", {}),
    )


def resolve_endpoints(config: ModelConfig, override: str | None = None) -> list[Endpoint]:
    """Endpoint order: BEDROCK_ENDPOINTS (comma list of names) if set, else config order."""
    if override is None:
        override = os.environ.get("BEDROCK_ENDPOINTS")
    if not override or not override.strip():
        return list(config.endpoints)
    by_name = {e.name: e for e in config.endpoints}
    order: list[Endpoint] = []
    for name in (n.strip() for n in override.split(",")):
        if not name:
            continue
        if name not in by_name:
            raise ValueError(f"BEDROCK_ENDPOINTS: unknown endpoint {name!r}; known: {list(by_name)}")
        if by_name[name] not in order:
            order.append(by_name[name])
    return order


class CallStatus(str, Enum):
    OK = "ok"
    THROTTLED = "throttled"            # rate limit -> adaptive retry, then cooldown
    QUOTA_EXCEEDED = "quota_exceeded"  # daily/cumulative quota -> cooldown immediately
    UNAVAILABLE = "unavailable"        # model not in region / no access -> skip for process lifetime
    TRANSIENT = "transient"            # timeout / connection -> retry once
    INVALID_REQUEST = "invalid_request"  # malformed request -> fail now, no switching
    AUTH = "auth"                      # credentials problem -> abort, not an endpoint issue
    ERROR = "error"                    # anything else


_QUOTA_MARKERS = ("too many tokens per day", "tokens per day", "quota", "daily limit")
_MODEL_ID_MARKERS = ("model identifier", "model id", "on-demand throughput")


def classify_error(exc: BaseException) -> CallStatus:
    """Map a boto/botocore exception to the §6.3 switching action."""
    from botocore.exceptions import (
        ClientError,
        ConnectionError as BotoConnectionError,
        NoCredentialsError,
        PartialCredentialsError,
        ReadTimeoutError,
    )

    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return CallStatus.AUTH
    if isinstance(exc, (ReadTimeoutError, BotoConnectionError, TimeoutError)):
        return CallStatus.TRANSIENT
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = err.get("Code", "")
        message = (err.get("Message") or "").lower()
        if code == "ServiceQuotaExceededException":
            return CallStatus.QUOTA_EXCEEDED
        if code in ("ThrottlingException", "TooManyRequestsException"):
            # Bedrock reports daily token quota as ThrottlingException with a distinct message.
            if any(m in message for m in _QUOTA_MARKERS):
                return CallStatus.QUOTA_EXCEEDED
            return CallStatus.THROTTLED
        if code in ("ResourceNotFoundException", "AccessDeniedException"):
            return CallStatus.UNAVAILABLE
        if code == "ValidationException":
            # Only a bad model ID for this region marks the combination unavailable (§6.3).
            # Other validation errors (e.g. input too long) are request bugs: switching would not help.
            if any(m in message for m in _MODEL_ID_MARKERS):
                return CallStatus.UNAVAILABLE
            return CallStatus.INVALID_REQUEST
        if code in ("UnrecognizedClientException", "InvalidSignatureException", "ExpiredTokenException"):
            return CallStatus.AUTH
        if code in ("ModelNotReadyException", "ServiceUnavailableException", "InternalServerException",
                    "ModelTimeoutException"):
            return CallStatus.TRANSIENT
    return CallStatus.ERROR
