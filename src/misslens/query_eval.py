"""질의 API의 라운드 평가: 인-아웃 세트(`test_queries.csv`) 통과율 + RAGAS 지표(D61).

한 라운드는 다음 순서로 진행한다.
1. `test_queries.csv`의 질의마다 `answer_query`(= `POST /query`)를 실행한다.
2. 통과 판정: (a) `expected_tools`가 모두 trace에 있다(route:<intent> 포함), (b) 판정기(judge 역할)가
   `expected_traits`를 모두 충족하고 `forbidden`이 하나도 없다고 본다. 둘 다 맞으면 통과.
3. RAGAS(0.4): 근거를 찾아 답한 질의(contexts가 있는 것)에 faithfulness, answer_relevancy(ResponseRelevancy),
   context_precision(LLMContextPrecisionWithoutReference: 정답 문장이 없으므로 답변을 기준으로 근거의 쓸모를 본다).
   RAGAS의 평가 모델도 `get_llm("judge")`를 거친다(규칙 5). 임베딩은 색인과 같은 bge-m3.
4. `evaluation/query_round<N>_report.md`와 `query_round<N>.json`에 저장하고, 앞 라운드가 있으면 비교한다.

이 지표는 질의응답 품질이지 학습자 통계가 아니다(학습자 통계는 ledger에서만, 규칙 1).
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
import types
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from misslens.bedrock import PROJECT_ROOT
from misslens.qa import QueryResult
from misslens.trace import span

logger = logging.getLogger(__name__)

QUERIES_CSV = PROJECT_ROOT / "test_queries.csv"
REPORT_DIR = PROJECT_ROOT / "evaluation"
CATEGORIES = ("positive", "negative", "edge", "guardrail")
RAGAS_METRICS = (("context_precision", "context_precision"), ("faithfulness", "faithfulness"),
                 ("answer_relevancy", "answer_relevancy"))


# --- 인-아웃 세트 ---------------------------------------------------------------------------

def _split(value: str | None) -> list[str]:
    return [s.strip() for s in (value or "").split(";") if s.strip()]


@dataclass
class QueryCase:
    id: int
    category: str
    input: str
    expected_traits: list[str]
    forbidden: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    note: str = ""


def load_queries(path: Path | None = None) -> list[QueryCase]:
    with open(path or QUERIES_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    cases = []
    for r in rows:
        if r["category"] not in CATEGORIES:
            raise ValueError(f"id {r['id']}: unknown category {r['category']!r}")
        cases.append(QueryCase(int(r["id"]), r["category"], r["input"], _split(r["expected_traits"]),
                               _split(r.get("forbidden")), _split(r.get("expected_tools")), r.get("note") or ""))
    return cases


# --- 판정 ---------------------------------------------------------------------------------

class TraitCheck(BaseModel):
    item: str = Field(description="The trait or forbidden item, copied as given")
    ok: bool = Field(description="For a trait: the answer satisfies it. For a forbidden item: the answer is free of it")
    reason: str = Field(description="One short sentence")


class CaseJudgement(BaseModel):
    """Judgement of one assistant answer against its expected traits and forbidden items."""
    traits: list[TraitCheck] = Field(description="One entry per expected trait, in order")
    forbidden: list[TraitCheck] = Field(description="One entry per forbidden item, in order")


JUDGE_SYSTEM = """You evaluate a study-coach assistant's answer to one learner query (the learner writes Korean).
For each expected trait, decide whether the answer satisfies it. For each forbidden item, decide whether the answer
is free of it (ok=true means it does NOT appear). Judge the answer text as written; be strict but fair: a trait about
citing a source is met by a "(출처: ...)" mention, a trait about language is about the main answer language.
The answer may be a fixed refusal or a feature notice; judge it the same way."""


def judge_case(llm: Any, case: QueryCase, res: QueryResult) -> CaseJudgement:
    body = (f"Learner query:\n{case.input}\n\nAssistant answer:\n{res.answer}\n\n"
            f"Retrieved sources: {', '.join(sorted({c['doc_id'] for c in res.contexts})) or 'none'}\n\n"
            "Expected traits:\n" + "\n".join(f"- {t}" for t in case.expected_traits)
            + "\n\nForbidden items:\n" + ("\n".join(f"- {t}" for t in case.forbidden) or "(none)"))
    out = llm.with_structured_output(CaseJudgement).invoke([SystemMessage(JUDGE_SYSTEM), HumanMessage(body)])
    return CaseJudgement.model_validate(out) if isinstance(out, dict) else out


def called_tools(res: QueryResult) -> set[str]:
    return set(res.steps) | {res.route}


@dataclass
class CaseResult:
    id: int
    category: str
    input: str
    answer: str
    route: str
    steps: list[str]
    sources: list[str]
    tools_ok: bool
    missing_tools: list[str]
    traits_ok: bool
    forbidden_ok: bool
    checks: dict
    passed: bool
    contexts: list[dict] = field(default_factory=list)
    ragas: dict = field(default_factory=dict)


def evaluate_case(case: QueryCase, res: QueryResult, judgement: CaseJudgement | None) -> CaseResult:
    missing = [t for t in case.expected_tools if t not in called_tools(res)]
    # 판정기가 항목을 빠뜨리면 검사하지 않은 조건이 통과로 셈해진다. 개수가 맞아야 판정으로 인정한다.
    complete = bool(judgement) and (len(judgement.traits) == len(case.expected_traits)
                                    and len(judgement.forbidden) == len(case.forbidden))
    traits_ok = complete and all(c.ok for c in judgement.traits)
    forbidden_ok = complete and all(c.ok for c in judgement.forbidden)
    return CaseResult(case.id, case.category, case.input, res.answer, res.route, res.steps,
                      sorted({c["doc_id"] for c in res.contexts}), not missing, missing, traits_ok, forbidden_ok,
                      judgement.model_dump() if judgement else {}, not missing and traits_ok and forbidden_ok,
                      res.contexts)


# --- RAGAS ---------------------------------------------------------------------------------

def _shim_langchain_community() -> None:
    """ragas 0.4.3은 langchain-community 0.4에서 사라진 VertexAI 모듈을 isinstance 검사용으로만 import한다.
    그 이름만 빈 클래스로 채워 import가 되게 한다(우리는 VertexAI를 쓰지 않는다)."""
    name = "langchain_community.chat_models.vertexai"
    try:
        __import__(name)
    except ModuleNotFoundError:
        mod = types.ModuleType(name)
        mod.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[name] = mod


def ragas_scores(rows: list[CaseResult], llm: Any, embedder: Any, *, max_workers: int = 2) -> None:
    """contexts가 있는 행마다 RAGAS 세 지표를 계산해 row.ragas에 넣는다."""
    targets = [r for r in rows if r.contexts and r.route == "answer"]
    if not targets:
        return
    _shim_langchain_community()
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from langchain_core.outputs import Generation, LLMResult
    from ragas import EvaluationDataset, RunConfig, SingleTurnSample, evaluate
    from ragas.embeddings.base import BaseRagasEmbeddings
    from ragas.llms.base import BaseRagasLLM
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithoutReference, ResponseRelevancy

    class RoleRagasLLM(BaseRagasLLM):
        """RAGAS 프롬프트를 get_llm(role) 라우터로 보낸다(엔드포인트 전환·트레이스 유지)."""

        def __init__(self, role_llm: Any):
            super().__init__()
            self.role_llm = role_llm

        def generate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
            texts = [_text(self.role_llm.invoke([HumanMessage(prompt.to_string())])) for _ in range(max(1, n))]
            return LLMResult(generations=[[Generation(text=t) for t in texts]])

        async def agenerate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
            return await asyncio.to_thread(self.generate_text, prompt, n, temperature, stop, callbacks)

        def is_finished(self, response) -> bool:
            return True

    class EmbedderRagas(BaseRagasEmbeddings):
        def __init__(self, emb: Any):
            super().__init__()
            self.emb = emb

        def embed_documents(self, texts):
            return [list(map(float, v)) for v in self.emb.encode(list(texts))]

        def embed_query(self, text):
            return self.embed_documents([text])[0]

        async def aembed_documents(self, texts):
            return await asyncio.to_thread(self.embed_documents, texts)

        async def aembed_query(self, text):
            return await asyncio.to_thread(self.embed_query, text)

    samples = [SingleTurnSample(user_input=r.input, response=r.answer, retrieved_contexts=[c["text"] for c in r.contexts])
               for r in targets]
    metrics = [LLMContextPrecisionWithoutReference(), Faithfulness(), ResponseRelevancy(strictness=1)]
    result = evaluate(EvaluationDataset(samples=samples), metrics=metrics, llm=RoleRagasLLM(llm),
                      embeddings=EmbedderRagas(embedder), run_config=RunConfig(max_workers=max_workers, timeout=300),
                      show_progress=False, raise_exceptions=False)
    names = {"llm_context_precision_without_reference": "context_precision", "faithfulness": "faithfulness",
             "answer_relevancy": "answer_relevancy"}
    for row, score in zip(targets, result.scores):
        row.ragas = {names.get(k, k): (None if v is None or v != v else round(float(v), 4)) for k, v in score.items()}


def _text(msg: Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    return str(content or "")


# --- 라운드 ---------------------------------------------------------------------------------

def run_round(cases: list[QueryCase], ask: Callable[[str], QueryResult], judge_llm: Any, *,
              progress: Callable[[str], None] = lambda m: None) -> list[CaseResult]:
    rows = []
    for case in cases:
        with span("query_eval.case", case=case.id):
            res = ask(case.input)
            try:
                judgement = judge_case(judge_llm, case, res)
            except Exception as exc:  # noqa: BLE001 - 판정 실패는 미통과로 기록한다
                logger.warning("judge failed on case %s: %s", case.id, exc)
                judgement = None
        row = evaluate_case(case, res, judgement)
        rows.append(row)
        progress(f"{case.id:>2} {case.category:<9} {'통과' if row.passed else '미통과'} · {row.route} · "
                 f"{', '.join(row.steps)}")
    return rows


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def metrics(rows: list[CaseResult]) -> dict:
    by_cat = {c: {"passed": sum(r.passed for r in rows if r.category == c),
                  "total": sum(r.category == c for r in rows)} for c in CATEGORIES}
    ragas_rows = [r for r in rows if r.ragas]
    return {
        "passed": sum(r.passed for r in rows), "total": len(rows), "by_category": by_cat,
        "tools_ok": sum(r.tools_ok for r in rows), "traits_ok": sum(r.traits_ok for r in rows),
        "forbidden_ok": sum(r.forbidden_ok for r in rows),
        "ragas": {key: _mean([r.ragas.get(key) for r in ragas_rows]) for key, _ in RAGAS_METRICS},
        "ragas_samples": len(ragas_rows),
    }


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def render_report(round_no: int, m: dict, rows: list[CaseResult], *, previous: dict | None, notes: str,
                  judge_model: str, answer_model: str) -> str:
    prev = previous or {}
    lines = [f"# 질의 API 평가 — {round_no}라운드", "",
             f"- 날짜: {datetime.now():%Y-%m-%d %H:%M}",
             f"- 인-아웃 세트: `test_queries.csv` ({m['total']}건)",
             f"- 답변 모델: `{answer_model}` · 판정기·RAGAS 평가 모델: `{judge_model}` · 임베딩: bge-m3",
             "", "## 이번 라운드", "", notes, "", "## 통과율", "",
             "| 항목 | 이번 라운드 | 앞 라운드 |", "|---|---|---|"]
    p_total = f"{prev['passed']}/{prev['total']}" if prev else "-"
    lines.append(f"| **전체 통과** | **{m['passed']}/{m['total']}** | {p_total} |")
    for c in CATEGORIES:
        cur, old = m["by_category"][c], (prev.get("by_category") or {}).get(c)
        old_txt = f"{old['passed']}/{old['total']}" if old else "-"   # 3.11 호환: f-string 안에 같은 따옴표를 쓰지 않는다
        lines.append(f"| {c} | {cur['passed']}/{cur['total']} | {old_txt} |")
    lines += [f"| 기대 도구 호출 | {m['tools_ok']}/{m['total']} | {prev.get('tools_ok', '-')} |",
              f"| 기대 특성 충족 | {m['traits_ok']}/{m['total']} | {prev.get('traits_ok', '-')} |",
              f"| 금지 항목 없음 | {m['forbidden_ok']}/{m['total']} | {prev.get('forbidden_ok', '-')} |",
              "", f"## RAGAS 지표 (근거를 찾아 답한 {m['ragas_samples']}건 평균)", "",
              "| 지표 | 이번 라운드 | 앞 라운드 |", "|---|---|---|"]
    for key, label in RAGAS_METRICS:
        lines.append(f"| {label} | {_fmt(m['ragas'][key])} | {_fmt((prev.get('ragas') or {}).get(key))} |")
    lines += ["", "- context_precision: `LLMContextPrecisionWithoutReference`(정답 문장 대신 답변을 기준으로 검색 "
              "발췌가 쓸모 있었는지). faithfulness: 답변 주장이 발췌로 뒷받침되는 비율. answer_relevancy: "
              "`ResponseRelevancy`(답변에서 거꾸로 만든 질문과 원 질문의 임베딩 유사도, strictness 1).",
              "", "## 질의별 결과", "",
              "| id | 분류 | 통과 | 경로 | 단계 | 미충족 | RAGAS (cp/f/ar) |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        unmet = [c["item"] for c in r.checks.get("traits", []) if not c["ok"]]
        unmet += [f"금지: {c['item']}" for c in r.checks.get("forbidden", []) if not c["ok"]]
        unmet += [f"도구 없음: {t}" for t in r.missing_tools]
        if not r.checks:
            unmet.append("판정 실패")
        rg = "/".join(_fmt(r.ragas.get(k)) for k, _ in RAGAS_METRICS) if r.ragas else "-"
        lines.append(f"| {r.id} | {r.category} | {'✅' if r.passed else '❌'} | {r.route} | "
                     f"{' → '.join(r.steps)} | {'; '.join(unmet).replace('|', '/') or '-'} | {rg} |")
    lines += ["", "## 답변 원문", ""]
    for r in rows:
        lines += [f"**{r.id}. {r.input}**", "", "> " + r.answer.replace("\n", "\n> "), ""]
    return "\n".join(lines) + "\n"


def save_round(round_no: int, m: dict, rows: list[CaseResult], report: str, *, notes: str,
               out_dir: Path | None = None) -> Path:
    out = out_dir or REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    # 검색 발췌 본문(로그인 교안 등 제3자 자료)은 공개 저장소에 남기지 않는다(규칙 7). 출처(sources)만 둔다.
    results = [{k: v for k, v in asdict(r).items() if k != "contexts"} for r in rows]
    (out / f"query_round{round_no}.json").write_text(json.dumps(
        {"round": round_no, "notes": notes, "metrics": m, "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    path = out / f"query_round{round_no}_report.md"
    path.write_text(report, encoding="utf-8")
    return path


def load_round_metrics(round_no: int, out_dir: Path | None = None) -> dict | None:
    path = (out_dir or REPORT_DIR) / f"query_round{round_no}.json"
    return json.loads(path.read_text(encoding="utf-8"))["metrics"] if path.exists() else None


__all__ = ["CaseJudgement", "CaseResult", "QueryCase", "evaluate_case", "judge_case", "load_queries", "load_round_metrics",
           "metrics", "ragas_scores", "render_report", "run_round", "save_round"]
