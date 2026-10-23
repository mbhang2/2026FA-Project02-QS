"""Lexical relevance and homonym guard for retrieved source clips.

Ensures that sources about non-financial homonyms (e.g. dental decay, forest litter
decomposition, particle physics) do not pollute the hypothesis grounding context,
while keeping every legitimate quantitative, financial, or query-matching source.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import SourceClip

_STOP = frozenset({
    "the", "a", "an", "in", "of", "for", "on", "and", "or", "within",
    "during", "across", "using", "via", "with", "to", "from", "at", "by",
    "as", "into", "through", "about", "over", "between", "under", "is",
    "are", "was", "were", "be", "been", "being", "that", "this", "these",
})

FINANCE_ANCHORS = frozenset({
    "price", "prices", "pricing", "return", "returns", "market", "markets",
    "trading", "trade", "trades", "trader", "traders", "stock", "stocks",
    "commodity", "commodities", "gold", "silver", "asset", "assets",
    "volatility", "portfolio", "equity", "equities", "futures", "future",
    "investor", "investors", "investment", "alpha", "beta", "hedge",
    "hedging", "sharpe", "factor", "factors", "risk", "risks", "finance",
    "financial", "economics", "economic", "econometric", "bond", "bonds",
    "yield", "yields", "spread", "spreads", "exchange", "exchanges",
    "intraday", "momentum", "reversion", "reversal", "liquidity", "order",
    "orders", "execution", "slippage", "drawdown", "overnight", "trend",
    "arbitrage", "currency", "currencies", "fx", "forex", "macro",
    "interest", "inflation", "cpi", "fomc", "fed", "treasury", "derivatives",
    "options", "option", "garch", "estimator", "signal", "signals",
    "backtest", "backtesting", "vwap", "microstructure", "quote", "quotes",
    "limit", "bid", "ask", "almgren", "chriss", "kelly", "stop", "loss",
})

# Specific homonym disqualifiers: only drop if query does NOT mention them
# and the paper has zero financial grounding.
_HOMONYM_DISQUALIFIERS = {
    "dental": ("caries", "tooth", "teeth", "pediatric", "dental"),
    "biology_litter": ("leaf litter", "litter types", "forest floor", "litter decomposition"),
    "particle_physics": ("cern", "lhc", "electroweak", "hadron", "quark", "boson", "muon decay"),
    "medicine_cancer": ("oncology", "cancer", "tumor", "chemotherapy", "biomarker"),
}


def is_relevant(clip: SourceClip, query: str) -> bool:
    """Return True if the clip is relevant or acceptable for the query."""
    text = f"{clip.title} {clip.summary}".lower()
    q_lower = query.lower()

    # 1. Check if the clip matches obvious non-financial homonym keywords
    for category, terms in _HOMONYM_DISQUALIFIERS.items():
        if any(term in text for term in terms) and not any(term in q_lower for term in terms):
            # If it has strong finance anchors despite the word, keep it; otherwise reject
            strong_anchors = ("trading", "portfolio", "stock price", "financial market", "commodity futures")
            if not any(anchor in text for anchor in strong_anchors):
                return False

    # 2. Check if the clip contains any query content word (len > 2)
    query_words = [w.lower() for w in re.findall(r"[a-z0-9]+", q_lower) if w.lower() not in _STOP and len(w) > 2]
    has_query_term = any(w in text for w in query_words)
    if has_query_term:
        return True

    # 3. Check if the clip contains any finance/market anchor word
    has_finance_anchor = any(re.search(rf"\b{f}\b", text) for f in FINANCE_ANCHORS)
    if has_finance_anchor:
        return True

    # If neither query terms nor any finance term is present, it is unrelated
    return False


def filter_relevant(clips: list[SourceClip], query: str, min_keep: int = 2) -> list[SourceClip]:
    """Filter out clearly off-topic clips while preserving maximum good sources."""
    relevant = [c for c in clips if is_relevant(c, query)]
    # If filtering leaves fewer than min_keep, retain original clips rather than starving
    if len(relevant) < min_keep:
        return clips
    return relevant
