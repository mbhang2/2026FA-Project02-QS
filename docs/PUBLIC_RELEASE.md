# Public release — what changed from the working repository

This repository was published from the team's private working repository on
2026-10-30. Publishing required removing material that cannot be redistributed,
and removing anything from git history gives every commit a new id. This page
lists every change, so that nothing about the published record is silent.

## Removed from every commit

| What | Why |
|---|---|
| `data/cache/` session arrays (NQ) | Derived from licensed Databento data. Rebuilt locally from R2 by `scripts/warm_cache.py`. |
| One-minute ES bars under `*/market_data/raw/` | Licensed market data. The synthetic `placeholder.csv` is kept. |
| `data/papers/text/` | Full texts of third-party papers, which we have no right to redistribute. Paper metadata and abstracts in `data/papers/raw/` are kept; `scripts/fetch_fulltext.py` re-fetches the bodies. |
| `runs/**/fills/` and `*.gz` | Fill logs stored as Git LFS pointers whose objects are not carried over. Each ledger row keeps the fill-log digest. |
| `docs/reviews/*/_sources/` | 3,000 generated strategy files, regenerated deterministically from the committed generator and seeds. |
| Files over 7 MB | Mostly per-candidate null tables (`nulls.json`) and raw calibration dumps. Every campaign keeps its summary records. |
| `secrets/*` (except `.gitkeep`), `.envrc`, `.scratch/`, caches | Local configuration and scratch files. |
| `CLAUDE.md`, root `AGENTS.md`, `docs/tasks/` | Working instructions and task briefs for the team's coding agents. Standing documents may still cite them as history. |

No commit was dropped: a commit whose only changes were removed is kept, empty,
so the sequence of commits and their messages is unchanged.

## Redacted

The team's LLM proxy endpoint is private. Every occurrence — in code, tests,
documentation, run records and commit messages — was replaced with
`https://llm-proxy.invalid/v1` (`.invalid` is reserved by RFC 2606 and can never
resolve). The real endpoint is supplied at run time through the
`LITELLM_BASE_URL` environment variable, which the code already reads.

Commit-message trailers added by the team's coding assistant were also removed.

## Run records re-signed

The endpoint was recorded inside every campaign record, and campaign records
are tamper-evident: each stores a SHA-256 of its own contents. Redacting it
therefore made every record read as tampered.
`scripts/release/resign_redacted_records.py` repairs this in one disclosed,
reproducible step:

- 47 `campaign.json` records were re-hashed with `CampaignRecord.digest()`;
- the 29 `tier2-summary.json` files that cite them were re-hashed with
  `campaign_summary.digest_of`;
- each changed digest was substituted in the files that cite it.

Run against the unredacted working repository, the same script changes nothing.
The only content difference in any re-signed record is the endpoint string.

Re-signing was applied on `main`. The teammate branches keep their records as
they were at the time, with the endpoint redacted but not re-signed, so a record
checked out from one of those branches reads as tampered; the `main` copy is the
one to verify.

## Commit ids

`docs/release/commit-map.tsv` is git-filter-repo's own map from every original
commit id to its id here. The trial ledgers still name the commits their trials
ran at, as they should; `scripts/ledger_check.py` translates those ids through
the map when it checks a row (`resolve_sha`). Rows themselves were not edited.
