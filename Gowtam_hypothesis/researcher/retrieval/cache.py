"""Disk cache for retrieval queries to protect external APIs from redundant calls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[2] / "output" / "cache"


def get_cache(backend: str, query: str) -> list[dict] | None:
    if os.environ.get("DISABLE_RETRIEVAL_CACHE") or "PYTEST_CURRENT_TEST" in os.environ:
        return None
    key = hashlib.sha256(f"{backend}:{query.strip().lower()}".encode("utf-8")).hexdigest()
    path = CACHE_DIR / backend / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def set_cache(backend: str, query: str, data: list[dict]) -> None:
    if os.environ.get("DISABLE_RETRIEVAL_CACHE") or "PYTEST_CURRENT_TEST" in os.environ:
        return
    key = hashlib.sha256(f"{backend}:{query.strip().lower()}".encode("utf-8")).hexdigest()
    path = CACHE_DIR / backend / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except Exception:
        pass
