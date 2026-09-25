from researcher.schemas import (
    DataRequires,
    HypothesisRecord,
    InvalidRecord,
    SourceClaim,
)


def _valid_record(**kw) -> HypothesisRecord:
    base = dict(
        id="intraday-continuation-01",
        mechanism="An outsized move inside a session reflects a temporary "
                  "imbalance more often than new information.",
        falsifiable="If the effect is real, the price drifts back more often "
                     "than it continues after the largest moves.",
        operationalize="Measure the session move against its own realised "
                       "range so far; fade it after a threshold in units of "
                       "that range.",
        data_requires=DataRequires(interval="5m"),
        sources=(SourceClaim(claim="the effect is real in equity index "
                                   "intraday bars", source_url="https://arxiv.org/abs/0000.00000"),),
        confidence_prior="Widely claimed, cheap to test.",
    )
    base.update(kw)
    return HypothesisRecord(**base)


def test_valid_record_roundtrips():
    r = _valid_record()
    assert r.id == "intraday-continuation-01"
    assert r.sources[0].source_url


def test_rejects_empty_sources():
    try:
        _valid_record(sources=())
        assert False, "expected InvalidRecord"
    except InvalidRecord as e:
        assert "no sources" in str(e)


def test_rejects_empty_falsifiable():
    try:
        _valid_record(falsifiable="   ")
        assert False, "expected InvalidRecord"
    except InvalidRecord:
        pass


def test_rejects_multi_sentence_falsifiable():
    try:
        _valid_record(falsifiable="Prices fall. Then they rise again.")
        assert False, "expected InvalidRecord"
    except InvalidRecord as e:
        assert "single claim" in str(e)


def test_rejects_bad_id():
    try:
        _valid_record(id="not a valid id!")
        assert False, "expected InvalidRecord"
    except InvalidRecord:
        pass


def test_rejects_source_without_url():
    try:
        _valid_record(sources=(SourceClaim(claim="x", source_url=""),))
        assert False, "expected InvalidRecord"
    except InvalidRecord:
        pass
