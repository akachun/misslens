"""Bedrock endpoint 설정과 오류 분류(ARCHITECTURE §6.2, §6.3).

scripts/probe_models.py와 (이후) llm.get_llm()이 함께 쓴다. 모델 ID나 리전을 직접 갖지 않고
모두 config/models.yaml에서 가져온다.
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
        """어느 역할이든 쓰는 기본 모델을 중복 없이 설정 순서대로 돌려준다(주 모델 먼저, 그다음 fallback)."""
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
    """Endpoint 순서: BEDROCK_ENDPOINTS(쉼표로 구분한 이름 목록)가 있으면 그 순서, 없으면 설정 순서."""
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
    THROTTLED = "throttled"            # 호출 제한 -> adaptive retry 후 cooldown
    QUOTA_EXCEEDED = "quota_exceeded"  # 일일/누적 할당량 -> 즉시 cooldown
    UNAVAILABLE = "unavailable"        # 리전에 모델이 없거나 접근 권한 없음 -> 프로세스가 끝날 때까지 건너뜀
    TRANSIENT = "transient"            # 타임아웃 / 연결 오류 -> 한 번 재시도
    INVALID_REQUEST = "invalid_request"  # 잘못된 요청 -> 바로 실패, 전환하지 않음
    AUTH = "auth"                      # 자격 증명 문제 -> 중단, endpoint 문제가 아님
    ERROR = "error"                    # 그 밖의 모든 경우


_QUOTA_MARKERS = ("too many tokens per day", "tokens per day", "quota", "daily limit")
_MODEL_ID_MARKERS = ("model identifier", "model id", "on-demand throughput")


def classify_error(exc: BaseException) -> CallStatus:
    """boto/botocore 예외를 §6.3의 전환 동작으로 대응시킨다."""
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
            # Bedrock은 일일 토큰 할당량 초과를 별도 메시지가 붙은 ThrottlingException으로 알린다.
            if any(m in message for m in _QUOTA_MARKERS):
                return CallStatus.QUOTA_EXCEEDED
            return CallStatus.THROTTLED
        if code in ("ResourceNotFoundException", "AccessDeniedException"):
            return CallStatus.UNAVAILABLE
        if code == "ValidationException":
            # 이 리전에 맞지 않는 모델 ID일 때만 그 조합을 unavailable로 표시한다(§6.3).
            # 다른 검증 오류(예: 입력이 너무 김)는 요청의 버그라서 전환해도 소용없다.
            if any(m in message for m in _MODEL_ID_MARKERS):
                return CallStatus.UNAVAILABLE
            return CallStatus.INVALID_REQUEST
        if code in ("UnrecognizedClientException", "InvalidSignatureException", "ExpiredTokenException"):
            return CallStatus.AUTH
        if code in ("ModelNotReadyException", "ServiceUnavailableException", "InternalServerException",
                    "ModelTimeoutException"):
            return CallStatus.TRANSIENT
    return CallStatus.ERROR
