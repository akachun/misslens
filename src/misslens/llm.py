"""get_llm(role): Bedrock calls with endpoint failover (ARCHITECTURE §6, D26, D27).

Every LLM call in the app goes through get_llm(role). Model IDs, regions and timeouts come only
from config/models.yaml.

    llm = get_llm("generator")
    question = llm.with_structured_output(Question).invoke(prompt)
    text = llm.invoke(prompt).content

Switching rules (§6.3), per (endpoint, model) combination. botocore retries are OFF
(total_max_attempts=1) so the router alone decides how often each error class is retried (D32):
  - throttled                         -> up to THROTTLE_MAX_ATTEMPTS tries with exponential backoff,
                                         then cooldown, next endpoint
  - quota exceeded                    -> no retry, cooldown, next endpoint
  - model unavailable / access denied -> unavailable for the process, next endpoint
  - timeout / connection / 5xx        -> retry once, then next endpoint
  - invalid request / other error     -> fail now (switching would not help)
  - credentials                       -> fail now
All endpoints failed for the role's model -> repeat with fallback_model -> LLMCallError.
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

# Combinations seen as quota-exceeded by a probe younger than this start in cooldown.
MODEL_STATUS_MAX_AGE_SECONDS = 24 * 3600
THROTTLE_MAX_ATTEMPTS = 5      # total tries on one endpoint while throttled (D13, D32)
THROTTLE_BASE_DELAY = 1.0      # seconds; doubles each retry, with jitter
THROTTLE_MAX_DELAY = 20.0
TRANSIENT_MAX_ATTEMPTS = 2     # first try + one retry

_SWITCH = {CallStatus.THROTTLED, CallStatus.QUOTA_EXCEEDED, CallStatus.UNAVAILABLE, CallStatus.TRANSIENT}

Combo = tuple[str, str]  # (endpoint name, base model)
LLMFactory = Callable[[Endpoint, str, int], Any]  # (endpoint, base model, timeout) -> chat model


@dataclass
class Attempt:
    endpoint: str
    model: str
    status: str
    error: str = ""


class LLMCallError(RuntimeError):
    """Every allowed endpoint x model combination failed, or the error was not switchable."""

    def __init__(self, role: str, attempts: list[Attempt], cause: BaseException | None = None):
        self.role = role
        self.attempts = attempts
        self.cause = cause
        summary = "; ".join(f"{a.endpoint}:{a.model}={a.status}" for a in attempts) or "no candidate"
        last = next((a.error for a in reversed(attempts) if a.error), "")
        super().__init__(f"LLM call failed for role {role!r}: {summary}" + (f" ({last})" if last else ""))


@dataclass
class RouterState:
    """Process-wide memory of cooldowns, unavailable combinations and last success per role."""

    cooldown_until: dict[Combo, float] = field(default_factory=dict)
    unavailable: set[Combo] = field(default_factory=set)
    last_success: dict[str, str] = field(default_factory=dict)  # role -> endpoint name
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_cooling(self, combo: Combo, now: float) -> bool:
        return self.cooldown_until.get(combo, 0.0) > now

    def seed_from_model_status(self, path: Path, cooldown_seconds: int, now: float,
                               wall_now: datetime | None = None) -> list[Combo]:
        """Start quota-exceeded combinations from a recent probe in cooldown (§6.2)."""
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
    """Runs a call against candidate (endpoint, model) combinations in §6.3 order."""

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
                except Exception as exc:  # noqa: BLE001 - classified below
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
    """What get_llm(role) returns: invoke / with_structured_output / bind_tools, each call with failover."""

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
        """Tool-calling model for agent loops (coach, D44). Every turn is one router call, so a throttled or
        over-quota endpoint mid-loop switches like any other call."""
        role, router = self.role, self.router
        return RunnableLambda(
            lambda inp: router.run(role, lambda llm: llm.bind_tools(tools, **kwargs).invoke(inp)),
            name=f"{role}_tools",
        )


def bedrock_factory(endpoint: Endpoint, base_model: str, timeout: int):
    from langchain_aws import ChatBedrockConverse

    # Retries off: botocore would also retry timeouts, 5xx and daily-quota throttles (D32).
    cfg = Config(region_name=endpoint.region, connect_timeout=10, read_timeout=timeout,
                 retries={"mode": "standard", "total_max_attempts": 1})
    return ChatBedrockConverse(model=endpoint.model_id(base_model), region_name=endpoint.region, config=cfg)


_default_router: Router | None = None
_default_lock = threading.Lock()


def default_router() -> Router:
    """Process-wide router built from config/models.yaml, .env and data/model_status.json."""
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
