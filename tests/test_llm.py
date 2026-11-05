import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from misslens.bedrock import load_model_config
from misslens.llm import THROTTLE_MAX_ATTEMPTS, LLMCallError, Router, RouterState, bedrock_factory, get_llm

CFG = load_model_config()
SONNET = CFG.roles["generator"]["model"]
SONNET_FALLBACK = CFG.roles["generator"]["fallback_model"]
HAIKU = CFG.roles["verifier"]["model"]
EP = [e.name for e in CFG.endpoints]


def throttle(msg="Rate exceeded"):
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": msg}}, "Converse")


def quota():
    return ClientError({"Error": {"Code": "ServiceQuotaExceededException", "Message": "quota"}}, "Converse")


def bad_model():
    return ClientError({"Error": {"Code": "ValidationException",
                                  "Message": "The provided model identifier is invalid."}}, "Converse")


def too_long():
    return ClientError({"Error": {"Code": "ValidationException",
                                  "Message": "Input is too long for requested model."}}, "Converse")


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Script:
    """Factory whose models fail per (endpoint, model) according to a plan, then answer 'ok'."""

    def __init__(self, plan=None):
        self.plan = plan or {}  # (endpoint, model) -> list of exceptions raised before succeeding
        self.calls: list[tuple[str, str]] = []
        self.built: list[tuple[str, str]] = []

    def factory(self, endpoint, model, timeout):
        self.built.append((endpoint.name, model))
        script = self

        class _M:
            def invoke(self, inp, **kw):
                key = (endpoint.name, model)
                script.calls.append(key)
                errs = script.plan.get(key, [])
                if errs:
                    raise errs.pop(0)
                return AIMessage(content=f"ok:{endpoint.name}:{model}")

        return _M()


def throttled_out():
    """Enough throttles to exhaust the router's retries on one endpoint."""
    return [throttle() for _ in range(THROTTLE_MAX_ATTEMPTS)]


def make_router(plan=None, clock=None, endpoints=None):
    s = Script(plan)
    s.sleeps = []
    r = Router(CFG, endpoints or list(CFG.endpoints), s.factory, RouterState(), clock or FakeClock(),
               sleep=s.sleeps.append)
    return r, s


def test_first_endpoint_success():
    r, s = make_router()
    out = get_llm("generator", r).invoke("hi")
    assert out.content == f"ok:{EP[0]}:{SONNET}"
    assert s.calls == [(EP[0], SONNET)]


def test_throttle_and_quota_switch_in_documented_order():
    r, s = make_router({(EP[0], SONNET): throttled_out(), (EP[1], SONNET): [quota()]})
    out = get_llm("generator", r).invoke("hi")
    assert out.content == f"ok:{EP[2]}:{SONNET}"
    assert s.calls == [(EP[0], SONNET)] * THROTTLE_MAX_ATTEMPTS + [(EP[1], SONNET), (EP[2], SONNET)]
    assert r.state.is_cooling((EP[0], SONNET), r.clock())
    assert r.state.is_cooling((EP[1], SONNET), r.clock())


def test_daily_token_quota_reported_as_throttling_cools_down():
    r, s = make_router({(EP[0], SONNET): [throttle("Too many tokens per day, please wait")]})
    get_llm("generator", r).invoke("hi")
    assert r.state.is_cooling((EP[0], SONNET), r.clock())
    assert s.calls == [(EP[0], SONNET), (EP[1], SONNET)]   # quota: no retry on the same endpoint
    assert s.sleeps == []


def test_throttle_retries_same_endpoint_with_backoff():
    r, s = make_router({(EP[0], SONNET): [throttle(), throttle()]})
    out = get_llm("generator", r).invoke("hi")
    assert out.content == f"ok:{EP[0]}:{SONNET}"
    assert s.calls == [(EP[0], SONNET)] * 3
    assert len(s.sleeps) == 2 and s.sleeps[1] > s.sleeps[0] * 0.9 / 2  # grows (with jitter)
    assert not r.state.is_cooling((EP[0], SONNET), r.clock())


def test_botocore_retries_are_disabled():
    llm = bedrock_factory(CFG.endpoints[0], SONNET, 60)
    assert llm.client.meta.config.retries["total_max_attempts"] == 1  # one try, no botocore retry


def test_cooldown_is_skipped_then_expires():
    clock = FakeClock()
    r, s = make_router({(EP[0], SONNET): throttled_out()}, clock=clock)
    llm = get_llm("generator", r)
    llm.invoke("1")
    r.state.last_success.clear()          # force start from endpoint 1 again
    s.calls.clear()
    llm.invoke("2")
    assert s.calls == [(EP[1], SONNET)]   # endpoint 1 still cooling
    clock.t += CFG.cooldown_seconds + 1
    r.state.last_success.clear()
    s.calls.clear()
    llm.invoke("3")
    assert s.calls == [(EP[0], SONNET)]


def test_cooldown_is_per_model_not_per_endpoint():
    r, s = make_router({(EP[0], SONNET): [quota()]})
    get_llm("generator", r).invoke("hi")
    s.calls.clear()
    get_llm("verifier", r).invoke("hi")
    assert s.calls == [(EP[0], HAIKU)]


def test_last_success_is_tried_first():
    r, s = make_router({(EP[0], SONNET): throttled_out()})
    llm = get_llm("generator", r)
    llm.invoke("1")
    clock_after = r.clock.t + CFG.cooldown_seconds + 1
    r.clock.t = clock_after                # endpoint 1 no longer cooling
    s.calls.clear()
    llm.invoke("2")
    assert s.calls == [(EP[1], SONNET)]


def test_unavailable_model_is_skipped_for_process():
    r, s = make_router({(EP[0], SONNET): [bad_model()]})
    llm = get_llm("generator", r)
    llm.invoke("1")
    assert (EP[0], SONNET) in r.state.unavailable
    r.state.last_success.clear()
    r.clock.t += 10 * CFG.cooldown_seconds
    s.calls.clear()
    llm.invoke("2")
    assert s.calls == [(EP[1], SONNET)]


def test_transient_retries_once_on_same_endpoint():
    r, s = make_router({(EP[0], SONNET): [ReadTimeoutError(endpoint_url="x")]})
    out = get_llm("generator", r).invoke("hi")
    assert out.content.startswith(f"ok:{EP[0]}")
    assert s.calls == [(EP[0], SONNET), (EP[0], SONNET)]


def test_transient_twice_switches():
    r, s = make_router({(EP[0], SONNET): [ReadTimeoutError(endpoint_url="x"), ReadTimeoutError(endpoint_url="x")]})
    out = get_llm("generator", r).invoke("hi")
    assert out.content.startswith(f"ok:{EP[1]}")
    assert not r.state.is_cooling((EP[0], SONNET), r.clock())


def test_invalid_request_fails_without_switching():
    r, s = make_router({(EP[0], SONNET): [too_long()]})
    with pytest.raises(LLMCallError) as ei:
        get_llm("generator", r).invoke("hi")
    assert s.calls == [(EP[0], SONNET)]
    assert ei.value.attempts[-1].status == "invalid_request"
    assert (EP[0], SONNET) not in r.state.unavailable


def test_credentials_error_fails_without_switching():
    r, s = make_router({(EP[0], SONNET): [NoCredentialsError()]})
    with pytest.raises(LLMCallError):
        get_llm("generator", r).invoke("hi")
    assert s.calls == [(EP[0], SONNET)]


def test_fallback_model_after_all_endpoints():
    plan = {(e, SONNET): [quota()] for e in EP}
    r, s = make_router(plan)
    out = get_llm("generator", r).invoke("hi")
    assert out.content == f"ok:{EP[0]}:{SONNET_FALLBACK}"
    assert s.calls == [(e, SONNET) for e in EP] + [(EP[0], SONNET_FALLBACK)]


def test_role_without_fallback_fails_after_all_endpoints():
    plan = {(e, HAIKU): [quota()] for e in EP}
    r, s = make_router(plan)
    with pytest.raises(LLMCallError) as ei:
        get_llm("verifier", r).invoke("hi")
    assert [a.endpoint for a in ei.value.attempts] == EP
    assert all(a.status == "quota_exceeded" for a in ei.value.attempts)


def test_all_fail_including_fallback():
    plan = {(e, m): [quota()] for e in EP for m in (SONNET, SONNET_FALLBACK)}
    r, _ = make_router(plan)
    with pytest.raises(LLMCallError) as ei:
        get_llm("generator", r).invoke("hi")
    assert len(ei.value.attempts) == 2 * len(EP)


def test_endpoint_override_limits_order():
    r, s = make_router({(EP[1], SONNET): [quota()]},
                       endpoints=[e for e in CFG.endpoints if e.name in (EP[1], EP[3])])
    out = get_llm("generator", r).invoke("hi")
    assert out.content == f"ok:{EP[3]}:{SONNET}"


def test_model_instances_are_cached():
    r, s = make_router()
    llm = get_llm("generator", r)
    llm.invoke("1")
    llm.invoke("2")
    assert s.built == [(EP[0], SONNET)]


def test_unknown_role():
    r, _ = make_router()
    with pytest.raises(KeyError):
        get_llm("nope", r)


def test_seed_from_model_status(tmp_path):
    p = tmp_path / "model_status.json"
    now = datetime.now(timezone.utc)
    p.write_text(json.dumps({"probed_at": now.isoformat(), "quota_exceeded": [f"{EP[0]}:{SONNET}"]}))
    st = RouterState()
    assert st.seed_from_model_status(p, 900, now=100.0) == [(EP[0], SONNET)]
    assert st.is_cooling((EP[0], SONNET), 100.0 + 899)
    assert not st.is_cooling((EP[0], SONNET), 100.0 + 901)

    old = RouterState()
    stale_now = now + timedelta(days=2)
    assert old.seed_from_model_status(p, 900, now=100.0, wall_now=stale_now) == []

    naive = tmp_path / "naive.json"   # no timezone in probed_at -> treated as UTC, no crash
    naive.write_text(json.dumps({"probed_at": now.replace(tzinfo=None).isoformat(),
                                 "quota_exceeded": [f"{EP[0]}:{SONNET}"]}))
    assert RouterState().seed_from_model_status(naive, 900, now=0.0) == [(EP[0], SONNET)]


class Pick(BaseModel):
    answer_index: int


def test_structured_output_with_fake_chat_model():
    """with_structured_output goes through the router; GenericFakeChatModel stands in for Bedrock."""

    def factory(endpoint, model, timeout):
        fake = GenericFakeChatModel(messages=iter([AIMessage(content='{"answer_index": 2}')]))

        class _Structured:
            def with_structured_output(self, schema, **kw):
                return fake | (lambda m: schema.model_validate_json(m.content))

        return _Structured()

    r = Router(CFG, list(CFG.endpoints), factory, RouterState(), FakeClock(), sleep=lambda _: None)
    out = get_llm("generator", r).with_structured_output(Pick).invoke("q")
    assert out == Pick(answer_index=2)


def test_bind_tools_switches_endpoint_like_other_calls():
    """D44: each agent turn of a tool-calling model goes through the router (quota mid-loop -> next endpoint)."""
    r, s = make_router({(EP[0], SONNET): [quota()]})

    def factory(endpoint, model, timeout):
        m = Script.factory(s, endpoint, model, timeout)
        m.bind_tools = lambda tools, **kw: m          # the bound model invokes like the plain one
        return m

    r.factory = factory
    out = get_llm("explainer", r).bind_tools([]).invoke("hi")
    assert out.content == f"ok:{EP[1]}:{SONNET}"
