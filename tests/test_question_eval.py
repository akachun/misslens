"""D59: 문제 생성 품질 라운드 평가(인-아웃 세트, LLM-as-Judge, 리포트). 모델은 가짜로 대체한다."""

from __future__ import annotations

import json
import random

import pytest

from fakes import FAIL, PASS, FakeRoleLLM, draft
from misslens.agents.generator import Generator
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Ledger
from misslens.llm import LLMCallError
from misslens.question_eval import (GenerationStats, Judgement, SetItem, generate_round, issues_of, judge_all,
                                   load_question_set, load_round_metrics, metrics, render_report, save_round,
                                   summarize_judgement)

PACK = load_exam_pack("ccar-f")
def opt(letter, kind="requirement", because=True, defensible=False):
    return {"option": letter, "violates_kind": kind, "real_reason": "r", "wrong_because_of_violates": because,
            "defensible": defensible}


GOOD = {"wrong_options": [opt("B"), opt("C"), opt("D")], "answer_correct": True, "answer_copies_stem": False,
        "shared_misconception": False, "unrealistic": False, "stem_contradiction": None, "translation_issues": [],
        "fact_issues": [], "uncertain_facts": [], "reason": "fine"}
BAD = {**GOOD, "wrong_options": [opt("B", "proposal"), opt("C", defensible=True), opt("D")],
       "uncertain_facts": ["x"], "reason": "B's violates is an engineer's proposal | not a requirement"}


def payload(answer_index=0):
    return {**draft(answer_index), "task_id": "2.3", "domain": "d2", "scenario_id": None, "grounding": "retrieved"}


def test_question_set_file_is_valid():
    items, meta = load_question_set("ccar-f")
    assert len(items) == 20 and meta["exam_id"] == "ccar-f"
    assert {i.task_id for i in items} <= set(PACK.task_ids)
    assert all(i.external["verdict"] in ("ok", "fix", "drop") for i in items)
    assert {k for i in items for k in i.external["issues"]} <= {"violates", "multiple", "fact", "translation",
                                                                 "fit", "stem"}


def test_judge_blind_then_rubric_and_calibration():
    llm = FakeRoleLLM([{"answer": "A", "reason": "r"}, GOOD,                 # 1번: 직접 푼 답 일치, OK
                       {"answer": "C", "reason": "r"}, BAD,                  # 2번: 불일치, 수정
                       LLMCallError("judge", [])])                           # 3번: 판정 실패
    items = [SetItem("2.3", 1, {"verdict": "ok", "issues": []}),
             SetItem("2.3", 2, {"verdict": "fix", "issues": ["violates", "multiple"]}),
             SetItem("2.1", 3, {"verdict": "fix", "issues": ["fact"]})]
    calls = []
    ctx = lambda pack, tid: calls.append(tid) or []  # noqa: E731
    res = judge_all(llm, PACK, [("q1", items[0], payload()), ("q2", items[1], payload()),
                                ("q3", items[2], payload())], ctx)
    assert calls == ["2.3", "2.1"]                                        # 세부 항목마다 검색 한 번
    blind_prompt = llm.prompts[0][1].content
    assert "[CORRECT]" not in blind_prompt and "violates" not in blind_prompt   # 직접 풀 때는 정답을 숨긴다
    assert "[CORRECT]" in llm.prompts[1][1].content and "보기 0" in llm.prompts[1][1].content
    assert [r.blind_agrees for r in res] == [True, False, None] and res[2].error.startswith("LLMCallError")
    bad = summarize_judgement(Judgement.model_validate(BAD))
    assert issues_of(bad) == ["violates", "multiple"] and bad["verdict"] == "fix" and bad["facts"] == "uncertain"
    assert summarize_judgement(Judgement.model_validate(GOOD))["verdict"] == "ok"
    assert summarize_judgement(Judgement.model_validate({**GOOD, "answer_correct": False}))["verdict"] == "drop"
    assert issues_of(summarize_judgement(Judgement.model_validate(
        {**GOOD, "unrealistic": True, "translation_issues": ["emitting -> 실행"],
         "stem_contradiction": "a vs b", "fact_issues": ["f"]}))) == ["fact", "translation", "fit", "stem"]
    m = metrics(res)
    assert m["judged"] == 2 and m["judge_ok_rate"] == 0.5 and m["violates_valid_rate"] == 0.5
    assert m["calibration"]["ok_vs_not_ok_agreement"] == 1.0
    assert m["calibration"]["issue_recall"] == {"violates": {"external": 1, "judge_found": 1},
                                                "multiple": {"external": 1, "judge_found": 1}}


def test_generate_round_counts_tries_and_rejections():
    ledger = Ledger(":memory:")
    gen_llm = FakeRoleLLM([draft(0), draft(0), draft(1)])
    ver_llm = FakeRoleLLM([PASS, {**FAIL, "answer_supported": True, "violations_real": False}, PASS])
    gen = Generator(PACK, ledger, gen_llm=gen_llm, verify_llm=ver_llm, rng=random.Random(0))
    set_id, st = generate_round(gen, ledger, PACK, [SetItem("2.3"), SetItem("2.1")])
    assert (st.planned, st.generated, st.first_try, st.tries) == (2, 2, 1, [1, 2])
    assert st.rejections == {"verifier:violations_real": 1}
    assert [q["status"] for q in ledger.set_questions(set_id)] == ["ready", "ready"]
    assert ledger.list_sets(PACK.exam_id) == []                          # 학습 세트 목록에 나타나지 않는다
    ledger.close()


def test_report_compares_with_previous_round(tmp_path):
    llm = FakeRoleLLM([{"answer": "A", "reason": "r"}, BAD])
    res = judge_all(llm, PACK, [("r2-q1", SetItem("2.3"), payload())], None)
    gen = GenerationStats(planned=1, generated=1, first_try=0, tries=[2], rejections={"validator": 1})
    m = metrics(res, gen)
    save_round(1, {**m, "judge_ok_rate": 0.25, "violates_valid_rate": 0.0}, res, "r1", out_dir=tmp_path)
    prev = load_round_metrics(1, out_dir=tmp_path)
    md = render_report(2, m, res, previous=prev, notes="프롬프트 보강", judge_model="judge-model")
    assert "| 판정기 OK 비율 | 0% | 25% | -25%p |" in md
    assert "생성 1/1 · 첫 시도 통과 0%" in md and "validator 1" in md
    assert "engineer's proposal / not a requirement" in md               # 표 안의 | 는 바꾼다
    path = save_round(2, m, res, md, out_dir=tmp_path)
    assert path.name == "round2_report.md"
    data = json.loads((tmp_path / "round2.json").read_text(encoding="utf-8"))
    assert data["metrics"]["generation"]["mean_tries"] == 2.0
    assert load_round_metrics(5, out_dir=tmp_path) is None


@pytest.mark.parametrize("schema", ["BlindAnswer", "Judgement", "OptionReview"])
def test_judge_schemas_are_english(schema):
    import re

    import misslens.question_eval as qe

    assert not re.search(r"[가-힣]", json.dumps(getattr(qe, schema).model_json_schema(), ensure_ascii=False))
    assert not re.search(r"[가-힣]", qe.JUDGE_SYSTEM + qe.BLIND_SYSTEM)


def test_cli_judges_existing_round_and_writes_report(tmp_path, monkeypatch):
    import misslens.question_eval as qe
    from misslens.cli import main
    from misslens.ledger import Answer

    src = tmp_path / "round1.sqlite"
    lg = Ledger(src)
    sid = lg.create_set(PACK.exam_id, 1)
    qid = lg.add_question(sid, seq=0, task_id="2.3", domain="d2", blueprint_version=PACK.version, payload=payload())
    lg.start_set(sid)
    lg.submit_set(sid, [Answer(qid, 0, 100)])
    lg.close()
    (tmp_path / "ccar-f").mkdir()
    (tmp_path / "ccar-f" / "question_set.yaml").write_text(
        f"exam_id: ccar-f\nround1_source: {src}\nitems:\n"
        f"  - {{round1_qid: {qid}, task_id: '2.3', external: {{verdict: fix, issues: [violates]}}}}\n",
        encoding="utf-8")
    monkeypatch.setattr(qe, "EVALS_DIR", tmp_path)
    monkeypatch.setattr(qe, "REPORT_DIR", tmp_path / "evaluation")
    monkeypatch.setattr("misslens.cli.open_index", lambda exam_id, **kw: None)
    monkeypatch.setattr("misslens.llm.get_llm", lambda role: FakeRoleLLM([{"answer": "A", "reason": "r"}, BAD]))
    assert main(["question-eval", "--round", "1", "--existing", "--notes", "기준선"]) == 0
    report = (tmp_path / "evaluation" / "round1_report.md").read_text(encoding="utf-8")
    assert "1라운드" in report and "기준선" in report and "판정기 보정" in report
    assert f"r1-q{qid}" in report


def test_cli_rejudges_round_set_and_keeps_generation_stats(tmp_path, monkeypatch):
    import misslens.question_eval as qe
    from misslens.cli import main

    db = tmp_path / "rounds.sqlite"
    lg = Ledger(db)
    sid = lg.create_set(PACK.exam_id, 1)
    lg.add_question(sid, seq=0, task_id="2.3", domain="d2", blueprint_version=PACK.version, payload=payload())
    lg.close()
    (tmp_path / "ccar-f").mkdir()
    (tmp_path / "ccar-f" / "question_set.yaml").write_text(
        "exam_id: ccar-f\nround1_source: none\nitems:\n  - {task_id: '2.3'}\n  - {task_id: '2.1'}\n",
        encoding="utf-8")
    out = tmp_path / "evaluation"
    monkeypatch.setattr(qe, "EVALS_DIR", tmp_path)
    monkeypatch.setattr(qe, "REPORT_DIR", out)
    monkeypatch.setattr(qe, "ROUNDS_DB", db)
    monkeypatch.setattr("misslens.cli.open_index", lambda exam_id, **kw: None)
    save_round(2, {"generation": {"planned": 2, "generated": 1, "first_try_rate": 1.0, "mean_tries": 1.0,
                                  "rejections": {}, "failures": {"2.1": "validator: x"}}}, [], "old", out_dir=out)
    monkeypatch.setattr("misslens.llm.get_llm", lambda role: FakeRoleLLM([{"answer": "A", "reason": "r"}, GOOD]))
    assert main(["question-eval", "--round", "2", "--from-set", str(sid)]) == 0
    data = json.loads((out / "round2.json").read_text(encoding="utf-8"))
    assert data["metrics"]["rounds_db_set"] == sid and data["metrics"]["judged"] == 1
    assert data["metrics"]["generation"]["failures"] == {"2.1": "validator: x"}
    assert "생성 실패 2.1: validator: x" in (out / "round2_report.md").read_text(encoding="utf-8")


def test_saved_round_records_are_readable():
    """evaluation/의 라운드 기록은 다음 라운드 비교에 쓰이므로 읽을 수 있어야 한다."""
    from misslens.question_eval import REPORT_DIR

    files = sorted(REPORT_DIR.glob("round*.json"))
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert {"round", "metrics", "results"} <= set(data), f.name
        assert (REPORT_DIR / f.name.replace(".json", "_report.md")).exists(), f.name
