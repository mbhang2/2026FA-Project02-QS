import pytest

from researcher.retrieval import arxiv


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query: all:momentum</title>
  <entry>
    <id>http://arxiv.org/abs/2401.01234v1</id>
    <title>Sequential Structure in Intraday   Futures Data</title>
    <summary>  A study of intraday   structure.  </summary>
    <published>2024-01-03T00:00:00Z</published>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.05678v1</id>
    <title>Second Paper</title>
    <summary>Another abstract.</summary>
    <published>2024-01-04T00:00:00Z</published>
  </entry>
</feed>
"""


def test_parse_atom_extracts_entries():
    entries = arxiv.parse_atom(FEED)
    assert len(entries) == 2
    e0 = entries[0]
    assert e0.title == "Sequential Structure in Intraday Futures Data"
    assert e0.summary == "A study of intraday structure."
    assert e0.url == "http://arxiv.org/abs/2401.01234v1"


def test_parse_atom_rejects_non_feed():
    with pytest.raises(arxiv.ArxivError):
        arxiv.parse_atom(b"<html><body>not a feed</body></html>")
