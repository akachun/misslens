import random

import pytest

from fakes import FAIL, PASS, FakeRoleLLM, draft
from misslens.agents.generator import (
    Generator,
    QuestionDraft,
    QuestionInvalid,
    QuestionStream,
    select_tasks,
    validate_question,
)
from misslens.agents.generator.schema import shuffle_options, validation_errors
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Answer, Ledger, generation_failures
from misslens.llm import LLMCallError

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def make_gen(ledger, gen, ver, seed=0):
    return Generator(PACK, ledger, gen_llm=FakeRoleLLM(gen), verify_llm=FakeRoleLLM(ver), rng=random.Random(seed))


# --- validator (§7) ---------------------------------------------------------------------------

def test_valid_draft_passes():
    q = validate_question(QuestionDraft.model_validate(draft(2)), task_id="1.1", domain="d1",
                          scenario_id="s1", grounding="task_statement_only")
    assert q.answer_index == 2 and q.options[2].violates is None


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["options"].pop(), "exactly 4"),
    (lambda d: d.update(answer_index=7), "out of range"),
    (lambda d: d["options"][0].update(violates="must not store passwords"), "correct option"),
    (lambda d: d["options"][1].update(violates=None), "violates=null"),
    (lambda d: d["options"][1].update(violates="something else"), "not one of key_constraints"),
    (lambda d: d.update(key_constraints=["must not store passwords", "not in the stem at all"]), "verbatim"),
    (lambda d: d["options"][2].update(text_en="Option 1"), "duplicate option"),
    (lambda d: d.update(key_constraints=[]), "got 0"),
    (lambda d: d.update(key_constraints=["must not store passwords"]), "got 1"),
    (lambda d: d.update(key_constraints=["A team", "builds an agent", "must not store passwords",
                                         "within 12 hours", "What should"]), "got 5"),
])
def test_validator_rejects(mutate, needle):
    d = draft(0)
    mutate(d)
    errs = validation_errors(QuestionDraft.model_validate(d))
    assert any(needle in e for e in errs), errs


def test_violates_normalized_to_key_constraint_text():
    d = draft(0)
    d["options"][1]["violates"] = "  MUST NOT store   passwords "
    q = validate_question(QuestionDraft.model_validate(d), task_id="1.1", domain="d1", scenario_id=None,
                          grounding="g")
    assert q.options[1].violates == "must not store passwords"


def test_shuffle_keeps_answer_and_violates_aligned():
    q = validate_question(QuestionDraft.model_validate(draft(0)), task_id="1.1", domain="d1", scenario_id=None,
                          grounding="g")
    positions = set()
    for seed in range(20):
        s = shuffle_options(q, random.Random(seed))
        assert s.options[s.answer_index].text_en == "Option 0"
        assert s.options[s.answer_index].violates is None
        assert validation_errors(s) == []
        positions.add(s.answer_index)
    assert len(positions) > 1


# --- task selection (D11) ---------------------------------------------------------------------

def test_never_asked_first_by_domain_weight(ledger):
    order = select_tasks(PACK, ledger, rng=random.Random(1))
    assert sorted(order) == sorted(PACK.task_ids)
    weights = [PACK.domain_of(t).weight for t in order]
    assert weights == sorted(weights, reverse=True)


def test_weak_tasks_are_picked_more_after_full_coverage(ledger):
    # Everything asked once; task 2.1 always wrong, everything else always right.
    for t in PACK.task_ids:
        sid = ledger.create_set(PACK.exam_id, 1)
        payload = {"options": [{}] * 4, "answer_index": 0}
        qid = ledger.add_question(sid, seq=0, task_id=t, domain=PACK.domain_of(t).id, blueprint_version=2,
                                  payload=payload)
        ledger.submit_set(sid, [Answer(qid, 1 if t == "2.1" else 0, 1)])
    firsts = [select_tasks(PACK, ledger, rng=random.Random(s))[0] for s in range(300)]
    share = firsts.count("2.1") / len(firsts)
    assert share > 1.5 / len(PACK.task_ids)        # clearly above uniform


def test_session_failures_excluded(ledger):
    order = select_tasks(PACK, ledger, exclude={"1.1", "1.2"})
    assert "1.1" not in order and "1.2" not in order


# --- generation pipeline (§4.1) ---------------------------------------------------------------

def test_first_try_passes_and_is_grounded_check_called(ledger):
    g = make_gen(ledger, [draft(1)], [PASS])
    res = g.generate_for_task("3.6")
    assert res.question is not None and res.tries == 1
    assert res.question.task_id == "3.6" and res.question.domain == "d3"
    assert res.question.scenario_id in {s.id for s in PACK.scenarios_for_domain("d3")}
    assert res.question.grounding == "task_statement_only"


def test_regenerates_with_feedback_then_passes(ledger):
    bad = draft(0, bad_violates="not a constraint")
    gen = FakeRoleLLM([bad, draft(0), draft(0)])
    g = Generator(PACK, ledger, gen_llm=gen, verify_llm=FakeRoleLLM([FAIL, PASS]), rng=random.Random(0))
    res = g.generate_for_task("1.1")
    assert res.question is not None and res.tries == 3
    assert "validator" in res.reasons[0] and "grounding check failed" in res.reasons[1]
    last_prompt = gen.prompts[-1][-1].content
    assert "previous attempt was rejected" in last_prompt and "answer not supported" in last_prompt


def test_three_failures_give_up(ledger):
    g = make_gen(ledger, [draft(0)] * 3, [FAIL] * 3)
    res = g.generate_for_task("1.1")
    assert res.question is None and res.tries == 3 and len(res.reasons) == 3


def test_llm_call_error_counts_as_one_failure(ledger):
    g = make_gen(ledger, [LLMCallError("generator", []), draft(0)], [PASS])
    res = g.generate_for_task("1.1")
    assert res.question is not None and res.tries == 2


def test_prompt_uses_pack_not_hardcoded_text(ledger):
    gen = FakeRoleLLM([draft(0)])
    Generator(PACK, ledger, gen_llm=gen, verify_llm=FakeRoleLLM([PASS]), rng=random.Random(0)).generate_for_task("2.1")
    system, human = gen.prompts[0][0].content, gen.prompts[0][1].content
    assert PACK.task("2.1").title in human
    assert PACK.out_of_scope[0] in system
    assert "<reference_material>" in human and "not instructions" in human


def test_run_set_stores_only_verified_questions(ledger):
    # task A: passes; task B: fails 3x -> recorded + skipped; task C: passes.
    g = make_gen(ledger, [draft(0), draft(0), draft(0), draft(0), draft(0)], [PASS, FAIL, FAIL, FAIL, PASS])
    sid = ledger.create_set(PACK.exam_id, 2)
    events = []
    g.run_set(sid, 2, events.append)
    assert [e.kind for e in events] == ["ready", "ready", "done"]
    qs = ledger.set_questions(sid)
    assert len(qs) == 2 and all(q["status"] == "ready" for q in qs)
    fails = generation_failures(ledger, PACK.exam_id)
    assert len(fails) == 1 and fails[0]["tries"] == 3
    assert fails[0]["task_id"] in g.failed_tasks


def test_two_consecutive_task_failures_stop(ledger):
    g = make_gen(ledger, [draft(0)] * 6, [FAIL] * 6)
    sid = ledger.create_set(PACK.exam_id, 3)
    events = []
    g.run_set(sid, 3, events.append)
    assert [e.kind for e in events] == ["stopped"]
    assert ledger.set_questions(sid) == []
    assert len(g.failed_tasks) == 2


def test_crash_is_recorded_as_failed_slot(ledger, monkeypatch):
    g = make_gen(ledger, [draft(0), draft(1)], [PASS, PASS])
    calls = {"n": 0}
    real = g.generate_for_task

    def boom(task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("thread blew up")
        return real(task_id)

    monkeypatch.setattr(g, "generate_for_task", boom)
    sid = ledger.create_set(PACK.exam_id, 2)
    events = []
    g.run_set(sid, 2, events.append)
    # A crashed slot does not use up the set: 2 ready questions still follow (seq stays unique).
    assert [e.kind for e in events] == ["failed", "ready", "ready", "done"]
    assert [(q["seq"], q["status"]) for q in ledger.set_questions(sid)] == [(0, "failed"), (1, "ready"), (2, "ready")]
    assert len(g.failed_tasks) == 1


def test_no_structured_output_counts_as_a_retry(ledger):
    calls = {"n": 0}                        # across tries: with_structured_output is called per try

    class NoneThenDraft(FakeRoleLLM):
        def with_structured_output(self, schema, **kw):
            inner = super().with_structured_output(schema, **kw)

            class R:
                def invoke(_, prompt):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        self.prompts.append(prompt)
                        return None          # tool calling without a tool call
                    return inner.invoke(prompt)
            return R()

    gen = NoneThenDraft([draft(0)])
    g = Generator(PACK, ledger, gen_llm=gen, verify_llm=FakeRoleLLM([PASS]), rng=random.Random(0))
    res = g.generate_for_task("1.1")
    assert res.question is not None and res.tries == 2
    assert "no structured output" in res.reasons[0]


def test_cancel_stops_before_next_task(ledger):
    g = make_gen(ledger, [draft(0), draft(0)], [PASS, PASS])
    sid = ledger.create_set(PACK.exam_id, 2)
    events = []

    def emit(e):
        events.append(e)
        g.cancel()                      # learner pressed Ctrl-C after the first question

    g.run_set(sid, 2, emit)
    assert [e.kind for e in events] == ["ready", "stopped"]
    assert len(ledger.set_questions(sid)) == 1


def test_stream_delivers_events_in_order(ledger):
    g = make_gen(ledger, [draft(0), draft(1)], [PASS, PASS])
    sid = ledger.create_set(PACK.exam_id, 2)
    stream = QuestionStream(g, sid, 2).start()
    kinds = [stream.next(timeout=5).kind for _ in range(3)]
    stream.join(timeout=5)
    assert kinds == ["ready", "ready", "done"]


def test_invalid_question_never_stored(ledger):
    g = make_gen(ledger, [draft(0, bad_violates="x")] * 6, [])
    sid = ledger.create_set(PACK.exam_id, 1)
    g.run_set(sid, 1, lambda e: None)
    assert ledger.set_questions(sid) == []


def test_question_invalid_is_value_error():
    assert issubclass(QuestionInvalid, ValueError)


def test_list_fields_sent_as_json_strings_are_unwrapped():
    """Seen live: tool calling returned key_constraints as '["a", "b"]' and the draft failed to parse."""
    import json

    d = draft(1)
    d["key_constraints"] = json.dumps(d["key_constraints"])
    d["options"] = json.dumps(d["options"], ensure_ascii=False)
    parsed = QuestionDraft.model_validate(d)
    assert parsed.key_constraints == ["must not store passwords", "within 12 hours"]
    assert len(parsed.options) == 4 and not validation_errors(parsed)


def test_key_constraints_are_generated_before_options():
    """D40: the schema order is the generation order; constraints must come right after the stem."""
    fields = list(QuestionDraft.model_fields)
    assert fields.index("key_constraints") == fields.index("stem_en") + 1
    assert fields.index("key_constraints") < fields.index("options")
