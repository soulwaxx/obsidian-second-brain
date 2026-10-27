# Hash-bound wiki reads

The package CLI provides index-independent, read-only `outline` and `read`
commands. Both require an explicit `--vault` and exactly one canonical
vault-relative `--page wiki/...md`; they bypass user integration configuration.
They do not build indexes, write lifecycle state, or establish that evidence is
factually current.

`outline` and section-selected `read --section` require the package's existing
`markdown-it-py` dependency. Install the shipped dependencies with
`python3 -m pip install -r requirements.txt`. Search, doctor, and explicit-span
`read --start ... --end ...` do not require this parser.

```sh
obsidian-second-brain outline --vault "$VAULT" --page wiki/topic.md --json
obsidian-second-brain read --vault "$VAULT" --page wiki/topic.md --json \
  --expected-hash SHA256_FROM_OUTLINE --section heading:1234
obsidian-second-brain read --vault "$VAULT" --page wiki/topic.md --json \
  --expected-hash SHA256_FROM_OUTLINE --start 100 --end 240 --view raw --max-chars 2000
```

Outline returns a SHA-256 of original file bytes, decoded source character
length, selectable frontmatter/preamble/heading entries, and bounded pagination
(default 10 entries, maximum 100). A continuation includes `expectedHash`; pass
it with `--offset` to ensure the next page describes the same source version.
Heading IDs use `heading:<zero-based-character-offset>`, so duplicate titles
remain distinct and IDs roundtrip source positions rather than heading anchors.
Heading labels are bounded; `titleTruncated` explicitly marks shortened titles.
Heading ranges include nested descendants up to the next heading of equal or
higher level. Frontmatter and body are retained; headings
are parsed with CommonMark, including ATX and Setext headings. The outline
contains labels and coordinates, not section bodies.

Read requires the outline/search `sourceHash` as `--expected-hash` and exactly
one `--section ID` or `--start N --end N` selector. Coordinates are decoded
Unicode character positions, zero-based and end-exclusive; line numbers are
one-based (`lineEnd` is the inclusive last touched line, or `lineStart` for an
empty range). UTF-8 decoding replaces malformed sequences, and CRLF pairs remain
in text and coordinates. The digest covers original bytes, not decoded text or
a projection. `raw` is the default view. `current` applies the same
coordinate-preserving strikethrough projection used by current retrieval; its
identity is `current-strike-mask-v1`.

`--max-chars` defaults to 12000 and is bounded to 1–12000. A
truncated response reports exact selected and returned ranges and a continuation
cursor. Continue with the same original selector, hash, view, and cursor; the
cursor cannot extend the selection. `truncated` is true when this response
omits either an original prefix or suffix. `selectionComplete` is true only when
this response itself covers the entire selected range; the final tail-only
continuation is not complete evidence for the original range. A partial
selection may omit qualifications.
If the source changes or the path cannot be safely reopened, the command returns
a structured error and no evidence text. Search JSON result objects preserve
existing ranking, excerpt, and score fields while adding `sourceHash` and a
`readRef` (`page`, `expectedHash`, `view`, `start`, `end`) when the excerpt's
source snapshot can be verified. Copy those values into `read` to inspect the
full underlying span. Hash identity is not factual freshness.

Pages with hidden components, symlink components, ambiguous case/Unicode
normalization aliases, hardlinks, non-regular files, or reserved `index.md`,
`log.md`, and `_plan.md` names are refused. Read-only operation does not require
an index and does not validate or rewrite frontmatter.

## Bounded-read evaluation (public aggregate)

A parent-agent evaluation used 40 frozen, context-reviewed cases against a
restricted 277-page private snapshot. The cases were not human-validated for
accuracy and did not test actual-model answer quality. The sample comprised 12
correction/negation, 12 long-section, 12 general, and 4 metadata cases. The
queries were agent-authored keyword queries; reviewed required ranges were fixed
before search, and search outputs and the evaluation's expansion rule were fixed
before comparing read budgets.

Search found the known target page in the top three for all 40 cases. The
initial selected range contained the reviewed required context in 30/40 cases;
10 selection misses were four metadata, five general, and one
correction/negation case. Only known target-page/context coverage was judged;
possible alternate evidence in other top-three pages was not exhaustively
assessed and is unknown, not a factual failure. The evaluation rule expanded to
the smallest outline range containing the whole hit span, or preserved the
explicit search span when no outline range contained it. Reviewed context was
generally the whole direct section before a child heading, with one narrow
subject-only case; metadata used its declared description. These are
source-context-range exposure measures, not evidence of truth, answer accuracy,
or model quality.

| Read budget | First-read context contained | Mean returned characters | Mean derived continuations to exhaust selection |
| ---: | ---: | ---: | ---: |
| 2,000 | 11/40 | 1,740.5 | 3.35 |
| 4,000 | 15/40 | 3,291.1 | 1.52 |
| 6,000 | 21/40 | 4,548.4 | 0.80 |
| 8,000 | 24/40 | 5,353.9 | 0.47 |
| 12,000 | 28/40 | 6,267.1 | 0.23 |

Raw and current views had the same first-read counts at each budget. The package-
relative CLI (Node launcher plus Python) had roughly 102–104 ms median read
latency in historical development measurements before final root-boundary
hardening. Final code was not rebenchmarked on the deleted private snapshot, so
these figures are not verified final-code latency. They were one local
observation per case/budget/view with budget order rotated; this is not a
cold/warm comparison or sustained-throughput claim. The
continuation counts are derived from selected-range lengths, not observed task
completion. No tested budget reached 38/40. The 12,000-character default is an
explicit maintainer choice for the highest observed coverage among tested
values, not a qualified optimum. Increasing the budget cannot recover evidence
outside the selected section: use outline to select the correct metadata or
section and continue with the same hash when needed. Ranking, search windows,
and models were not changed.

The original copied sources were unchanged, and read-only snapshot content
hashes and mtimes were unchanged; the index was built only in the copy because
the existing cache was
incompatible with the selected/excluded corpus. Private snapshot and case
artifacts were deleted. The private evaluation cannot be reproduced from this
public repository; synthetic regressions remain reproducible.
