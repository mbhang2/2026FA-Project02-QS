import pytest

from researcher.retrieval import searxng


def _resp(payload, status=200):
    class R:
        status_code = status
        text = ""

        def json(self):
            return payload

    return R()


def test_search_disabled_without_base_url(monkeypatch):
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    with pytest.raises(searxng.SearxngError):
        searxng.search("momentum")


def test_search_parses_and_filters(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://localhost:8080")
    payload = {"results": [
        {"title": "Momentum in futures", "url": "https://a.com/1",
         "content": "snippet"},
        {"title": "", "url": "https://a.com/2", "content": "no title"},
        {"title": "No url", "url": "", "content": "no url"},
        {"title": "Second", "url": "https://a.com/3", "content": "snippet2"},
    ]}
    monkeypatch.setattr(searxng.requests, "get", lambda *a, **k: _resp(payload))
    results = searxng.search("momentum", limit=6)
    assert [r.title for r in results] == ["Momentum in futures", "Second"]


def test_search_respects_limit(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://localhost:8080")
    payload = {"results": [
        {"title": f"r{i}", "url": f"https://a.com/{i}", "content": "c"}
        for i in range(10)
    ]}
    monkeypatch.setattr(searxng.requests, "get", lambda *a, **k: _resp(payload))
    assert len(searxng.search("x", limit=3)) == 3


def test_search_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://localhost:8080")
    monkeypatch.setattr(searxng.requests, "get",
                        lambda *a, **k: _resp({}, status=500))
    with pytest.raises(searxng.SearxngError):
        searxng.search("momentum")
