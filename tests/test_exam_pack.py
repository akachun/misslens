import copy

import pytest
import yaml
from pydantic import ValidationError

from misslens.exam_pack import EXAMS_DIR, ExamPack, load_exam_pack

RAW = yaml.safe_load((EXAMS_DIR / "ccar-f" / "exam.yaml").read_text(encoding="utf-8"))


def test_ccar_f_pack_loads():
    pack = load_exam_pack("ccar-f")
    assert pack.exam_id == "ccar-f"
    assert pack.status == "final"
    # Guide v1.0 blueprint: 5 domains, 7/5/6/6/6 task statements, 6 scenarios, 16 out-of-scope topics.
    assert [len(d.tasks) for d in pack.domains] == [7, 5, 6, 6, 6]
    assert len(pack.scenarios) == 6
    assert len(pack.out_of_scope) == 16
    assert pack.needs_reask
    assert len(pack.task_ids) == len(set(pack.task_ids)) > 0
    assert abs(sum(d.weight for d in pack.domains) - 1) < 1e-9


def test_lookups_and_task_weight():
    pack = load_exam_pack("ccar-f")
    first = pack.domains[0]
    tid = first.tasks[0].id
    assert pack.domain_of(tid) == first
    assert pack.task(tid).id == tid
    assert pack.task_weight(tid) == pytest.approx(first.weight / len(first.tasks))
    assert sum(pack.task_weight(t) for t in pack.task_ids) == pytest.approx(1.0)
    with pytest.raises(KeyError):
        pack.task("no-such-task")


def test_prompt_task_list_comes_from_pack():
    pack = load_exam_pack("ccar-f")
    text = pack.prompt_task_list()
    for d in pack.domains:
        assert d.name in text
        for t in d.tasks:
            assert f"[{t.id}] {t.title}" in text
    one = pack.domains[1].tasks[0].id
    only = pack.prompt_task_list([one])
    assert f"[{one}]" in only and pack.domains[0].name not in only


def _mutated(fn):
    raw = copy.deepcopy(RAW)
    fn(raw)
    return raw


@pytest.mark.parametrize("mutate", [
    lambda r: r["domains"][0].update(weight=0.5),                                  # weights != 1
    lambda r: r["domains"][1]["tasks"].append(dict(r["domains"][0]["tasks"][0])),  # duplicate task id
    lambda r: r["domains"][1].update(id=r["domains"][0]["id"]),                     # duplicate domain id
    lambda r: r.update(task_id_map={"old": "missing"}),                             # map to unknown id
    lambda r: r.update(question_format="essay"),
    lambda r: r.update(version=0),
    lambda r: r["scenarios"][0].update(primary_domains=["d9"]),                    # unknown domain
    lambda r: r["scenarios"][1].update(id=r["scenarios"][0]["id"]),                # duplicate scenario id
])
def test_invalid_packs_rejected(mutate):
    with pytest.raises(ValidationError):
        ExamPack.model_validate(_mutated(mutate))


def test_same_language_skips_reask():
    raw = _mutated(lambda r: r.update(learner_language="en"))
    assert not ExamPack.model_validate(raw).needs_reask


def test_task_id_map():
    tid = RAW["domains"][0]["tasks"][0]["id"]
    pack = ExamPack.model_validate(_mutated(lambda r: r.update(task_id_map={"old-1": tid})))
    assert pack.current_task_id("old-1") == tid
    assert pack.current_task_id(tid) == tid


def test_directory_must_match_exam_id(tmp_path):
    d = tmp_path / "other"
    d.mkdir()
    (d / "exam.yaml").write_text(yaml.safe_dump(RAW, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError):
        load_exam_pack("other", exams_dir=tmp_path)


def test_scenarios_and_out_of_scope_helpers():
    pack = load_exam_pack("ccar-f")
    d = pack.domains[0].id
    assert all(d in s.primary_domains for s in pack.scenarios_for_domain(d))
    assert len(pack.scenarios_for_domain(d)) >= 1
    text = pack.prompt_out_of_scope()
    assert text.count("\n") == len(pack.out_of_scope) - 1
    assert all(t in text for t in pack.out_of_scope)
