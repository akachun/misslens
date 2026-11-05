"""D43: learner self-labels collected during review -> agreement stat and eval export (T10 eval)."""

from __future__ import annotations

import io

import pytest
import yaml
from rich.console import Console

from fakes import draft
from misslens.cli import CLI, eval_command, export_eval
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Answer, Ledger, LedgerError, ReaskAnswer, diagnosis_agreement

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def reviewed_set(ledger, outcomes):
    """outcomes per question: 'right' | 'reading' | 'concept' | 'uncertain'. Answer is option 0."""
    sid = ledger.create_set(PACK.exam_id, len(outcomes))
    qids = [ledger.add_question(sid, seq=i, task_id="2.1", domain="d2", blueprint_version=PACK.version,
                                payload={**draft(0), "task_id": "2.1", "domain": "d2", "scenario_id": None,
                                         "grounding": "retrieved"}) for i in range(len(outcomes))]
    ledger.start_set(sid)
    ledger.submit_set(sid, [Answer(q, 0 if o == "right" else 1, 1000) for q, o in zip(qids, outcomes)])
    att = {a["question_id"]: a["id"] for a in ledger.set_attempts(sid)}
    reasks = []
    for q, o in zip(qids, outcomes):
        if o == "right":
            continue
        reasks.append(ReaskAnswer(att[q], False, 1 if o == "concept" else 0, o == "reading"))
    ledger.record_review(sid, reasks)
    return sid, [att[q] for q in qids]


def test_feedback_only_on_reviewed_misses(ledger):
    _, (right, miss) = reviewed_set(ledger, ["right", "reading"])
    with pytest.raises(LedgerError):
        ledger.record_miss_feedback(right, "concept")
    with pytest.raises(LedgerError):
        ledger.record_miss_feedback(miss, "maybe")
    ledger.record_miss_feedback(miss, "concept")
    ledger.record_miss_feedback(miss, "reading")                    # changing your mind overwrites
    (m,) = ledger.labeled_misses(PACK.exam_id)
    assert m["learner_label"] == "reading" and m["stage"] == "before_explanation"


def test_agreement_counts_and_excludes_invalid(ledger):
    _, ids = reviewed_set(ledger, ["reading", "concept", "uncertain", "concept"])
    for a, label in zip(ids, ["reading", "reading", "concept", "unsure"]):
        ledger.record_miss_feedback(a, label)
    ag = diagnosis_agreement(ledger, PACK.exam_id)
    assert (ag.labeled, ag.compared, ag.agree) == (4, 2, 1) and ag.rate == 0.5
    assert ag.confusion == {"reading->reading": 1, "concept->reading": 1, "uncertain->concept": 1,
                            "concept->unsure": 1}
    ledger.mark_invalid(ids[1])
    ag = diagnosis_agreement(ledger, PACK.exam_id)
    assert (ag.compared, ag.agree) == (1, 1)


def test_export_matches_fixture_shape(ledger, tmp_path):
    _, ids = reviewed_set(ledger, ["reading", "right"])
    ledger.record_miss_feedback(ids[0], "concept")
    path = tmp_path / "eval" / "misses.yaml"
    assert export_eval(ledger, PACK, path) == 1
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    (item,) = doc["items"]
    fixture = yaml.safe_load(open("tests/fixtures/eval_misses.yaml", encoding="utf-8"))["items"][0]
    assert set(fixture) - {"note"} <= set(item)                    # same fields as the T1-a fixture
    assert item["first_choice"] == 1 and item["violates"][item["answer_index"]] is None
    assert all(kc in item["stem_en"] for kc in item["key_constraints"])
    assert item["self_label"]["miss_type"] == "concept" and item["system"]["miss_type"] == "reading"


def test_eval_command_prints_rate(ledger):
    _, ids = reviewed_set(ledger, ["reading"])
    ledger.record_miss_feedback(ids[0], "reading")
    out = io.StringIO()
    eval_command(CLI(PACK, ledger, console=Console(file=out, width=200)), None)
    assert "일치율 100%" in out.getvalue() and "reading->reading" in out.getvalue()
