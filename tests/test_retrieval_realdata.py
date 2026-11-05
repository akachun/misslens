"""실제 색인 vs 저장한 기준선(D50). data/index를 읽는다. 청킹, 정리, 태깅, 순위를 바꾼 뒤
`pytest -m realdata`로 실행한다. 새 점수는 `python -m misslens.cli retrieval-eval --save-baseline`으로 받아들인다."""

import pytest

from misslens.agents.collector import load_sources
from misslens.exam_pack import load_exam_pack
from misslens.retrieval.evaluate import evaluate, load_baseline, load_golden
from misslens.retrieval.index import INDEX_ROOT

pytestmark = pytest.mark.realdata


def test_real_index_health_and_no_regression():
    if not (INDEX_ROOT / "ccar-f" / "meta.json").exists():
        pytest.skip("no local index")
    from misslens.retrieval.embed import get_embedder
    from misslens.retrieval.index import ChunkIndex

    pack = load_exam_pack("ccar-f")
    baseline = load_baseline("ccar-f")
    assert baseline is not None, "save a baseline first: retrieval-eval --save-baseline"
    rep = evaluate(ChunkIndex("ccar-f", get_embedder()), pack, load_golden("ccar-f"),
                   list(load_sources("ccar-f").sites.values()), baseline, max_drop=0.05)
    print(rep.as_dict()["golden"]["modes"])
    assert rep.health["ok"], rep.health["checks"]
    assert rep.regressions == []
    assert rep.golden["modes"]["plain"]["hit@5"] >= 0.8                 # 기준선 외의 절대 하한
