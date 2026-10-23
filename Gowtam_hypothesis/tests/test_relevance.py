import pytest
from researcher.retrieval import SourceClip
from researcher.retrieval.relevance import is_relevant, filter_relevant


def test_relevance_keeps_finance_sources():
    clip = SourceClip(
        title="Predicting Gold Futures Volatility with GARCH Models",
        url="https://example.com/paper1",
        summary="Empirical evidence on intraday commodity futures returns and volatility clustering.",
        backend="searxng",
    )
    assert is_relevant(clip, "volatility clustering and GARCH forecasting for gold")


def test_relevance_disqualifies_homonyms():
    clip = SourceClip(
        title="Comparison of data on Early Childhood Caries in kindergarten",
        url="https://example.com/caries",
        summary="Study of pediatric tooth decay and caries in toddler populations.",
        backend="semantic_scholar",
    )
    assert not is_relevant(clip, "out-of-sample decay of published anomalies")


def test_filter_relevant_preserves_min_sources():
    clips = [
        SourceClip(title="Some Paper", url="https://example.com/1", summary="summary", backend="arxiv"),
    ]
    # Even if clip has weak matching, min_keep ensures we don't starve to 0
    filtered = filter_relevant(clips, "commodity futures", min_keep=2)
    assert len(filtered) == 1
