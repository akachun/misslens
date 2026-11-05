import threading
from pathlib import Path

import pytest
import yaml

from misslens.ledger import (
    Answer,
    Ledger,
    LedgerError,
    ReaskAnswer,
    SetStateError,
    avg_time,
    coverage,
    generation_failures,
    judge,
    miss_breakdown,
    missed_phrase,
    set_score,
    task_accuracy,
    top_missed_phrases,
)

EXAM = "ccar-f"
FIXTURE = yaml.safe_load((Path(__file__).parent / "fixtures" / "eval_misses.yaml").read_text(encoding="utf-8"))


def payload(answer_index=0, violates=(None, "kc-b", "kc-c", "kc-d")):
    return {"options": [{"text_en": f"opt{i}", "text_native": f"보기{i}", "violates": v} for i, v in enumerate(violates)],
            "answer_index": answer_index, "key_constraints": [v for v in violates if v], "stem_en": "s"}


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def make_set(lg, tasks, *, exam=EXAM):
    """tasks: (task_id, payload) 목록. (set_id, [question id 목록])을 돌려준다."""
    sid = lg.create_set(exam, len(tasks))
    qids = [lg.add_question(sid, seq=i, task_id=t, domain=t.split(".")[0], blueprint_version=1, payload=p)
            for i, (t, p) in enumerate(tasks)]
    lg.start_set(sid)
    return sid, qids


# --- 판정 표(§4.2) -----------------------------------------------------------------

@pytest.mark.parametrize("first_correct, is_decoy, reask_correct, confident, expected", [
    (False, False, True, True, "reading"),
    (False, False, True, False, "uncertain"),
    (False, False, False, True, "concept"),
    (False, False, False, False, "concept"),
    (False, False, None, None, "concept"),   # 같은 언어 시험: 재질문 없음
    (True, True, True, True, None),          # 미끼: 판정 없음
    (True, True, False, False, None),
])
def test_judge_table(first_correct, is_decoy, reask_correct, confident, expected):
    assert judge(first_correct=first_correct, is_decoy=is_decoy,
                 reask_correct=reask_correct, reask_confident=confident) == expected


@pytest.mark.parametrize("item", [i for i in FIXTURE["items"] if not i["is_decoy"]], ids=lambda i: i["id"])
def test_missed_phrase_is_violates_of_first_choice_on_real_misses(item):
    p = {"options": [{"violates": v} for v in item["violates"]]}
    phrase = missed_phrase("reading", p, item["first_choice"])
    assert phrase == item["violates"][item["first_choice"]]
    assert missed_phrase("concept", p, item["first_choice"]) is None
    assert missed_phrase("uncertain", p, item["first_choice"]) is None


# --- 제출 트랜잭션 -----------------------------------------------------------------

def test_submit_grades_and_blocks_double_submit(ledger):
    sid, (q1, q2, q3) = make_set(ledger, [("1.1", payload(0)), ("1.2", payload(2)), ("2.1", payload(1))])
    ids = ledger.submit_set(sid, [Answer(q1, 0, 1000), Answer(q2, 1, 2000), Answer(q3, None, 3000)])
    rows = {r["id"]: r for r in ledger.set_attempts(sid)}
    assert [rows[i]["is_correct"] for i in ids] == [1, 0, 0]
    assert ledger.get_set(sid)["status"] == "submitted"
    with pytest.raises(SetStateError):
        ledger.submit_set(sid, [Answer(q1, 0, 1)])
    assert len(ledger.set_attempts(sid)) == 3


def test_submit_is_atomic_on_bad_answer(ledger):
    sid, (q1, q2) = make_set(ledger, [("1.1", payload(0)), ("1.2", payload(0))])
    with pytest.raises(LedgerError):
        ledger.submit_set(sid, [Answer(q1, 0, 1), Answer(q2, 9, 1)])
    assert ledger.set_attempts(sid) == []
    assert ledger.get_set(sid)["status"] == "in_progress"
    ledger.submit_set(sid, [Answer(q1, 0, 1), Answer(q2, 0, 1)])  # 아직 제출할 수 있다


def test_submit_requires_every_ready_question(ledger):
    sid, (q1, q2) = make_set(ledger, [("1.1", payload(0)), ("1.2", payload(0))])
    with pytest.raises(LedgerError):
        ledger.submit_set(sid, [Answer(q1, 0, 1)])           # q2 누락 -> 버리지 않고 거부
    assert ledger.get_set(sid)["status"] == "in_progress"
    ledger.submit_set(sid, [Answer(q1, 0, 1), Answer(q2, None, 1)])
    assert task_accuracy(ledger, EXAM)["1.2"].correct == 0


def test_failed_question_cannot_be_answered(ledger):
    sid = ledger.create_set(EXAM, 2)
    ok = ledger.add_question(sid, seq=0, task_id="1.1", domain="d1", blueprint_version=1, payload=payload())
    bad = ledger.add_question(sid, seq=1, task_id="1.2", domain="d1", blueprint_version=1, payload=None, status="failed")
    with pytest.raises(LedgerError):
        ledger.submit_set(sid, [Answer(ok, 0, 1), Answer(bad, 0, 1)])
    ledger.submit_set(sid, [Answer(ok, 0, 1)])                # 실패한 슬롯은 답이 필요 없다
    sid = ledger.create_set(EXAM, 1)
    with pytest.raises(ValueError):
        ledger.add_question(sid, seq=2, task_id="1.3", domain="d1", blueprint_version=1, payload=None)


def test_concurrent_double_submit_only_one_wins(ledger):
    sid, (q1,) = make_set(ledger, [("1.1", payload(0))])
    results = []

    def go():
        try:
            ledger.submit_set(sid, [Answer(q1, 0, 1)])
            results.append("ok")
        except SetStateError:
            results.append("blocked")

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["blocked"] * 4 + ["ok"]


# --- 리뷰 -----------------------------------------------------------------------------------

def submitted_set(ledger):
    """문제 4개: 실수 2개(B 선택, C 선택), 정답 2개."""
    sid, qs = make_set(ledger, [("1.1", payload(0)), ("1.2", payload(0)), ("2.1", payload(0)), ("2.2", payload(0))])
    a = ledger.submit_set(sid, [Answer(qs[0], 1, 10), Answer(qs[1], 2, 20), Answer(qs[2], 0, 30), Answer(qs[3], 0, 40)])
    return sid, a


def test_review_applies_table_and_records_phrase(ledger):
    sid, a = submitted_set(ledger)
    ledger.record_review(sid, [
        ReaskAnswer(a[0], False, 0, True),    # 실수 -> 정답 + 확신  = reading, 문구 = violates[B]
        ReaskAnswer(a[1], False, 0, False),   # 실수 -> 정답 + 추측 = uncertain
        ReaskAnswer(a[2], True, 0, True),     # 미끼 정답
        ReaskAnswer(a[3], True, 3, True),     # 미끼 오답
    ])
    rows = {r["id"]: r for r in ledger.set_attempts(sid)}
    assert rows[a[0]]["miss_type"] == "reading" and rows[a[0]]["missed_phrase"] == "kc-b"
    assert rows[a[1]]["miss_type"] == "uncertain" and rows[a[1]]["missed_phrase"] is None
    assert rows[a[2]]["miss_type"] is None and rows[a[2]]["is_decoy"] == 1
    assert ledger.get_set(sid)["status"] == "reviewed"
    mb = miss_breakdown(ledger, EXAM, set_id=sid)
    assert (mb.reading, mb.uncertain, mb.concept) == (1, 1, 0)
    assert mb.reading_ratio == 1.0                 # uncertain은 양쪽 모두에서 제외
    assert mb.baseline == 0.5


@pytest.mark.parametrize("bad", [
    lambda a: [ReaskAnswer(a[0], False, 0, True)],                                    # 실수 하나 누락
    lambda a: [ReaskAnswer(a[0], False, 0, True), ReaskAnswer(a[1], True, 0, True)],  # 실수를 미끼로
    lambda a: [ReaskAnswer(a[0], False, 0, True), ReaskAnswer(a[1], False, 0, True),
               ReaskAnswer(a[2], False, 0, True)],                                     # 정답을 미끼가 아닌 것으로
    lambda a: [ReaskAnswer(a[0], False, None, None), ReaskAnswer(a[1], False, 0, True)],  # 재질문 누락
    lambda a: [ReaskAnswer(a[0], False, 7, True), ReaskAnswer(a[1], False, 0, True)],     # 재질문 범위 밖
])
def test_review_rejects_inconsistent_input(ledger, bad):
    sid, a = submitted_set(ledger)
    with pytest.raises(LedgerError):
        ledger.record_review(sid, bad(a))
    assert ledger.get_set(sid)["status"] == "submitted"   # 롤백됨
    assert all(r["miss_type"] is None for r in ledger.set_attempts(sid))


def test_review_only_once_and_only_after_submit(ledger):
    sid, qs = make_set(ledger, [("1.1", payload(0))])
    with pytest.raises(SetStateError):
        ledger.record_review(sid, [])
    a = ledger.submit_set(sid, [Answer(qs[0], 0, 1)])
    ledger.record_review(sid, [ReaskAnswer(a[0], True, 0, True)])
    with pytest.raises(SetStateError):
        ledger.record_review(sid, [ReaskAnswer(a[0], True, 0, True)])


def test_same_language_review_records_concept(ledger):
    sid, a = submitted_set(ledger)
    ledger.record_review(sid, [ReaskAnswer(a[0], False, None, None), ReaskAnswer(a[1], False, None, None)],
                         reask_skipped=True)
    assert miss_breakdown(ledger, EXAM).concept == 2


# --- 통계 --------------------------------------------------------------------------------

def test_task_accuracy_laplace_last_10_and_invalid_excluded(ledger):
    # 1.1에 시도 13번: 처음 2번 정답, 이후 11번 오답 -> 창은 마지막 10번을 유지한다(모두 오답).
    attempt_ids = []
    for i in range(13):
        sid, (q,) = make_set(ledger, [("1.1", payload(0))])
        attempt_ids += ledger.submit_set(sid, [Answer(q, 0 if i < 2 else 1, 100)])
    acc = task_accuracy(ledger, EXAM)["1.1"]
    assert (acc.correct, acc.attempts) == (0, 10)
    assert acc.accuracy == pytest.approx(1 / 12)
    for aid in attempt_ids[-2:]:
        ledger.mark_invalid(aid)
    acc = task_accuracy(ledger, EXAM)["1.1"]
    assert (acc.correct, acc.attempts) == (1, 10)   # 유효 11개가 남는다. 창이 정답 하나 위로 되돌아간다


def test_uncertain_counts_as_wrong_in_accuracy(ledger):
    sid, a = submitted_set(ledger)
    ledger.record_review(sid, [ReaskAnswer(a[0], False, 0, False), ReaskAnswer(a[1], False, 1, True)])
    rows = {r["id"]: r for r in ledger.set_attempts(sid)}
    assert rows[a[0]]["miss_type"] == "uncertain"
    acc = task_accuracy(ledger, EXAM)["1.1"]
    assert (acc.correct, acc.attempts) == (0, 1)            # uncertain은 오답으로 남는다
    mb = miss_breakdown(ledger, EXAM)
    assert (mb.uncertain, mb.concept, mb.reading) == (1, 1, 0)
    assert mb.reading_ratio == 0.0                          # 0 / (0 + 1): uncertain은 분모에 없다


def test_task_id_map_merges_old_ids(ledger):
    sid, (q1, q2) = make_set(ledger, [("old-1", payload(0)), ("1.1", payload(0))])
    ledger.submit_set(sid, [Answer(q1, 0, 1), Answer(q2, 1, 1)])
    acc = task_accuracy(ledger, EXAM, task_id_map={"old-1": "1.1"})
    assert set(acc) == {"1.1"} and acc["1.1"].attempts == 2
    cov = coverage(ledger, EXAM, ["1.1", "1.2"], task_id_map={"old-1": "1.1"})
    assert cov.asked == ["1.1"]


def test_coverage_ignores_invalid_and_other_exams(ledger):
    sid, (q1, q2) = make_set(ledger, [("1.1", payload(0)), ("1.2", payload(0))])
    a = ledger.submit_set(sid, [Answer(q1, 0, 1), Answer(q2, 0, 1)])
    ledger.mark_invalid(a[1])
    other, (q3,) = make_set(ledger, [("1.3", payload(0))], exam="other-exam")
    ledger.submit_set(other, [Answer(q3, 0, 1)])
    cov = coverage(ledger, EXAM, ["1.1", "1.2", "1.3"])
    assert cov.asked == ["1.1"] and cov.never_asked == ["1.2", "1.3"]
    assert cov.ratio == pytest.approx(1 / 3)


def test_phrase_ranking_excludes_invalid_and_uses_corrections(ledger):
    for _ in range(2):
        sid, a = submitted_set(ledger)
        ledger.record_review(sid, [ReaskAnswer(a[0], False, 0, True), ReaskAnswer(a[1], False, 0, True)])
    assert top_missed_phrases(ledger, EXAM) == [("kc-b", 2), ("kc-c", 2)]
    ledger.mark_invalid(a[0])
    assert top_missed_phrases(ledger, EXAM) == [("kc-c", 2), ("kc-b", 1)]
    ledger.correct_missed_phrase(a[1], "kc-b")
    assert top_missed_phrases(ledger, EXAM) == [("kc-b", 2), ("kc-c", 1)]


def test_phrase_correction_only_for_reading_misses(ledger):
    sid, a = submitted_set(ledger)
    ledger.record_review(sid, [ReaskAnswer(a[0], False, 0, False), ReaskAnswer(a[1], False, 1, True),
                               ReaskAnswer(a[2], True, 0, True)])
    for aid in (a[0], a[1], a[2], a[3]):                    # uncertain, concept, 미끼, 재질문 안 함
        with pytest.raises(LedgerError):
            ledger.correct_missed_phrase(aid, "anything")
    assert top_missed_phrases(ledger, EXAM) == []


def test_miss_breakdown_excludes_invalid(ledger):
    sid, a = submitted_set(ledger)
    ledger.record_review(sid, [ReaskAnswer(a[0], False, 1, True), ReaskAnswer(a[1], False, 0, True),
                               ReaskAnswer(a[2], True, 0, True)])
    ledger.mark_invalid(a[1])
    mb = miss_breakdown(ledger, EXAM)
    assert (mb.concept, mb.reading, mb.decoys) == (1, 0, 1)
    assert mb.reading_ratio == 0.0


def test_avg_time_and_failures(ledger):
    sid, a = submitted_set(ledger)
    ledger.mark_invalid(a[3])
    t = avg_time(ledger, EXAM)
    assert t.overall_ms == pytest.approx(20)
    assert t.by_domain_ms == {"1": 15, "2": 30}
    ledger.record_generation_failure(EXAM, "1.1", 3, "grounding: answer not supported (use1-us/sonnet-4-6)")
    assert generation_failures(ledger, EXAM)[0]["task_id"] == "1.1"


def test_material_dedup_by_hash(ledger):
    assert ledger.add_material(EXAM, source_type="upload", content_hash="h1", source_uri="a.pdf") is not None
    assert ledger.add_material(EXAM, source_type="collect", content_hash="h1", source_uri="https://x") is None


def test_file_db_persists(tmp_path):
    path = tmp_path / "ledger.sqlite"
    lg = Ledger(path)
    sid, (q,) = make_set(lg, [("1.1", payload(0))])
    lg.submit_set(sid, [Answer(q, 0, 1)])
    lg.close()
    lg2 = Ledger(path)
    assert task_accuracy(lg2, EXAM)["1.1"].attempts == 1
    lg2.close()


def test_set_score_counts_valid_first_answers(ledger):
    sid, a = submitted_set(ledger)          # 실수 2개, 정답 2개
    assert (set_score(ledger, sid).correct, set_score(ledger, sid).total) == (2, 4)
    ledger.mark_invalid(a[0])
    assert (set_score(ledger, sid).correct, set_score(ledger, sid).total) == (2, 3)
