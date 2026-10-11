# Private retrieval evaluation

`scripts/evaluate-retrieval.py` measures prepared retrieval caches against an
explicit, versioned gold file. It is separate from the deterministic
`benchmark-semantic-retrieval.py` boundary fixture, and is not a CI model-quality
gate. Keep private corpus content, gold, queries and reports outside the source
repository and published package. Commit only synthetic public-safe fixtures.

## Run against a prepared disposable snapshot

Use an explicit vault root and gold path. The evaluator never installs models,
builds indexes, repairs caches, or changes authored pages. Prepare caches through
separate explicit builds on disposable roots, following the
[setup and retrieval contracts](setup.md#local-retrieval).

```sh
# Fully offline BM25; no Ollama required.
python3 scripts/evaluate-retrieval.py \
  --vault /absolute/path/to/disposable-vault \
  --gold /absolute/path/to/private-gold.json > /private/path/bm25-report.json

# Already-installed model, current compatible semantic cache, loopback only.
python3 scripts/evaluate-retrieval.py \
  --vault /absolute/path/to/disposable-vault \
  --gold /absolute/path/to/private-gold.json \
  --live-semantic --ollama-model qwen3-embedding:4b \
  --candidate-sizes 10 20 50 --cutoff 10 --repeats 3 \
  --condition warm > /private/path/live-report.json
```

`--ollama-url` defaults to `http://127.0.0.1:11434`; live operations accept only
loopback endpoints, disable proxies and reject redirects. Model defaults do not
authorize a download. `--timeout` is the embedding request timeout (default
8 seconds). Cutoff/candidate sizes must be 1–100; repeats must be positive.

`--condition cold|warm|unspecified` records a condition established by the
operator. It does **not** warm/unload models, flush caches or establish hardware
provenance. Record model residency and filesystem/backend conditions separately.
Do not infer inference-server hardware from the client or a forwarded loopback
endpoint.

The optional `--legacy-page-head` requires `--live-semantic` and is a separately
labeled, expensive legacy rerank probe. It embeds page heads on each request;
it is not cached cosine reranking and never runs as an automatic fallback.

## Gold version 1

Gold is a JSON object with `version: 1`, a nonempty `revision`, and nonempty
`intents`. Each intent requires:

- Unique stable `id`, nonempty `category`, `split` (`development` or `held-out`)
  and temporal `intent` (`current` or `historical`).
- Exactly paired `queries.en` and `queries.it` with equivalent reviewed meaning.
  Paired queries must have identical ISO-date scope. Historical intent currently
  requires an ISO date in both languages; no date-free intent heuristic is added.
- Nonempty acceptable `targets`. Each has an eligible `wiki/...md` `path`, UTF-8
  original-source SHA-256 `hash`, and reviewed `evidence` spans.
- Each span has integer `start`, exclusive `end`, exact original-character
  `text`, a `label` and boolean `supportsAnswer`. Coordinates refer to original
  decoded UTF-8 including original CRLF, not projected/normalized text.
- Labels are `current`, `stale`, `mixed`, `neither` or `unjudgeable`. Judge
  surrounding context, not merely a matching current-fact substring. Preserve
  ambiguity and unmarked contradictions rather than inventing factual currency.
- Optional `canonicalPages` must be a subset of acceptable target paths.

Source hashes, paths and exact span text are validated; drift is rejected rather
than scored as a ranking regression. Schema validity does not establish judgment
quality, factual truth, human review or statistical power. Record annotation
actor/authority and remaining unknowns separately. Freeze split, translations,
labels, corpus and representation before candidate rankings. Previously exposed
intents are development evidence for new hypotheses, not fresh unseen holdout.

Review enough factual/temporal context to judge the actual returned window.
Unreviewed or truncated context stays unjudgeable. Mixed explanatory history can
support an answer only through reviewed whole-span support; it is not universally
stale. Hash equality proves byte freshness against the snapshot, not deployment
or vendor truth.

## Methods and metrics

Live reports include shipped full-corpus RRF, semantic-only, and fixed lexical
shortlist cosine/RRF at each candidate size. They share production scoring and
fusion helpers, not an independent ranking clone. Candidate discovery and
fixed-shortlist reranking have different recall ceilings: a reranker cannot
recover a target absent from its lexical candidates. Compatible-vector coverage
is reported for each fixed shortlist.

- `hit@k`: at least one acceptable page occurs within the first k results.
- `recall@k`: fraction of reviewed acceptable pages present within that window.
  A bounded positive set is not an exhaustive relevance census.
- `mrr@cutoff`: reciprocal rank of the first acceptable page within the cutoff,
  or zero. Canonical-page metrics are reported separately.
- `ku@1` and `answerHit`: reviewed exposed-answer evidence. Unjudgeable results
  are `null` and excluded from judged means, not counted as invented accuracy.
- Full-source-span and visible-excerpt labels are separate. A correction beyond
  the excerpt cutoff cannot earn exposed-evidence credit. Stale-before-current
  and mixed-before-current observations are distinct.

ISO-date queries retain historical lexical policy and are labeled
`lexical-date-policy`; they are not successful semantic inference observations.
Split-specific, language/category summaries and per-query differences are
reported. Bootstrap pooling pairs by intent: translated copies are not
independent samples. Report sample/topic dependence and power limitations.

The internal Python `compare_reports(baseline, candidate, method="rrf",
split="held-out")` scorer rejects incompatible or incomplete comparisons. It
requires matched gold/corpus/source/input/projection/settings/candidates/cutoff,
verified model identity and current-by-hash coverage. It never promotes defaults.
Any model comparison must disclose differing quantization/runtime and intrinsic
model-dependent grouping even when raw input hashes/settings match.

## Provenance, timing and degradation

Reports record source/tree identity and script hashes, corpus and gold hashes,
model digest, available quantization/runtime metadata, input/projection schemes,
cache settings, candidate sizes, cutoff, actual modes, coverage and conditions.
Corpus/cache/gold/code/model drift during a run is rejected.

BM25 timing includes CLI freshness checks. Semantic timing covers query inference
and candidate scoring, **not initial semantic-cache validation, launcher startup
or end-to-end hybrid rendering**. Measure the shipped CLI independently before
advertising end-to-end latency. Repeats produce medians and nearest-rank p95;
tiny probe sets are not universal latency distributions.

Use separate explicit build experiments to measure empty-cache, compatible
repeat and localized-edit costs: input/character counts, full build duration,
cache bytes and memory only where actually observed. Client-process cumulative
peak RSS is neither per-operation memory nor inference-server RAM/VRAM. Keep
edited fixtures separate from the frozen comparison corpus.

Failed, unavailable or partial semantic runs retain diagnostics/BM25 results
but return nonzero. They cannot masquerade as complete semantic baselines.
A zero detected stale-regression count does not clear temporal safety when
context is unjudgeable. Ordinary CI remains deterministic, service-free and
uses synthetic sources/fake loopback endpoints; live quality is an explicitly
scoped private experiment, not an installation or release prerequisite.
