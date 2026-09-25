from researcher import retrieval
from researcher.retrieval import arxiv, semantic_scholar, SourceClip


def test_dedup_by_title_and_url(monkeypatch):
    monkeypatch.setattr(arxiv, "search",
                        lambda q, max_results=10: [
                            arxiv.ArxivEntry(entry_id="http://arxiv.org/abs/1",
                                             title="Momentum in futures",
                                             summary="a", url="http://arxiv.org/abs/1"),
                            arxiv.ArxivEntry(entry_id="http://arxiv.org/abs/2",
                                             title="Momentum in Futures",  # dup title
                                             summary="b", url="http://arxiv.org/abs/2"),
                        ])
    monkeypatch.setattr(semantic_scholar, "search",
                        lambda q, limit=10: [
                            semantic_scholar.Paper(title="Momentum in futures",
                                                   abstract="c",
                                                   url="http://arxiv.org/abs/1"),
                            semantic_scholar.Paper(title="Order flow",
                                                   abstract="d",
                                                   url="https://s2.org/9"),
                        ])

    clips = retrieval.retrieve("momentum", per_backend=4)
    # "Momentum in futures" appears as arxiv(1), arxiv(2 dup title), s2(dup) -> 1
    # "Order flow" -> 1.  So 2 distinct clips.
    assert len(clips) == 2
    titles = {c.title.lower() for c in clips}
    assert titles == {"momentum in futures", "order flow"}


def test_backend_failure_degrades_not_crashes(monkeypatch):
    def boom(q, max_results=10):
        raise arxiv.ArxivError("down")

    monkeypatch.setattr(arxiv, "search", boom)
    monkeypatch.setattr(semantic_scholar, "search",
                        lambda q, limit=10: [
                            semantic_scholar.Paper(title="Only source", abstract="",
                                                   url="https://s2.org/1")])
    clips = retrieval.retrieve("x", per_backend=4)
    assert len(clips) == 1
    assert clips[0].backend == "semantic_scholar"
