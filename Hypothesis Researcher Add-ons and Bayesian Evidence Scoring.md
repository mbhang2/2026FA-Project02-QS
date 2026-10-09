# Hypothesis Researcher: Add-ons and Bayesian Evidence Scoring

Design document v1 for Gowtam · October 2026

## 1. How I understand the system today

The researcher turns one vague research ask into a population of linted, falsifiable hypotheses and writes them as .md files. It never writes strategy code and never decides winners. The campaign, its nulls and the multiple-testing correction do the judging.

```
Research Ask
  → Decompose (LLM: 3–6 mechanism families)
  → Retrieve (arXiv, Semantic Scholar, SearXNG opt-in)
  → Propose (LLM: hypothesis + spec)
  → Critique + Refine (bounded, max 2)
        hard gate: deterministic lints (gate-vocab leak, outcome leak, fake citation)
        soft: MiniCheck NLI grounding, advisory review
  → Render + Rail Lint (final authority)
  → config/hypotheses/pre-registration
  → Campaign (register → run → nulls → report)
  → Gate (max_of_n accept/reject)
  → read back approved outcomes → orchestrate next direction → refined ask
```

Design principles that I think are the right ones and that every add-on below should respect:

1. **Propose is separated from decide.** Nothing in the researcher can influence acceptance.
2. **Hard gates are deterministic; soft signals are advisory.** This keeps runs reproducible.
3. **Every loop is bounded.** Refinement is capped at 2, and blocked items are logged, counted and not retried.
4. **Only retrieved sources may be cited.** Invented citations are blocked.

### Gaps I see

1. All retrieved sources are treated equally, and ranking is by similarity only.
2. The outer loop closes at the ask level in prose. Campaign outcomes do not update anything quantitatively.
3. Trial counting is incomplete. Blocked and refined hypotheses, and every parameter range in a spec, are forking paths. A max\_of\_n gate over N=50 needs a null built for the maximum statistic, not for a single run.
4. Diversity is only enforced at decompose time. Two proposals can be near-duplicates, or repeat something an earlier campaign already tested.
5. Time-based leakage is not linted. A paper published after your train window, or a strategy that decayed after publication, changes what the evidence means.
6. Web results from SearXNG are untrusted text going into an LLM prompt.
7. The spec field is required but not machine-checked against the data catalog or a cost model.
8. There is no evaluation harness for the researcher itself. The 49 tests cover code, not proposal quality.

## 2. Add-on catalogue

Priority: P1 do first, P2 next, P3 later. Effort: S under a day, M a few days, L a week or more.

| ID | Add-on | Why it matters | Effort | Priority |
| --- | --- | --- | --- | --- |
| A1 | Bayesian Weighted Information Score (Section 3) | Weights evidence by reliability and feeds the confidence prior | L | P1 |
| B1 | Trial ledger and effective-N accounting | Honest multiple-testing correction | M | P1 |
| B2 | Max-statistic null and Deflated Sharpe in the report | Matches the max\_of\_n gate | M | P1 |
| C1 | Typed spec schema plus data and cost checker | Catches unrunnable or free-lunch hypotheses before the campaign | M | P1 |
| D1 | Prompt-injection hardening for web sources | SearXNG output is untrusted | S | P1 |
| E1 | Researcher eval harness (golden asks, adversarial leaks) | Measures proposal quality and lint recall | M | P1 |
| F1 | Population diversity and novelty filter | Avoids 50 versions of one idea | M | P2 |
| F2 | Hypothesis registry across campaigns | Novelty and learning memory | M | P2 |
| G1 | Retrieval upgrades (rerank, citation graph, SSRN, caching) | Better evidence per query | M | P2 |
| G2 | Publication-date and decay lint | Temporal leakage and alpha decay | S | P2 |
| H1 | Critic ensemble and confidence calibration | Reduces single-critic noise, checks priors against reality | M | P2 |
| I1 | Deterministic replay bundle | Reproduce any proposal exactly | M | P2 |
| J1 | Bandit orchestrator over families | Explore/exploit using outcomes | M | P3 |
| K1 | Human review queue with reasons | Fast triage of borderline items | S | P3 |

### B. Statistical honesty

**B1 Trial ledger.** Append one row per hypothesis ever created, including blocked and refined versions: id, parent id, family, spec hash, number of parameter combinations implied by the spec ranges, outcome. Campaign reports then use effective trials = sum of parameter grid sizes across everything tried on the same dataset, not just N=50. Store it as an append-only JSONL file next to the pre-registration.

**B2 Max-statistic null.** For max\_of\_n, the null must be the distribution of the best of N under no edge. Build it by block-bootstrapping or sign-flipping returns, running the same N configurations, and recording the maximum each time. Report the observed max against that null. Also report Deflated Sharpe Ratio and, if you have the return matrix, Probability of Backtest Overfitting. Use a stationary or block bootstrap because 5-minute bars are autocorrelated and volatility clusters.

**B3 Locked holdout.** Keep one slice of XAUUSD data that no hypothesis, lint or report ever touches until a candidate has passed the gate on train. Log every access.

### C. Specification quality

**C1 Typed spec.** Turn the free-text specification into a small typed block: signal definition, entry rule, exit rule, parameter ranges with min, max and step, bar size, session filter, cost assumption, data fields required. A deterministic checker then verifies four things:

- every required data field exists in the dataset catalog
- every parameter range has a finite grid (and returns its size to the trial ledger)
- a cost model is present (spread and slippage for XAUUSD matter a lot at 5-minute frequency)
- no rule references future bars (a simple lookahead lint on the formula tokens)

This is still not strategy code. It checks that the spec is compilable, which strengthens the not\_specified critique rule you added.

**C2 Cost and capacity sanity.** Estimate turnover implied by the entry and exit rules and flag specs whose expected edge per trade is below a configurable multiple of the spread.

### D. Security

**D1 Web source hardening.** Treat every SearXNG snippet as hostile input. Strip instructions, wrap each source in a delimited block, tell the proposer that source text is data, and run a cheap injection detector (imperative phrases aimed at the model, hidden text, odd unicode) before anything enters the prompt. Add a domain allowlist and denylist, store a content hash per fetched page, and cap characters per source.

### E. Evaluating the researcher itself

**E1 Eval harness.** Build a folder of golden asks with expected properties, plus adversarial cases:

- asks that tempt the model to leak a realized outcome
- asks with no real literature (the system should block, not invent)
- a seeded fake citation in the source pool
- near-duplicate sources

Track lint recall (did the hard gate catch every planted violation), block rate, refinement rate, spec completeness rate, and percent of hypotheses that survive the campaign gate. Run it in CI with a frozen retrieval snapshot so results do not drift.

### F. Population quality

**F1 Diversity and novelty.** Embed each hypothesis mechanism statement. Reject or merge pairs above a similarity threshold, and compare against the registry (F2). Require coverage across families with a minimum count per family when budget allows.

**F2 Registry.** One table across campaigns: hypothesis id, family, mechanism embedding, spec hash, dataset, outcome, date. It powers novelty checks, the Bayesian family priors and the bandit.

### G. Retrieval and evidence handling

**G1 Retrieval upgrades.** Add a cross-encoder rerank after the initial pull, citation-graph expansion (one hop from high-quality seeds), SSRN and journal metadata where accessible, per-family query expansion, and a local cache keyed by query plus date. Dedupe by DOI, arXiv id and normalized title.

**G2 Publication-date and decay lint.** Store publication date per source. Flag evidence whose sample period overlaps your test window. Down-weight evidence about anomalies published long ago, because published effects tend to weaken after publication (McLean and Pontiff documented this for equity anomalies). This also feeds the Bayesian scorer as a feature.

### H. Critic quality

**H1 Ensemble and calibration.** Run two critics from different model families on the semantic check, and treat disagreement as a soft flag rather than a vote. Log each hypothesis's stated confidence prior against its eventual campaign outcome and compute a Brier score per family. Once you have enough outcomes, use that to rescale priors.

### I. Operations

**I1 Replay bundle.** For every emitted hypothesis, store the ask, model ids and versions, prompts, temperature and seed, retrieval results with hashes, lint outputs and the registry snapshot version. Add a command that re-renders a hypothesis from the bundle and diffs it.

### J. Outer loop

**J1 Bandit orchestrator.** Replace prose-only next-direction logic with Thompson sampling over mechanism families using Beta posteriors from the registry (details in 3.7). Keep a minimum exploration floor so no family starves.

## 3. Bayesian Weighted Information Score (BWIS)

### 3.1 Verdict on the idea

The idea is sound and fits your architecture, provided it stays advisory. It addresses gap 1 directly. Three cautions shape the design:

1. **Do not double count.** Sources cite each other and reuse the same datasets. A naive Bayes sum would treat five papers built on the same Fama-French sample as five independent confirmations. The design therefore groups evidence by lineage and discounts within groups.
2. **Do not let it gate.** The score shapes what gets proposed and what prior is stated. It never blocks a hypothesis. That preserves proposing versus deciding and keeps reproducibility.
3. **Be honest that early weights are judgment, not data.** With few campaign outcomes, priors dominate. Label the score as uncalibrated until it has been backtested (3.11).

### 3.2 Principles

- Score claims, not documents. One paper can contain a strong claim and a weak one.
- Reliability, relevance and grounding are separate factors and multiply.
- Uncertainty is carried explicitly: the output is a credible interval, not a single number.
- Everything is deterministic: conjugate updates, fixed seeds, versioned registry snapshots.
- The score used for a hypothesis is frozen and hashed at pre-registration, and it can never include that hypothesis's own outcome.

### 3.3 Data model

An **evidence item** e is a claim extracted from a source passage. Fields:

```
evidence_id, source_id, passage_hash
claim_text
direction: supports | contradicts | neutral
strength: descriptive | moderate | strong   (see 3.5)
asset, frequency, sample_start, sample_end, method
oos_flag, code_or_data_available
lineage_id    (cluster, see 3.5)
grounding_p   (MiniCheck probability that the passage supports the claim)
```

A **source record** lives in a source registry and carries class (journal, arXiv, SSRN, practitioner, web), venue tier, citation count normalized by age, publication date, domain for web sources, and the current Beta posterior (alpha, beta) for reliability.

The **latent variable** T for a hypothesis is: a real, tradable, post-cost edge exists for this mechanism on this market and horizon.

### 3.4 Network structure

```
Source class, venue, citations, age, OOS, code ──► Reliability R_s
Query/claim vs hypothesis (embeddings, asset, bar size, regime) ──► Applicability A_e
Passage + claim (NLI) ──► Grounding G_e
Citation graph, shared data, near-duplicate text ──► Lineage cluster L
Family base rate π_f ──► Latent edge T ──► Campaign outcome O (observed later)
Evidence e depends on T, weighted by R_s · A_e · G_e, correlated within L
```

Implementation note: do not start with a general BN library. The structure above reduces to Beta-Bernoulli reliability nodes plus a log-odds aggregator, which is closed-form and deterministic. If you later want full joint inference, move the same graph to PyMC or pgmpy and compare results against the closed-form version as a test.

### 3.5 Scoring algorithm

**Step 1: Source reliability R\_s.** R\_s is the probability that a claim from this source holds in the form stated. Prior by class with strength n0 (pseudo-observations), then feature adjustments on the logit scale, then the learned posterior (3.7):

```
logit(R_s) = logit(prior_mean[class])
           + b1 * log1p(citations_per_year)
           + b2 * venue_tier
           + b3 * oos_flag
           + b4 * code_or_data_available
           - b5 * years_since_publication_decay
           - b6 * web_noise_score
R_s ~ Beta(alpha_s, beta_s)   (mean matches the line above, strength n0 plus learned counts)
```

**Step 2: Applicability A\_e in \[0, 1\].** Product of mechanism similarity (embedding or cross-encoder score), asset transfer (same asset 1.0, related commodity 0.7, equity-based claim applied to gold 0.4), and frequency transfer (daily claim applied to 5-minute bars is heavily discounted, for example 0.4, because microstructure and costs differ).

**Step 3: Grounding G\_e.** MiniCheck probability that the cited passage supports the extracted claim. If G\_e is below 0.5 the item is dropped from the sum and listed as ungrounded.

**Step 4: Effective weight and signed contribution.**

```
w_e = R_s * A_e * G_e
l_e = sign(direction) * w_e * lambda(strength) * tau
```

lambda(strength) is the maximum log-likelihood-ratio a single item can carry: descriptive or in-sample only 0.3, moderate (out-of-sample or multiple markets) 0.7, strong (independently replicated out-of-sample) 1.2. A contradiction or replication failure is signed negative with the same scale. tau is a publication-bias shrink, default 0.5, because positive results are over-published.

**Step 5: Lineage clustering.** Group items into clusters: shared dataset and author group, direct citation lineage, or near-duplicate text. Combine within a cluster with a discount exponent:

```
l_k = (sum of l_e in cluster k) / |k| ** gamma
```

gamma defaults: near-duplicate text 1.0 (counts once), same dataset 0.75, citation lineage 0.5.

**Step 6: Posterior.**

```
L = logit(pi_f) + clamp(sum_k l_k, -L_max, +L_max)
P_post = sigmoid(L)
```

Base rate pi\_f comes from the family Beta posterior in the registry (initial default about 0.08 with strength 20, meaning most mechanisms do not survive costs). L\_max default 2.0 stops a pile of weak evidence from dominating the base rate.

**Step 7: Uncertainty.** Draw M=2000 samples of every R\_s from its Beta with a fixed seed, recompute P\_post each time, and report the 5th and 95th percentiles.

**Outputs per hypothesis (the evidence block):**

- p\_prior, p\_post, interval
- n\_eff: number of lineage clusters carrying nonzero weight
- conflict: share of weight on contradicting evidence
- top contributors with their factors R, A, G and lineage
- list of dropped (ungrounded) claims
- registry snapshot version and config hash

### 3.6 Starting priors (hand-set, to be learned)

| Source class | Prior mean R | Strength n0 |
| --- | --- | --- |
| Top peer-reviewed journal | 0.70 | 10 |
| Other peer-reviewed | 0.60 | 8 |
| arXiv q-fin preprint | 0.50 | 6 |
| SSRN working paper | 0.50 | 6 |
| Practitioner whitepaper or blog | 0.35 | 4 |
| Forum, social, unknown web | 0.20 | 4 |

These are deliberately low-confidence numbers. Put them in config, version them, and record a sensitivity analysis (3.11) so you know which ones actually move results.

### 3.7 Learning from campaign outcomes

Two posteriors learn over time, both updated in batch at epoch boundaries (for example after every K decided campaigns), never mid-run, so a run always reads one frozen registry snapshot.

**Family base rate.** Beta(a\_f, b\_f). A hypothesis whose campaign is accepted adds 1 to a\_f, rejected adds 1 to b\_f. Count one outcome per hypothesis, not one per each of the N=50 configurations. Use only gate-decided outcomes that survived the multiple-testing correction.

**Source reliability.** For each source that contributed to a decided hypothesis, add credit proportional to its share of total absolute contribution:

```
credit_s = sum |l_e| for s  /  sum |l_e| over all evidence for h
alpha_s += eta * credit_s   if accepted
beta_s  += eta * credit_s   if rejected
```

eta is a tempering rate (default 0.5) because campaign outcomes are noisy and credit assignment is approximate. Cap how much any single epoch can move a source.

Two guards against self-deception:

- Many campaigns on the same dataset create selection bias in what you learn. Update only from campaigns whose accept decision survived the correction, and keep the locked holdout (B3) out of this loop.
- Never update from the hypothesis currently being scored.

**Bandit use.** Thompson sampling: draw from each family's Beta, allocate the next researcher budget in proportion to which family wins the draw, subject to an exploration floor of one hypothesis per family per N rounds.

### 3.8 Where it plugs into the pipeline

| Stage | Change |
| --- | --- |
| Retrieve | Rank and budget sources by expected information gain (how much the posterior could move) instead of similarity alone. Stop retrieving when the interval stops narrowing. Always run one query aimed at contradicting evidence. |
| Evidence extraction | New step after retrieve: LLM extracts claim items with the fields in 3.3. Deterministic validators check required fields. |
| Propose | Prompt receives an evidence dossier sorted by weight, with a mandatory steelman-the-null paragraph from the strongest contradicting items. The stated confidence prior is initialized from p\_post and the proposer may move it by at most a fixed delta. The schema records prior\_source as bwis. |
| Critique | The critic is shown which claims rest on a single low-reliability or ungrounded source. Findings stay advisory. |
| Hard gate | Add one deterministic lint: the stated prior must equal p\_post within delta, and the evidence block hash must match. This prevents the model from inflating its own confidence. |
| Pre-registration | Freeze and hash the evidence block with the hypothesis. |
| After the gate | Outcome goes to the registry. Posteriors update at the next epoch. |

### 3.9 Schemas

Source registry entry (YAML-style, one file or table row per source):

```
source_id: arxiv:XXXX.XXXXX
class: arxiv_qfin
venue_tier: 0
published: 2023-05-01
citations_per_year: 12.4
reliability_alpha: 3.0
reliability_beta: 3.0
last_updated_epoch: 4
```

Evidence block inside a hypothesis .md front matter:

```
evidence:
  p_prior: 0.08
  p_post: 0.17
  interval: [0.11, 0.26]
  n_eff: 3
  conflict: 0.22
  registry_version: epoch-4
  config_hash: <sha256>
  contributors:
    - {source: arxiv:..., R: 0.52, A: 0.8, G: 0.93, lineage: L2, l: 0.31}
```

### 3.10 Module layout and interfaces

```
researcher/bwis/
  config.py        # priors, lambda table, tau, gamma, L_max, eta, seeds
  registry.py      # source + family tables, snapshots, epoch updates
  extract.py       # claim extraction (LLM) + deterministic validators
  features.py      # R_s features, applicability, grounding adapters
  lineage.py       # clustering: dataset, citations, near-duplicate text
  score.py         # steps 1-7, returns EvidenceBlock
  update.py        # outcome credit assignment, batch epoch update
  bandit.py        # Thompson sampling over families
```

Core signatures:

```python
def score_hypothesis(family: str, items: list[EvidenceItem],
                     registry: RegistrySnapshot, cfg: BwisConfig,
                     seed: int = 0) -> EvidenceBlock: ...

def apply_outcomes(registry: RegistrySnapshot, decided: list[DecidedHypothesis],
                   cfg: BwisConfig) -> RegistrySnapshot: ...  # returns new version
```

`score_hypothesis` must be pure: same inputs and seed give byte-identical output.

### 3.11 Testing and calibration

Property tests (these catch most implementation bugs):

- adding an independent supporting item with positive weight never lowers p\_post
- duplicating an item or adding a near-duplicate source does not change p\_post beyond the gamma rule
- all weights are in \[0, 1\] and p\_post is in (0, 1)
- clamping holds: no combination exceeds L\_max
- identical inputs and seed give identical output
- contradicting items never raise p\_post

Backtest once you have outcomes:

- Does p\_post predict campaign acceptance? Report AUC, Brier score and a reliability diagram. With few campaigns, report intervals and say so.
- Ablations: uniform weights versus BWIS, with and without lineage discount, with and without applicability.
- Leave-one-source-out sensitivity: how much does p\_post move when the top source is removed? Flag fragile hypotheses.
- Prior sensitivity: vary each class prior by plus or minus 0.1 and record which hypotheses change rank.

Until at least a few dozen decided hypotheses exist, mark the score uncalibrated in the evidence block.

### 3.12 Failure modes and mitigations

| Risk | Mitigation |
| --- | --- |
| Double counting through shared data or citation chains | Lineage clusters with gamma discounts |
| LLM claim extraction errors | Grounding check, deterministic field validators, sampled human audit |
| Prompt injection via web text | D1 hardening; web class has the lowest prior and the injection detector runs before extraction |
| Goodhart: the proposer chases high-weight sources | The score is computed outside the LLM, and the hard-gate lint compares the stated prior with p\_post |
| Overfitting source reliability to few outcomes | Tempered eta, per-epoch caps, strong priors |
| Publication bias | tau shrink, always retrieve contradicting evidence |
| Domain shift (daily equity papers vs 5-minute gold) | Applicability factor, explicit frequency and asset transfer discounts |
| Circularity with the campaign | Frozen snapshot per run, no self-outcome updates, correction-aware updates only |
| Non-reproducibility | Conjugate math, fixed seeds, versioned registry, config hash in the evidence block |

### 3.13 Rollout phases and acceptance criteria

**Phase 0, shadow mode.** Extract evidence and compute scores but do not show them to the proposer. Log everything. Accept when extraction passes validators on at least 95 percent of items on your golden asks and the property tests pass.

**Phase 1, advisory.** Add the dossier and steelman-the-null paragraph to the propose prompt, and add the prior-equals-p\_post lint. Accept when proposal block rate does not rise materially and the E1 harness shows no loss in lint recall.

**Phase 2, retrieval budgeting.** Use information-gain ranking and sequential stopping. Accept when the same or better p\_post interval narrowing is reached with fewer sources fetched.

**Phase 3, learning.** Turn on epoch updates and the family bandit. Accept when the backtest shows p\_post is better than uniform weighting on Brier score, or else leave the learned updates off and keep the hand-set priors.

## 4. Suggested order of work

1. B1 and B2 first. They fix the statistical honesty of the existing campaign and need no new modeling.
2. C1 and D1. They harden the spec and the new web input path.
3. E1. Build the harness before adding BWIS so you can tell whether it helps.
4. BWIS Phase 0, then F2 (the registry both need), then BWIS Phases 1 to 3.
5. F1, G1, G2, H1, I1 and J1 as capacity allows.

The campaign xauusd-gowtam-01 that is already registered is a good first test case for B1 and B2: recompute its effective trial count and max-statistic null and see how its result changes.
