"""HTTP 질의 API(과제 표준 규약, D61): `POST /query` {"question"} → {"answer", "contexts", "trace"}.

실행: `uvicorn misslens.api:app --port 8000` (샌드박스 기록으로 보려면 `MISSLENS_SANDBOX=1`).
질문에 답하기만 하고 학습 기록은 쓰지 않는다. 문제 풀이·계획 승인·수집은 화면과 CLI에서 한다(qa.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from misslens.qa import answer_query


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000, description="사용자 질의")


class Context(BaseModel):
    doc_id: str
    text: str


class TraceStep(BaseModel):
    step: str
    input: str
    output: str


class QueryOut(BaseModel):
    answer: str
    contexts: list[Context]
    trace: list[TraceStep]
    route: str = Field(description="answer | blocked:<종류> | route:<intent>")


@dataclass
class QAService:
    pack: Any
    ledger: Any
    index: Any
    llm: Any
    router_llm: Any

    def ask(self, question: str):
        return answer_query(question, pack=self.pack, ledger=self.ledger, index=self.index, llm=self.llm,
                            router_llm=self.router_llm)


@lru_cache(maxsize=1)
def default_service() -> QAService:
    from misslens.cli import open_index
    from misslens.exam_pack import load_exam_pack
    from misslens.ledger import Ledger
    from misslens.llm import get_llm

    pack = load_exam_pack(os.environ.get("MISSLENS_EXAM", "ccar-f"))
    return QAService(pack, Ledger(), open_index(pack.exam_id), get_llm("explainer"), get_llm("router"))


def create_app(service_factory=default_service) -> FastAPI:
    app = FastAPI(title="MissLens", description="자격증 시험 학습 코치 질의 API")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/query", response_model=QueryOut)
    def query(body: QueryIn) -> QueryOut:
        # 동기 함수라 FastAPI가 스레드 풀에서 실행한다(ledger는 lock으로 직렬화된 연결 하나를 공유).
        return QueryOut(**service_factory().ask(body.question).as_dict())

    return app


app = create_app()

__all__ = ["QAService", "QueryIn", "QueryOut", "app", "create_app"]
