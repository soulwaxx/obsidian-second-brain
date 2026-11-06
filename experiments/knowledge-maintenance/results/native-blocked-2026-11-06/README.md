# Blocked native pilot draft — 2026-11-06

**Status: blocked; not a completed benchmark.** This package preserves the actual bounded native attempt without publishing raw traces or raw run metadata. No retry or further live attempt was made. Empirical #41 evidence remains outstanding, and #40/#41 remain open until the user’s merge rule is satisfied.

## What happened

The native request guard stopped on `request-contract-mismatch` before the instrumented model HTTP fetch. The stop code does not disclose which of the guarded request fields differed. Do not interpret this as provider rejection, OAuth, or an exact model, output-cap, endpoint, or route mismatch. Host non-OAuth admission passed, but that does not establish credential value or type.

The experiment ledger contains **0 model HTTP attempts and 0 model completions**. Two orchestration sessions occurred (maintenance session: 253.851 ms; maintenance-judge session: 3.663 ms); these are lifecycle timings, **not model completions**. Maintenance stopped incomplete after the first checkpoint; BM25 preparation took 39.988 ms. Total recorded elapsed time was 567.008 ms.

## Evidence and limits

- The sealed v3 artifact contains 144 answer cells, all missing; 0 scored, accuracy null, paired comparisons unavailable.
- It contains four empty snapshots: the 2025-01-10 snapshot is marked complete, and the three later snapshots are missing. The four maintenance judgments are incomplete, with all 24 topic statuses unknown and 0 judged topics.
- The score is an evaluation of this incomplete artifact, not evidence of answer or maintenance quality. The independent rescore matches persisted score fields except `efficiency`, which the coordinator derives from run metadata and session timings.
- The efficiency ledger’s category token totals are zero because it recorded no model attempts. These are not provider-returned usage, not evidence of free inference, and not a statement about calls or costs outside this experiment. Pricing is unknown (`null`).
- The frozen candidate-system identity is `9b7e6ec2a8e6188a0debbde6a8fb3bbedc6467d8ab4b13bf4447dc59e6884774`; all 67 frozen code-file hashes matched. The parent recorded source revision `794b8a547bab61ee2d27a6dc3b9e1e77523ee0c9` before the attempt; the runner itself left its revision field unrecorded. This publication does not change frozen experiment code.
- Recorded runtime: Node.js 24.22.2, Python 3.14.8, Pi 1.5.0; configured solver `openai/gpt-6.1-sol` and judge `openai/gpt-6.1-terra`, both medium. The runtime reported the OpenAI Responses API and `https://api.openai.com` origin, but no model HTTP request was dispatched. Immutable backend revision is unknown.
- The fixture and gold are synthetic, agent-authored, and not human-reviewed. Offline mechanical checks do not establish semantic judge accuracy or real-model quality.

## Published files

- [`artifact-v2.json`](artifact-v2.json) — exact sealed artifact bytes from the run; schema version 3.
- [`score.json`](score.json) — exact persisted score bytes.
- [`run-summary.json`](run-summary.json) — minimal sanitized run and provenance summary; opaque session identifiers and raw metadata are not included.

The artifact and score were scanned for absolute host paths, authorization/bearer strings, and API-key-shaped strings before publication; none were found. Raw traces and raw run metadata remain external and are not linked or copied here. The artifact filename `artifact-v2.json` is retained from the runner even though its embedded schema version is 3.
