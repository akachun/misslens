"""지금 응답하는 엔드포인트 x 모델 조합을 확인한다(ARCHITECTURE §6.2, TASK T1-b).

모델이 있는지는 config/models.yaml의 `availability`로 이미 안다. 이 스크립트는 목록에 있는 조합마다 지금
응답하는지(할당량 초과, 호출 제한, 접근 권한)를 확인하고 data/model_status.json에 쓴다.
`availability`에 없는 조합은 호출하지 않는다.

사용법:
    python scripts/probe_models.py                    # 모든 엔드포인트 x 모델 + 구조화 출력 확인
    python scripts/probe_models.py --skip-structured
    BEDROCK_ENDPOINTS=usw2-us python scripts/probe_models.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from dotenv import load_dotenv
from pydantic import BaseModel, Field

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

PROBE_PROMPT = "Reply with the single word: pong"
PROBE_TIMEOUT = 60


def _client(region: str, timeout: int):
    # 재시도하지 않는다: 첫 응답을 그대로 보고한다.
    cfg = Config(region_name=region, connect_timeout=10, read_timeout=timeout,
                 retries={"total_max_attempts": 1, "mode": "standard"})
    return boto3.client("bedrock-runtime", config=cfg)


def probe_one(endpoint: Endpoint, base_model: str) -> dict:
    model_id = endpoint.model_id(base_model)
    started = time.monotonic()
    result = {"endpoint": endpoint.name, "region": endpoint.region, "prefix": endpoint.prefix,
              "model": base_model, "model_id": model_id}
    try:
        resp = _client(endpoint.region, PROBE_TIMEOUT).converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": PROBE_PROMPT}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
        result.update(status=CallStatus.OK.value, reply=text[:40])
    except Exception as exc:  # noqa: BLE001 - 아래에서 분류한다
        status = classify_error(exc)
        result.update(status=status.value, error=f"{type(exc).__name__}: {exc}"[:300])
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


class _ProbeAnswer(BaseModel):
    """실제 Question과 같은 모양의 작은 스키마(보기 목록 + 인덱스)."""
    options: list[str] = Field(description="Exactly 3 short color names")
    answer_index: int = Field(description="Index of the option that is the color of a clear daytime sky")
    reason_ko: str = Field(description="One short sentence in Korean")


def probe_structured(config: ModelConfig, endpoints: list[Endpoint], results: list[dict]) -> dict:
    """처음으로 응답한 엔드포인트에서 generator 모델로 구조화 출력 호출을 한 번 한다."""
    from langchain_aws import ChatBedrockConverse

    role = config.roles["generator"]
    base_model = role["model"]
    ok_names = {r["endpoint"] for r in results if r["model"] == base_model and r["status"] == "ok"}
    candidates = [e for e in endpoints if e.name in ok_names]
    if not candidates:
        return {"status": "skipped", "model": base_model, "reason": "no endpoint answered for this model"}

    attempts = []
    for ep in candidates:
        started = time.monotonic()
        try:
            llm = ChatBedrockConverse(model=ep.model_id(base_model), region_name=ep.region,
                                      temperature=0, max_tokens=300,
                                      config=Config(read_timeout=role["timeout"],
                                                    retries={"total_max_attempts": 1, "mode": "standard"}))
            out = llm.with_structured_output(_ProbeAnswer).invoke(
                "Give three color names and mark which one is the color of a clear daytime sky.")
            valid = isinstance(out, _ProbeAnswer) and len(out.options) == 3 and 0 <= out.answer_index < 3
            return {"status": "ok" if valid else "invalid", "endpoint": ep.name, "model": base_model,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "output": out.model_dump() if isinstance(out, BaseModel) else repr(out)[:300],
                    "failed_attempts": attempts}
        except Exception as exc:  # noqa: BLE001
            attempts.append({"endpoint": ep.name, "status": classify_error(exc).value,
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {"status": "failed", "model": base_model, "failed_attempts": attempts}


def role_coverage(config: ModelConfig, results: list[dict]) -> dict:
    ok = {(r["endpoint"], r["model"]) for r in results if r["status"] == "ok"}
    coverage = {}
    for role, spec in config.roles.items():
        models = [spec["model"]] + ([spec["fallback_model"]] if spec.get("fallback_model") else [])
        coverage[role] = [f"{ep}:{m}" for m in models for (ep, mm) in sorted(ok) if mm == m]
    return coverage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-structured", action="store_true", help="skip the Sonnet structured output call")
    parser.add_argument("--output", default=str(MODEL_STATUS_PATH))
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    config = load_model_config()
    endpoints = resolve_endpoints(config)
    models = config.role_models()

    results: list[dict] = []
    for ep in endpoints:
        for model in models:
            if not config.is_available(ep, model):
                continue
            r = probe_one(ep, model)
            results.append(r)
            print(f"{r['endpoint']:<13} {r['model']:<45} {r['status']:<15} {r['latency_ms']:>6}ms"
                  + (f"  {r.get('error', '')[:110]}" if r["status"] != "ok" else ""))
            if r["status"] == CallStatus.AUTH.value:
                print("\nAWS 자격 증명 오류입니다. .env 또는 AWS 프로필을 확인하세요.", file=sys.stderr)
                return 2

    structured = {"status": "skipped", "reason": "--skip-structured"} if args.skip_structured \
        else probe_structured(config, endpoints, results)
    coverage = role_coverage(config, results)

    status = {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoints_order": [e.name for e in endpoints],
        "results": results,
        # llm.py는 이 조합들을 cooldown 상태로 시작한다(§6.2).
        "quota_exceeded": [f"{r['endpoint']}:{r['model']}" for r in results if r["status"] == "quota_exceeded"],
        "structured_output": structured,
        "role_coverage": coverage,
    }
    from pathlib import Path
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n구조화 출력({structured.get('model', '')}): {structured['status']}"
          + (f" @ {structured['endpoint']}" if structured.get("endpoint") else ""))
    missing = [role for role, combos in coverage.items() if not combos]
    print("역할별 응답 조합: " + ", ".join(f"{k}={len(v)}" for k, v in coverage.items()))
    if status["quota_exceeded"]:
        print("할당량 초과: " + ", ".join(status["quota_exceeded"]))
    print(f"저장: {out}")
    if missing:
        print(f"응답하는 조합이 없는 역할: {missing}", file=sys.stderr)
        return 1
    return 0 if structured["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
