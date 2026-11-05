"""get_llm(role): endpoint failover를 갖춘 Bedrock 호출(ARCHITECTURE §6, D26, D27).

앱의 모든 LLM 호출은 get_llm(role)을 거친다. 모델 ID, 리전, 타임아웃은 config/models.yaml에서만
가져온다.

    llm = get_llm("generator")
    question = llm.with_structured_output(Question).invoke(prompt)
    text = llm.invoke(prompt).content

전환 규칙(§6.3)은 (endpoint, model) 조합마다 적용한다. botocore 재시도는 끈다
(total_max_attempts=1). 그래서 오류 종류별 재시도 횟수는 router 혼자 정한다(D32):
  - 호출 제한(throttled)              -> exponential backoff로 최대 THROTTLE_MAX_ATTEMPTS번 시도,
                                         그다음 cooldown, 다음 endpoint
  - 할당량 초과                       -> 재시도 없이 cooldown, 다음 endpoint
  - 모델 없음 / 접근 거부             -> 프로세스 동안 unavailable, 다음 endpoint
  - 타임아웃 / 연결 / 5xx             -> 한 번 재시도, 그다음 다음 endpoint
  - 잘못된 요청 / 기타 오류           -> 바로 실패(전환해도 소용없음)
  - 자격 증명                         -> 바로 실패
역할의 모델로 모든 endpoint가 실패하면 -> fallback_model로 반복 -> LLMCallError.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from botocore.config import Config
from langchain_core.runnables import Runnable, RunnableLambda

from misslens.bedrock import (
    MODEL_STATUS_PATH,
    PROJECT_ROOT,
    CallStatus,
    Endpoint,
    ModelConfig,
    classify_error,
    load_model_config,
    resolve_endpoints,
)

logger = logging.getLogger(__name__)

# 이보다 최근의 probe에서 할당량 초과로 확인된 조합은 cooldown 상태로 시작한다.
MODEL_STATUS_MAX_AGE_SECONDS = 24 * 3600
THROTTLE_MAX_ATTEMPTS = 5      # 호출 제한 중 한 endpoint에서 시도하는 총 횟수(D13, D32)
THROTTLE_BASE_DELAY = 1.0      # 초 단위. 재시도마다 두 배가 되고 jitter를 섞는다
THROTTLE_MAX_DELAY = 20.0
TRANSIENT_MAX_ATTEMPTS = 2     # 첫 시도 + 재시도 1번

_SWITCH = {CallStatus.THROTTLED, CallStatus.QUOTA_EXCEEDED, CallStatus.UNAVAILABLE, CallStatus.TRANSIENT}

Combo = tuple[str, str]  # (endpoint 이름, 기본 모델)
LLMFactory = Callable[[Endpoint, str, int], Any]  # (endpoint, 기본 모델, 타임아웃) -> chat model


@dataclass
class Attempt:
    endpoint: str
    model: str
    status: str
    error: str = ""


class LLMCallError(RuntimeError):
    """허용된 endpoint x model 조합이 모두 실패했거나, 전환할 수 없는 오류였다."""

    def __init__(self, role: str, attempts: list[Attempt], cause: BaseException | None = None):
        self.role = role
        self.attempts = attempts
        self.cause = cause
        summary = "; ".join(f"{a.endpoint}:{a.model}={a.status}" for a in attempts) or "no candidate"
        last = next((a.error for a in reversed(attempts) if a.error), "")
        super().__init__(f"LLM call failed for role {role!r}: {summary}" + (f" ({last})" if last else ""))


@dataclass
class RouterState:
    """cooldown, unavailable 조합, 역할별 마지막 성공 endpoint를 프로세스 전체에서 기억한다."""

    cooldown_until: dict[Combo, float] = field(default_factory=dict)
    unavailable: set[Combo] = field(default_factory=set)
    last_success: dict[str, str] = field(default_factory=dict)  # 역할 -> endpoint 이름
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_cooling(self, combo: Combo, now: float) -> bool:
        return self.cooldown_until.get(combo, 0.0) > now

    def seed_from_model_status(self, path: Path, cooldown_seconds: int, now: float,
                               wall_now: datetime | None = None) -> list[Combo]:
        """최근 probe에서 할당량 초과로 나온 조합을 cooldown 상태로 시작한다(§6.2)."""
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            probed_at = datetime.fromisoformat(data["probed_at"])
            if probed_at.tzinfo is None:
                probed_at = probed_at.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.warning("ignoring unreadable %s", path)
            return []
        wall_now = wall_now or datetime.now(timezone.utc)
        if (wall_now - probed_at).total_seconds() > MODEL_STATUS_MAX_AGE_SECONDS:
            return []
        seeded = []
        for entry in data.get("quota_exceeded", []):
            endpoint, _, model = entry.partition(":")
            combo = (endpoint, model)
            self.cooldown_until[combo] = now + cooldown_seconds
            seeded.append(combo)
        return seeded


class Router:
    """후보 (endpoint, model) 조합에 §6.3 순서대로 호출을 실행한다."""

    def __init__(self, config: ModelConfig, endpoints: list[Endpoint], factory: LLMFactory,
                 state: RouterState | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.endpoints = endpoints
        self.factory = factory
        self.state = state or RouterState()
        self.clock = clock
        self.sleep = sleep
        self._models: dict[tuple[str, str, int], Any] = {}

    def _model(self, endpoint: Endpoint, base_model: str, timeout: int):
        key = (endpoint.region, endpoint.model_id(base_model), timeout)
        with self.state.lock:
            if key not in self._models:
                self._models[key] = self.factory(endpoint, base_model, timeout)
            return self._models[key]

    def _ordered_endpoints(self, role: str) -> list[Endpoint]:
        last = self.state.last_success.get(role)
        if last is None:
            return list(self.endpoints)
        first = [e for e in self.endpoints if e.name == last]
        return first + [e for e in self.endpoints if e.name != last]

    def candidates(self, role: str) -> list[tuple[Endpoint, str]]:
        spec = self.config.roles[role]
        models = [spec["model"]] + ([spec["fallback_model"]] if spec.get("fallback_model") else [])
        return [(ep, m) for m in models for ep in self._ordered_endpoints(role)
                if self.config.is_available(ep, m)]

    def run(self, role: str, call: Callable[[Any], Any]) -> Any:
        if role not in self.config.roles:
            raise KeyError(f"unknown role {role!r}; known: {sorted(self.config.roles)}")
        timeout = int(self.config.roles[role]["timeout"])
        attempts: list[Attempt] = []
        for endpoint, model in self.candidates(role):
            combo = (endpoint.name, model)
            now = self.clock()
            with self.state.lock:
                if combo in self.state.unavailable:
                    attempts.append(Attempt(endpoint.name, model, "skipped_unavailable"))
                    continue
                if self.state.is_cooling(combo, now):
                    attempts.append(Attempt(endpoint.name, model, "skipped_cooldown"))
                    continue
            llm = self._model(endpoint, model, timeout)
            tries = 0
            while True:
                tries += 1
                try:
                    result = call(llm)
                except Exception as exc:  # noqa: BLE001 - 아래에서 분류한다
                    status = classify_error(exc)
                    if status is CallStatus.TRANSIENT and tries < TRANSIENT_MAX_ATTEMPTS:
                        logger.info("llm role=%s endpoint=%s model=%s transient, retrying once",
                                    role, endpoint.name, model)
                        continue
                    if status is CallStatus.THROTTLED and tries < THROTTLE_MAX_ATTEMPTS:
                        delay = min(THROTTLE_MAX_DELAY, THROTTLE_BASE_DELAY * 2 ** (tries - 1))
                        delay *= 0.5 + random.random() / 2
                        logger.info("llm role=%s endpoint=%s model=%s throttled, retry %d in %.1fs",
                                    role, endpoint.name, model, tries, delay)
                        self.sleep(delay)
                        continue
                    attempts.append(Attempt(endpoint.name, model, status.value, f"{type(exc).__name__}: {exc}"[:300]))
                    self._record_failure(combo, status)
                    if status not in _SWITCH:
                        raise LLMCallError(role, attempts, exc) from exc
                    logger.warning("llm role=%s endpoint=%s model=%s failed (%s), switching",
                                   role, endpoint.name, model, status.value)
                    break
                with self.state.lock:
                    self.state.last_success[role] = endpoint.name
                logger.info("llm role=%s endpoint=%s model=%s ok", role, endpoint.name, model)
                return result
        raise LLMCallError(role, attempts)

    def _record_failure(self, combo: Combo, status: CallStatus) -> None:
        with self.state.lock:
            if status in (CallStatus.THROTTLED, CallStatus.QUOTA_EXCEEDED):
                self.state.cooldown_until[combo] = self.clock() + self.config.cooldown_seconds
            elif status is CallStatus.UNAVAILABLE:
                self.state.unavailable.add(combo)


class RoleLLM:
    """get_llm(role)이 돌려주는 객체: invoke / with_structured_output / bind_tools, 호출마다 failover한다."""

    def __init__(self, router: Router, role: str):
        self.router = router
        self.role = role

    def invoke(self, input: Any, **kwargs: Any) -> Any:
        return self.router.run(self.role, lambda llm: llm.invoke(input, **kwargs))

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:
        role, router = self.role, self.router
        return RunnableLambda(
            lambda inp: router.run(role, lambda llm: llm.with_structured_output(schema, **kwargs).invoke(inp)),
            name=f"{role}_structured",
        )

    def bind_tools(self, tools: list, **kwargs: Any) -> Runnable:
        """에이전트 루프(coach, D44)용 tool-calling 모델. 매 턴이 router 호출 하나라서, 루프 도중 호출 제한이나
        할당량 초과에 걸린 endpoint도 다른 호출처럼 전환된다."""
        role, router = self.role, self.router
        return RunnableLambda(
            lambda inp: router.run(role, lambda llm: llm.bind_tools(tools, **kwargs).invoke(inp)),
            name=f"{role}_tools",
        )


def bedrock_factory(endpoint: Endpoint, base_model: str, timeout: int):
    from langchain_aws import ChatBedrockConverse

    # 재시도를 끈다: 켜 두면 botocore가 타임아웃, 5xx, 일일 할당량 throttle까지 재시도한다(D32).
    cfg = Config(region_name=endpoint.region, connect_timeout=10, read_timeout=timeout,
                 retries={"mode": "standard", "total_max_attempts": 1})
    return ChatBedrockConverse(model=endpoint.model_id(base_model), region_name=endpoint.region, config=cfg)


_default_router: Router | None = None
_default_lock = threading.Lock()


def default_router() -> Router:
    """config/models.yaml, .env, data/model_status.json으로 만든 프로세스 전체 공용 router."""
    global _default_router
    with _default_lock:
        if _default_router is None:
            from dotenv import load_dotenv

            load_dotenv(PROJECT_ROOT / ".env", override=False)
            config = load_model_config()
            router = Router(config, resolve_endpoints(config), bedrock_factory)
            seeded = router.state.seed_from_model_status(MODEL_STATUS_PATH, config.cooldown_seconds, router.clock())
            if seeded:
                logger.info("starting in cooldown from model_status.json: %s", seeded)
            _default_router = router
        return _default_router


def get_llm(role: str, router: Router | None = None) -> RoleLLM:
    router = router or default_router()
    if role not in router.config.roles:
        raise KeyError(f"unknown role {role!r}; known: {sorted(router.config.roles)}")
    return RoleLLM(router, role)
