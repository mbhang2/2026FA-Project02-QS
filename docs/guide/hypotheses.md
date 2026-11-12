# Hypotheses

The research question a campaign answers. It is a markdown file pasted
verbatim into every proposer prompt, hashed into the campaign record, and
pre-registered by its commit. This guide covers where questions come from: a
human, a statement, a paper, or the previous campaign.

[Documentation map](../README.md) · rules: pre-registration Amendments 8, 11, 14, 15 in [design/pre-registration.md](../design/pre-registration.md) · retrieval: [design/paper-retrieval.md](../design/paper-retrieval.md)

## Files

| File | Role |
|---|---|
| `config/hypotheses/<slug>.md` | The question |
| `config/hypotheses/<slug>.constraints.yaml` | What the question fixes, as mechanical and judged checks (Amendment 14) |
| `config/hypotheses/<slug>.origin.json` | Where a machine-written question came from |
| `src/core/loop/hypothesis.py` | Loads a question and lints it for gate vocabulary |
| `src/core/research/outcome_lint.py` | Catches stated market outcomes ("NQ fell 4% in March") |
| `scripts/grounding.py` | Every number must appear in a source |
| `scripts/write_hypothesis.py` | A statement or draft in, a question out |
| `scripts/review_hypothesis.py` | The review seats: is it worth asking; is a revision still the same question |
| `scripts/refine_hypothesis.py` | Question k+1 from question k and what campaign k did |
| `scripts/outcome_retrieval.py` | Diagnose campaign k, retrieve and vet papers for each problem |
| `scripts/distil_paper.py`, `scripts/retrieve.py` and the `fetch_*`, `screen_papers`, `rank_papers` scripts | One paper into one question; the retrieval pipeline |
| `config/prompts/question-distil.md`, `hypothesis-refine.md`, `hypothesis-review.md`, `family-review.md` | The prompts; fingerprinted |

## A question's shape

A `#` heading stating the claim, then sections for **the claim**, **what is yours
to choose**, **what is NOT yours to choose** (required), and *why it might be
true*, *what would make the answer interesting*, *what this is not asking for*
(recommended). It may never name the gate: no "validation", "p-value",
"significant", "trials", and the rest of `hypothesis.lint`'s list.

## Ways a question is made

| Way | Command | Read by a human first? |
|---|---|---|
| **Written by a person** | commit the file, register with `--hypothesis` | yes |
| **From a statement** (Amendment 15, *unsigned*) | `nq_campaign.py --stage register --question "…"`, or `question:` / `seed_question:` in a plan | **no**: may spend trials, may not support a claim until read |
| **From a statement, read first** | `write_hypothesis.py --question "…"`, read, commit, then `--hypothesis` | yes |
| **From a paper** (Amendment 8) | `retrieve.py`, then `distil_paper.py --paper <id>` | yes |
| **Refined from the last campaign** (Amendment 11) | automatic between campaigns of a family | **no**: same containment as from a statement |

### From a statement

`write_unattended` picks up to three registry papers for context, writes the
file, and repairs it against the gates: shape, gate-vocabulary lint, outcome
lint, grounding, length. Then **the review seat must return exactly
`NO_OBJECTION`**; "could not tell", a seat that did not run, or one overruled at
the repair cap all refuse. Constraints are kept only if they came `from` the
statement. The commit is marked `Machine-Written: <campaign>`.
Options: `--writer-model`, `--reviewer-model`, `--no-papers`.

### Refined between campaigns

After campaign k reports, inside a family and before any validation exists:

1. **Diagnose.** One call reads campaign k's evidence (what the strategy did,
   profit concentration, the auditors' objections; never scores, p-values or the
   verdict) and names up to three problems, each quoting that evidence.
2. **Retrieve.** Each problem's query is ranked (BM25) over the registry's usable
   papers. With `--fetch-new` (plan: `fetch_new_papers: true`), arXiv is searched
   for it first.
3. **Vet.** A second call keeps only papers that actually answer their problem.
4. **Refine.** The refiner applies the vetted ideas, and **its own judgement for
   problems no paper answers**. It must remain the same question and invent no
   numbers.
5. **Gates and seats.** Lint, grounding, family-unchanged, length; then the
   hypothesis seat and the family seat. **If either seat never read it, the
   refinement is refused before it is written.** The commit records the
   diagnosis, which paper answered which problem, and which changes were the
   refiner's own.

Options: `--model` (plan `refine_model`), `--reviewer-model`
(`refine_reviewer_model`), `--no-retrieve` (`retrieve: false`), `--fetch-new`.

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `NOT registered -- the review seat did not clear it` | Seat said CANNOT_TELL, objected past the cap, or did not run | Read the status; use `--reviewer-model` if its model is unavailable, or write and read it by hand |
| `the hypothesis did not pass its gates` | A lint, grounding or shape finding survived every repair | The listed findings; often a number with no source |
| `realised outcome: …` findings | The writer stated what the market did | Expected to be repaired automatically; persistent ones mean the statement itself contains it |
| Refinement `REFUSED: … seat(s) never read this question` | The seats' model is substituted or down | `refine_reviewer_model:` with a model that serves itself |
| `REFUSED. A validation artefact is readable` | Refining after the stop | Refine only during phase 1 ([sessions.md](sessions.md)) |
| `retrieved: no paper answers the diagnosis` | The registry has nothing relevant | Expected often; the refiner uses its judgement. `fetch_new_papers: true` widens the search |
| The diagnosis differs between runs | It is a model call | Each problem must still quote real evidence |
