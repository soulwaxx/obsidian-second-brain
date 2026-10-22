# Answer-bearing passage exposure experiment

Keep production retrieval and its defaults unchanged. This experiment supports a separate, excerpt-only proposal for review, but it supplies no evidence of a quality gain on unseen notes. Removing metadata or replacing semantic chunk selection with lexical overlap has counterexamples even in these fixtures.

This record addresses [issue #21](https://github.com/soulwaxx/obsidian-second-brain/issues/21). It tests passage mechanics with public-safe synthetic sources, without reading the private vault, running a model, or building a retrieval cache.

## Hypotheses and controls

The shipped code selects the highest-cosine chunk for each page before page-level rank fusion (`scripts/retrieve.py`, `score_semantic_pages` and `fuse_ranks`). Its hybrid renderer collapses whitespace and exposes the selected chunk's first 360 characters (`hybrid_results`). Both a wrong chunk and a useful chunk's hidden suffix can therefore explain a page hit without visible evidence.

The experiment injects fixed page rankings and chunk-0 winners into the shipped `hybrid_results` helper. These injected rankings deliberately recreate failure conditions; they are not measured model predictions. The chunk boundaries also come from the fixture, rather than the production semantic chunker.

Eight agent-authored development intents have paired English/Italian questions over English notes. The fixture fixes source text, reviewed answer occurrences, temporal labels, chunk boundaries and page order before policy comparison. All cases remain development evidence: there is no fresh holdout or human validation.

Each query returns four pages. The target occupies an assigned rank of 1, 2 or 3; the other three pages have no evidence judgments. Every policy operates on all four returned pages using only the query and source text, without access to target identity or labels.

| Policy | Changed variable | Fixed variables |
| --- | --- | --- |
| `prefix` | None; checked against the shipped hybrid excerpt | Page order, chunk-0 winner, 360-character budget |
| `body-prefix` | Skip source YAML in the displayed prefix | Page order, chunk winner, remaining prefix rule |
| `query-paragraph` | Display the body paragraph with most distinct query-token matches; earliest tie; body-prefix fallback on no match | Page order, chunk winner, 360-character budget |
| `lexical-chunk-prefix` | Pick the chunk whose best body paragraph has most distinct query-token matches; earliest tie | Page order, original prefix renderer, 360-character budget |

Compare `body-prefix` with `prefix` for the metadata-display ablation. Compare `query-paragraph` with `body-prefix` for paragraph selection, since both skip YAML. Compare `lexical-chunk-prefix` with `prefix` for chunk selection independently of rendering.

The experiment keeps original decoded character offsets, including CRLF and accented text. It uses the existing evaluator's full-context judgments and visible-prefix mapping. The 360-character limit includes an ellipsis when needed; it never credits a hidden occurrence or a truncated factual qualification.

## Results

The baseline has two exposed target spans, five spans inside the selected chunk but beyond its visible prefix, and one span outside the selected chunk. Those are constructed mechanics counts, not a reproduction of the private 301-page run in the issue. Page hits stay at 4/8 at cutoff 1 and 8/8 at cutoff 3 for every policy and language, by construction.

Counts below refer to the reviewed target occurrence, with eight questions per language. Translated rows are paired observations of eight intents, rather than sixteen independent samples.

| Policy | EN exposed at 1 | EN exposed at 3 | IT exposed at 1 | IT exposed at 3 |
| --- | ---: | ---: | ---: | ---: |
| `prefix` | 2/8 | 2/8 | 2/8 | 2/8 |
| `body-prefix` | 2/8 | 2/8 | 2/8 | 2/8 |
| `query-paragraph` | 2/8 | 5/8 | 2/8 | 3/8 |
| `lexical-chunk-prefix` | 2/8 | 3/8 | 2/8 | 2/8 |

Specific observations explain why aggregate exposure alone is insufficient:

- `late-answer`: an English query-guided paragraph exposes an answer that the selected chunk already contains. The Italian translation shares no useful terms with that paragraph and keeps the hidden-prefix failure.
- `wrong-chunk`: no rendering-only policy recovers the answer from another chunk. Lexical chunk selection recovers it in English, but fails in Italian.
- `metadata-displacement` and `metadata-answer`: skipping YAML gains one body answer and loses one valid answer stored as `owner: Mira`. The aggregate change is zero in each language.
- `temporal-correction`: the baseline shows the stale duration. English paragraph selection exposes the correction; Italian overlap ties on `timeout` and selects the stale paragraph. The test supplies no temporal-safety clearance.
- `long-context`: the correct paragraph exceeds 360 characters. Its truncated qualification remains unjudgeable under every policy.
- `unicode-crlf`: the selected paragraph's original-source coordinates survive line endings and Unicode. Its visible span improves in both languages because both questions share `Caffè` with the answer.

At cutoff 3, each policy exposes 7 judged result windows and 17 unknown result windows across 24 returned results per language. Six baseline queries have unknown `answerHit` because no judged positive appears and other returned pages remain unreviewed. `query-paragraph` reduces that query-unknown count to three in English and five in Italian; those unknowns never become invented factual failures or successes.

The report retains cutoff-specific page hits, target occurrence exposure, supporting-answer hits, query-level unknowns and result-level judged/unknown counts separately. It also retains each selected window and temporal label. It reports no answer-accuracy percentage or confidence interval over this selected synthetic sample.

## Cost and identity

The excerpt cap stays at 360 characters per result. The total displayed characters below cover the top three results across eight questions; paragraph scans cover all four returned pages. These totals describe this fixture only.

| Policy | EN displayed characters | IT displayed characters | Paragraphs scanned per language |
| --- | ---: | ---: | ---: |
| `prefix` | 2,955 | 2,955 | 0 |
| `body-prefix` | 2,579 | 2,579 | 0 |
| `query-paragraph` | 1,567 | 1,913 | 36 |
| `lexical-chunk-prefix` | 2,942 | 2,955 | 37 |

A local run on macOS 27.0.1 arm64 with Python 3.14.8 measures 50 repetitions per translated query and policy, or 800 samples per policy. Four-page window-selection medians are 0.074 ms (`prefix`), 0.071 ms (`body-prefix`), 0.099 ms (`query-paragraph`) and 0.097 ms (`lexical-chunk-prefix`); nearest-rank p95 values are 0.101, 0.101, 0.142 and 0.137 ms respectively. Timing includes current-view projection and rendering, but excludes startup, ranking, evaluator work, model inference and cache validation.

Policies run sequentially without controlled host scheduling or a separate warmup. These small helper timings establish neither end-to-end latency nor a performance advantage. No inference-server memory or cache-size measurements apply because this experiment uses neither.

The source baseline is repository commit `66aaab817d2901c10eacba6d89d9d63e014dfe84`. Fixture revision `passage-mechanics-v1` has SHA-256 `fcb4e8781f933361442de101e515fd230f41b65cf29c5adef4770a33fc247b7a`; each report records fixture, individual source and script hashes plus projection scheme. Model and cache identity are explicitly `null`, because no model or cache participates.

## Reproduce

Run from the repository root with the repository's Python dependencies installed, including PyYAML. The script creates and removes a disposable temporary vault; it has no vault argument, service request or production settings write.

```sh
# Deterministic report, including per-query excerpts and hashes.
python3 experiments/passage-exposure/experiment.py > /tmp/passage-exposure.json

# Optional local helper timing. Timing fields are nondeterministic.
python3 experiments/passage-exposure/experiment.py --timing-repeats 50 \
  > /tmp/passage-exposure-timed.json

# Includes experiment regression checks and existing evaluator contracts.
python3 tests/test_retrieval_evaluation.py
```

`tests/test_retrieval_evaluation.py` checks repeatability, network refusal, rendering-versus-selection failures, metadata regressions, per-language counts, unknown judgments and CRLF/Unicode coordinates. The existing date-policy test keeps ISO-date searches lexical. Historical questions are outside this current-fact fixture and are excluded from its tables.

## Decision and next experiment

An excerpt-only proposal is worth reviewing because fixed-chunk rendering recovers three additional English occurrences here without increasing the character cap. Its strongest counterexample is the loss of valid metadata evidence, followed by stale-context selection and weaker cross-language coverage. Those failures prevent adopting this candidate as written.

Keep semantic chunk selection unchanged. The lexical selector repairs one constructed English case and leaves its Italian counterpart unresolved. Blind metadata removal has zero net exposure gain here, and increasing the budget or adding a cross-encoder remains untested.

A separate implementation proposal needs an offset-aware window contract that preserves metadata evidence and factual qualifications. Freeze its policy before evaluating a fresh, context-reviewed EN/IT slice with temporal and metadata answers; keep the exposed cases here as development tests. Report page ranking, cutoff-specific evidence and unknown counts together with shipped-CLI cost before proposing any default change.

Metadata's effect on embedding scores and page discovery remains unanswered. Testing it requires a separate representation experiment with verified model/cache identity and controlled grouping, since metadata can change both unit embeddings and semantic chunk boundaries. This fixture changes only metadata display and cannot assign the private run's selection failures to metadata.
