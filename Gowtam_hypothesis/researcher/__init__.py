"""Autonomous hypothesis researcher (+ orchestration loop) workstream.

This is a standalone workstream tree (sister to ``Gowtam_L1/``), not part of the
MVP's ``src.core`` frozen harness. It produces the *input* the harness already
consumes: linted hypothesis files under ``config/hypotheses/``.

Two decisions worth stating up front, because they are the difference between a
producer that strengthens the harness's guarantees and one that quietly breaks
them:

1. **The package is ``researcher``, not ``src/researcher``.** The main repo owns
   the ``src`` namespace (``src.core.loop.hypothesis`` is the rail this workstream
   feeds). A second ``src/`` here would collide on ``sys.path`` the moment the
   driver imports *both*. A distinct top-level name keeps the import graph
   unambiguous.

2. **The agent does not run this code, and this code does not run the strategy.**
   This package is the *driver* tier: it has network and calls the LiteLLM gateway
   directly (plain ``POST {base}/chat/completions``, mirroring
   ``src.core.loop.llm``). The MVP's strategy subprocess has no network and never
   imports this package, exactly as it never imports its own scorer.
"""
from .schemas import SourceClaim, DataRequires, HypothesisRecord
from .render import render

__all__ = ["SourceClaim", "DataRequires", "HypothesisRecord", "render"]
