from pathlib import Path

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError

from misslens.bedrock import CallStatus, classify_error, load_model_config, resolve_endpoints


def _client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def test_config_loads_and_keeps_documented_order():
    cfg = load_model_config()
    assert [e.name for e in cfg.endpoints] == [
        "use1-us", "usw2-us", "use1-global", "usw2-global", "apne2-global"]
    assert "apne2-apac" not in cfg.availability
    for role in cfg.roles.values():
        for ep in cfg.endpoints:
            assert cfg.is_available(ep, role["model"])


def test_model_id_adds_prefix():
    cfg = load_model_config()
    ep = cfg.endpoints[0]
    assert ep.model_id("x.model-v1:0") == f"{ep.prefix}.x.model-v1:0"


def test_role_models_lists_primary_and_fallbacks_once():
    models = load_model_config().role_models()
    assert len(models) == len(set(models)) == 4


def test_resolve_endpoints_override_order_and_dedup():
    cfg = load_model_config()
    eps = resolve_endpoints(cfg, "usw2-us, use1-global,usw2-us")
    assert [e.name for e in eps] == ["usw2-us", "use1-global"]


def test_resolve_endpoints_default_and_env(monkeypatch):
    cfg = load_model_config()
    monkeypatch.delenv("BEDROCK_ENDPOINTS", raising=False)
    assert resolve_endpoints(cfg) == cfg.endpoints
    monkeypatch.setenv("BEDROCK_ENDPOINTS", "apne2-global")
    assert [e.name for e in resolve_endpoints(cfg)] == ["apne2-global"]


def test_resolve_endpoints_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_endpoints(load_model_config(), "apne2-apac")


def test_unknown_endpoint_in_availability_rejected(tmp_path: Path):
    p = tmp_path / "m.yaml"
    p.write_text("endpoints: [{name: a, region: r, prefix: us}]\navailability: {b: [m]}\nroles: {}\n")
    with pytest.raises(ValueError):
        load_model_config(p)


@pytest.mark.parametrize("exc, expected", [
    (_client_error("ThrottlingException", "Rate exceeded"), CallStatus.THROTTLED),
    (_client_error("ThrottlingException", "Too many tokens per day, please wait"), CallStatus.QUOTA_EXCEEDED),
    (_client_error("ServiceQuotaExceededException"), CallStatus.QUOTA_EXCEEDED),
    (_client_error("ValidationException", "The provided model identifier is invalid."), CallStatus.UNAVAILABLE),
    (_client_error("ValidationException",
                   "Invocation of model ID x with on-demand throughput isn't supported."), CallStatus.UNAVAILABLE),
    (_client_error("ValidationException", "Input is too long for requested model."), CallStatus.INVALID_REQUEST),
    (_client_error("AccessDeniedException"), CallStatus.UNAVAILABLE),
    (_client_error("ResourceNotFoundException"), CallStatus.UNAVAILABLE),
    (_client_error("UnrecognizedClientException"), CallStatus.AUTH),
    (_client_error("ServiceUnavailableException"), CallStatus.TRANSIENT),
    (NoCredentialsError(), CallStatus.AUTH),
    (ReadTimeoutError(endpoint_url="https://x"), CallStatus.TRANSIENT),
    (RuntimeError("boom"), CallStatus.ERROR),
])
def test_classify_error(exc, expected):
    assert classify_error(exc) is expected
