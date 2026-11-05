"""Real index vs saved baseline (D50). Reads data/index; run with `pytest -m realdata` after changing chunking,
cleaning, tagging or ranking. Accept new scores with `python -m misslens.cli retrieval-eval --save-baseline`."""

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
    assert rep.golden["modes"]["plain"]["hit@5"] >= 0.8                 # absolute floor besides the baseline
