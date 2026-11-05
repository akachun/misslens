"""tests/fixtures/eval_misses.yaml의 일관성 검사(TASK T1-a).

손으로 쓴 fixture를 Question validator 규칙(ARCHITECTURE §7)과 맞춰 두어,
나중에 diagnose() 테스트와 수동 eval(T10)에 쓸 수 있게 한다.
"""

from pathlib import Path

import pytest
import yaml

FIXTURE = Path(__file__).parent / "fixtures" / "eval_misses.yaml"
ITEMS = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))["items"]


def test_ids_unique():
    ids = [i["id"] for i in ITEMS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("item", ITEMS, ids=lambda i: i["id"])
def test_item_shape(item):
    assert len(item["options_en"]) == 4
    assert len(item["violates"]) == 4
    assert 0 <= item["answer_index"] < 4
    assert 0 <= item["first_choice"] < 4


@pytest.mark.parametrize("item", ITEMS, ids=lambda i: i["id"])
def test_violates_matches_validator_rule(item):
    # 정답 보기만 violates=None이다. 나머지 violates는 모두 key_constraints 중 하나다.
    none_at = [k for k, v in enumerate(item["violates"]) if v is None]
    assert none_at == [item["answer_index"]]
    for v in item["violates"]:
        assert v is None or v in item["key_constraints"]


@pytest.mark.parametrize("item", ITEMS, ids=lambda i: i["id"])
def test_key_constraints_quote_the_stem(item):
    for kc in item["key_constraints"]:
        assert kc in item["stem_en"], kc


@pytest.mark.parametrize("item", ITEMS, ids=lambda i: i["id"])
def test_labels_consistent_with_decoy_flag(item):
    label = item["self_label"]
    if item["is_decoy"]:
        assert item["first_choice"] == item["answer_index"]
        assert label["miss_type"] is None
    else:
        assert item["first_choice"] != item["answer_index"]
        assert label["miss_type"] in {"concept", "reading", "uncertain"}
        if label["miss_type"] == "concept":
            assert label["missed_phrase"] is None


def test_task_ids_exist_in_exam_pack():
    from misslens.exam_pack import load_exam_pack

    pack = load_exam_pack("ccar-f")
    for item in ITEMS:
        if item["task_id"] is not None:
            assert pack.domain_of(item["task_id"]).id == item["domain"], item["id"]
