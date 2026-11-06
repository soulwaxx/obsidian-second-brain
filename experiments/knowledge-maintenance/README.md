# Longitudinal knowledge-maintenance evaluation (rows 5–7)

This directory provides the approved synthetic fixture and frozen-artifact adapter/scorer for combined #40/#41, plus an optional, explicitly enabled Pi live runner. All gold is explicitly **agent-authored and not human-reviewed**. Offline tests and the default dry-run do not execute models, access vaults, load credentials, or make network calls. A separate `--live` invocation can execute model calls only after the documented synthetic-vault, selected-auth, and external-output safeguards are satisfied.

## Blocked native pilot draft

The [2026-11-06 native pilot draft](results/native-blocked-2026-11-06/README.md) records a bounded attempt that stopped before model HTTP dispatch. It is explicitly **not a completed or comparative benchmark**: all 144 answer cells are missing, maintenance annotations are unknown/incomplete, and empirical #41 evidence remains outstanding. No raw traces or raw run metadata are published.

## Fixture and visibility boundaries

`fixtures.json` contains four dated source snapshots, six paired EN/IT intent families at every checkpoint, and evaluator-only gold in a distinct top-level `gold` object. It pins source SHA-256 hashes and exact Python Unicode-code-point offsets against original decoded text, including CRLF and Unicode. Solver-visible source records contain only checkpoint, path, text, and hash; they do not expose evaluator spans or human-review labels. The source text contains no evaluator disclaimer. Gold supplies per-task/per-checkpoint expected answer outcome/value/temporal scope/qualification, canonical pages, historical as-of dates, and coordinate pins. It corrects the 2025-03/04 retention truth to an explicit withdrawal/no current numeric period. The 2025-02-10 history question is unanswerable at the earlier checkpoint; its answer and evidence are not surfaced early. The retention question does not presuppose a later withdrawal.

API:

- `task_matrix(fixture)` produces the exact 4 × 6 × 2 × 3 = 144 answer cells.
- `maintenance_view(checkpoint, task, prior_wiki, fixture)` produces only the authorized maintenance prompt, conventions, prior wiki, and source history through that checkpoint.
- `solver_view(checkpoint, task, language, condition, prior_wiki, fixture)` is answer-only: question and treatment inputs, no maintenance hints, gold, evidence annotations, or future sources. `wiki-only` receives no source history; `sources` receives no wiki.
- `load_fixture`, `validate_fixture`, `sha256`, `canonical_hash`, and `identity_manifest` expose the pinned fixture identities/utilities.

A runner should use fresh isolated solver sessions, pass the relevant view only, preserve the same base task/settings across arms, and ensure the two wiki arms receive byte-identical checkpoint wiki copies. These safeguards are runner responsibilities, not hidden runtime behavior here.

## Versioned artifact and scorer API

`evaluation.py` implements `validate_artifact`, `score`, `score_maintenance`, `compare_compatibility`, and `seal_artifact`. It is stdlib-only and accepts a path or stdin:

```sh
python3 experiments/knowledge-maintenance/evaluation.py artifact.json
cat artifact.json | python3 experiments/knowledge-maintenance/evaluation.py
```

The v3 root is exactly `{schemaVersion, identities, settings, judgeSettings, wikiSnapshots, wikiJudgments, trials, artifactHash}`. `seal_artifact` hashes the canonical root excluding `artifactHash`; each content page, answer, source citation, snapshot, and annotation is also bound. `identities` is computed by `identity_manifest` and must exactly match actual fixture (excluding gold), gold, evaluator source bytes, pinned protocol, candidate system hash, solver settings, and distinct judge settings. Candidate-system and wiki content can vary between compared artifacts; the evaluator/fixture/gold/protocol/solver settings/judge settings cannot.

Solver `settings` explicitly pin `model`, `effort`, `attemptCount: 1`, the exact `TASK_CONTRACT` text and its verified hash, `readOnly: true`, and nonempty `limits`. Independent `judgeSettings` pin model, effort, rubric content and its verified canonical hash, and output bound. Each trial has exact cell metadata and identical solver settings, a `sourceHistoryHash`, and condition-appropriate `wikiHash`; source history and wiki hashes are validated against fixture/snapshot contents. Every cell must be present once with explicit `success`, `failed`, or `missing`. Each trial also carries stable nonsecret `unauthorizedWriteAttempts` codes. Successful cells carry answer/hash and a hash-bound answer judgment with explicit `outcome` (`supported`, `abstained`, `unsupported`, `unknown`), `complete`, covered topics, explanation, and **all** extracted claims. Claims pin exact candidate quote and containing unit/coordinates, temporal scope, disposition/relation, prior-support state, qualification judgment, explanation, and separate current/historical evidence. Current citations must come from the active checkpoint; historical citations obey the task's as-of date. Current claims cannot be certified with historical evidence; historical claims cannot use current evidence; prior support must cite a prior (not active) checkpoint. Established support, stale, contradicted-without-established-history, and unverified are kept distinct. Unknown historical support is conservative. Every claim is validated and included in output counts; an extra contradictory claim cannot hide behind a valid first claim.

Maintenance is scored separately through `wikiSnapshots` and `wikiJudgments`, not inferred from answer results. Each checkpoint snapshot pins every wiki page path/content/hash and a sorted content hash. Its v3 judgment binds the current snapshot hash and the preceding complete snapshot hash, explicitly annotates all six topics, candidate page quote/spans/hash, current/historical evidence, relation/prior support, history and qualifier handling, a complete page-to-topic inventory, transition assessment (`initialization`, `assessed`, or `unknown`), judge-marked unnecessary changes, and stable nonsecret unauthorized-write attempt codes. Initialization and unknown transitions use `unnecessaryChanges: null`; only an assessed revision has a list (including an empty list when no unnecessary changes are judged). `score_maintenance` reports finite topic coverage and accuracy, unsupported/unknown/abstained subjects, canonical-page reuse, duplicate topic pages, history preservation, qualifier loss, changed/removed paths, and unnecessary-change annotations. Empty/omitted content cannot get full quality from an empty denominator: topic counts are fixed at six; answer and maintenance accuracy use only actually judged outcomes (unknown/unverified are not invented failures), are null when no outcomes are scoreable, and always report expected/scored/unknown counts and coverage separately. A complete but partially unknown result can retain 100% accuracy over its judged subset, but its reduced coverage and unknown count remain explicit. Incomplete maintenance judgments receive null accuracy.

`score` reports per-checkpoint/language/intent/condition answer accuracy and coverage, claim-label/stale/contradiction/unknown/abstention/qualifier/unresolved answer-citation and judge-evidence counts, and per-cell paired differences for `sources+wiki − sources` and `wiki-only − sources`. Each treatment comparison retains the task/checkpoint/language pairing; translations are not treated as independent. Correct abstention is separately represented from omitted, incomplete, unsupported, and unknown judgments; an answer abstention requires a nonempty hash-bound answer, and a wiki abstention must quote an actual snapshot page expressing uncertainty. Historical answers can score correct only with supported historical evidence. Fake judge judgments are trusted as model judgments after mechanical candidate/source/hash/span checks; no keyword heuristic treats a claim as true. A false semantic annotation remains a model-judge risk, not human verification.

Usage/latency/cost are not estimated from text. The optional live runner fills the efficiency report from actual provider-reported usage and host-measured session/index timings; solver and judge calls remain separate, shared maintenance preparation is reported separately, and correct-only answers use the evaluator's `correct === true` labels. Prices remain null, and a missing usage value makes that category's aggregate token totals null.

## Optional Pi runner (development-only)

`run.mjs` is an explicit development experiment. It is not installed, exposed as a package command, run by normal tests/releases, or a promotion gate. The default invocation is an offline dry-run; `--live` must be supplied literally on the CLI and cannot be enabled from config/environment.

```sh
node experiments/knowledge-maintenance/run.mjs --help
node experiments/knowledge-maintenance/run.mjs --config /absolute/path/to/km-config.json --dry-run
node experiments/knowledge-maintenance/run.mjs --config /absolute/path/to/km-config.json --validate-config
```

The strict JSON config format is illustrated by `config.example.json`. CLI overrides take precedence over `OSB_KM_*` environment values, which take precedence over the dedicated config; documented provider/model/effort/budget defaults apply after those sources. Supported environment fields are `SOLVER_PROVIDER`, `SOLVER_MODEL`, `SOLVER_EFFORT`, `JUDGE_PROVIDER`, `JUDGE_MODEL`, `JUDGE_EFFORT`, `WORKDIR`, `AUTH_FILE`, `VAULT_ROOT`, `CALLS`, `MINUTES`, `OUTPUT_TOKENS`, `MAX_OUTPUT_TOKENS`, and `RESPONSE_TOKENS` (the latter two both set the per-response cap). Unknown keys and values fail closed. Dry-run/config validation prints a safe nonsecret manifest with the exact 144 cell IDs and source, gold, code, prompt, rubric, and settings hashes. It does not load Pi, read auth, inspect profiles/vaults, create paths, or call a provider.

The tool-less answer judges receive identical active/historical source evidence, the same task question and gold, and no condition/model/session metadata or condition-specific wiki. Candidate answer/hash and an opaque candidate UUID are the only trial-specific judge inputs. `annotation-schema.json` is included in every judge prompt with exact fields, enums, and evidence structures; the host binds checkpoint/snapshot/answer hashes and mechanically validates quotes, hashes, spans, and topic/page IDs. A malformed or truncated judgment becomes explicit unknown without repair. The separate answer-citation inventory is a bounded syntactic metric: it recognizes Markdown links, Obsidian wiki links, `[wiki/path.md]`, `[sources/path.md]`, and `[checkpoint/sources/path.md]`, with Unicode code-point spans. It resolves only exact targets in the frozen source history or wiki snapshot supplied to that arm, and labels unavailable-in-arm, missing, and future targets separately. It does not infer uncited references, resolve aliases or encoded paths, or establish whether a resolved target supports the answer. Judge-evidence coverage is reported separately; no citations yields zero answer citations, not an assumed judge-evidence failure.

Standalone CLI live use additionally requires `--live`, an explicit selected auth file, a host-created disposable synthetic vault with a `.knowledge-maintenance-synthetic` marker containing exactly `obsidian-second-brain synthetic knowledge-maintenance vault v1` plus a `wiki/` directory, and a new absolute external `workdir`. Both vault and output must be real paths outside this repository and the user's home/profile; output collisions and symlinked paths are refused. The host must prepare the synthetic vault and any disposable Git setup before invoking the runner. Before sessions, native tool calls, and host retrieval builds, the experiment checks the entire selected vault tree—including `.vault-meta`, `.obsidian`, and `.git`—with no-follow metadata checks and refuses symlinks or regular files with multiple hard links; this check does not read file contents. Git support is deliberately limited to an ordinary `.git/` directory inside the synthetic vault or a truly non-Git vault. A `.git` indirection file (including linked-worktree and `--separate-git-dir` layouts), external common/alternate Git metadata, an ancestor repository discovered when the vault has no `.git/`, and ambient Git environment redirects/config/trace overrides are refused before runner setup. Never point this experiment at a real or personal vault. The standalone CLI runner uses only the selected existing OpenAI `sk-` API-key record, loaded read-only into in-memory Pi auth storage; command-valued, environment-interpolated, OAuth/ChatGPT subscription records are refused because their Responses API path does not establish the required max-output-token contract. No auth file is written or copied. Proxy environment variables are refused and standalone requests are restricted to `https://api.openai.com/v1/responses`.

Native invocation uses the installed `pi-subagents` parent tool and its managed child `DefaultResourceLoader` extension paths. Do not use the sample `examples/extensions/subagent` launcher (it is not the installed implementation), direct `pi -e`/`pi -p`, a nested agent, or an ordinary model turn. First create one uniquely named project-scope controller from the parent session:

```js
subagent({ action: "create", options: { config: {
  name: "km-native-controller",
  description: "One-shot native knowledge-maintenance command entry",
  scope: "project",
  systemPrompt: "Handle only the supplied native command. Do not add tasks or attempt follow-up work.",
  systemPromptMode: "replace",
  inheritProjectContext: false,
  inheritGlobalContext: false,
  inheritSkills: false,
  defaultContext: "fresh",
  model: "openai/gpt-6.1-sol",
  thinking: "medium",
  extensions: "/absolute/path/to/obsidian-second-brain/experiments/knowledge-maintenance/native-bridge.mjs"
} } });
```

Use the created definition's exact name/path from the management result. The management API's `config.tools` accepts a CSV string or `false`, not an array; it cannot directly express `tools: []`. Edit **only this newly created temporary project definition** to add the YAML frontmatter `tools: []` and any omitted `inheritGlobalContext: false` field, then verify the resolved definition with `subagent({ action: "get", agent: "km-native-controller" })`. Do not leave the controller inheriting tools or context. Then issue the single native child call from the parent:

```js
subagent({
  agent: "km-native-controller",
  cwd: "/absolute/path/to/obsidian-second-brain",
  task: "/km-native --live --config /absolute/path/to/km-native-config.json",
  async: false,
  model: "openai/gpt-6.1-sol:medium",
  options: { context: "fresh" }
});
```

The controller definition is project-local under the current repository's `.pi/agents/`. Keep the controller's `cwd` at that repository so the subagent manager can discover its project-local definition; an unrelated disposable `cwd` produces `Unknown agent`. Only the command controller runs there, with no inherited context or tools; the actual experiment sessions still use the external synthetic vault and disposable workspace. Its `extensions` field is passed to `DefaultResourceLoader.additionalExtensionPaths` by the actual installed child session, not discovered from `.pi/extensions`, so no Pi project-extension trust override or global/profile edit is involved. `tools: []`, fresh context, `systemPromptMode: replace`, and the three `inherit*Context/Skills: false` fields prevent tools and personal/project instructions/skills from being carried into the controller. The actual installed foreground and background implementations both call the child session prompt with `Task: ${task}`; the bridge input hook recognizes that exact wrapper, invokes the command handler and returns `handled` before any ordinary model turn. The offline actual-SDK regression explicitly loads the extension and verifies that wrapper yields no model messages.

For the parent's actual child-entry probe, first use the same invocation but task `/km-native` (without `--live`, config, or vault paths). The handler must report the literal-live requirement and return without an ordinary model turn, runtime, auth, output, or provider request. After that offline-entry probe, the parent may choose to run the separately approved live command above. When the parent finishes the probe, it must delete **only** this unique temporary project definition through management (`subagent({ action: "delete", agent: "km-native-controller", options: { agentScope: "project" } })`) and verify it is gone. If the parent session does not have project-agent management enabled/trusted, or cannot establish the empty tool allowlist on the just-created definition, stop and report that local limitation; do not edit global settings/trust, use the misleading sample launcher, or proceed to a model turn.

The config contains the same solver/judge, limits, absolute `vaultRoot`, and new external `workdir` settings as `config.example.json`, but must not contain `authFile`; native mode refuses `--auth-file` and any resolved auth-file setting. Pi must already have the requested solver/judge models available in its active `ModelRegistry`. The bridge creates a fresh isolated `ModelRuntime` with only a nonsecret internal sentinel, strips its auth/header/environment/base-URL overrides, and delegates streaming through the host registry's public `streamSimple`/`stream` methods so host request-time auth remains in Pi. It does not access a credential file, call auth-resolution methods, or copy credentials.

Before constructing a runner or output directory, native preflight requires both selected models to be unmodified built-in OpenAI Responses models, a clear `undefined` public registry error, no registered native-provider override or extension-provider configuration, and a positively initialized configured non-OAuth auth snapshot. It checks `hasConfiguredAuth(model) === true`, `isUsingOAuth(model) === false`, a configured public auth-status source (`stored`, `runtime`, or `environment`), and membership in the public available-model snapshot. Missing methods, unknown/unconfigured snapshots, malformed status values, OAuth, composition errors, and composed provider/model configuration all fail closed; OAuth is unsupported because the native public API cannot prove the host credential store is in-memory-only or prevent an SDK refresh from persisting it. Admission is rechecked at each host-provider delegation, without resolving credentials or inspecting auth/config files. The route also compares active provider/model data to an unconfigured built-in SDK baseline, pins the active model base URL, and checks the exact HTTPS `/responses` endpoint at the injected-fetch boundary; redirects are rejected. The offline installed-SDK tests cover composition-error fallback and OAuth rejection before command resolution, store modification, stream, refresh, or model fetch, as well as a positive initialized in-memory API-key fake-fetch path. This proves the inspected SDK path, not host credential correctness, server-side routing, provider-internal retries, or an OS sandbox. Native access is therefore limited to hosts with positively evidenced non-OAuth auth; native mode still requires separate parent approval.

The OpenAI Responses request is checked at the injected fetch boundary for the selected model, role-specific native reasoning effort, and `max_output_tokens` (at most 4096); provider and agent retries, compaction, and cache warming are disabled. An effort mismatch stops with `request-contract-mismatch` before fetch; it is never silently replaced with another effort. In the tested Pi SDK 1.5.0 path, a configured `high` judge effort was serialized as `medium`, so high-effort judge runs stop before provider dispatch. Revisit this limitation only after the SDK emits the configured role-specific effort in the native request. Controller `session.abort()` alone does not cancel an experiment while Pi is awaiting its input hook, because the session is idle during that hook; the bridge therefore aborts the active run on `session_shutdown` and awaits partial-report finalization and workspace cleanup. Installed `pi-subagents` bounds shutdown handlers to five seconds before disposal, so unusually slow finalization can still be cut short by that host limit. Attempts are counted immediately before fetch. Raw `response.completed` usage is required; missing/invalid usage stops further calls. The sidecar records nonsecret attempt counts, reported input/cached/output/total tokens, response IDs/model IDs where available, elapsed time, and unknown pricing/backend revision. It never estimates tokens from text or guarantees a dollar cap. Timeout cancels active sessions. `artifact-v2.json` is initialized with all missing cells and explicit unknown judgments and is checkpointed as the run proceeds; malformed judge output stays unknown without repair/retry. `score.json` separates the 144-cell answer score, finite-topic maintenance score, model completions, evaluator-correct-only answers, solver-versus-judge usage by condition, and maintenance-solver, maintenance-judge, and indexing times/cost categories. Unknown/missing usage is never estimated. Raw prompts/solver traces are not copied into reports.

An example pilot command (only after the host has prepared the two external paths and reviewed config) is:

```sh
node experiments/knowledge-maintenance/run.mjs --config /absolute/path/to/km-config.json --live
```

The default solver/judge are OpenAI `gpt-6.1-sol`/`gpt-6.1-terra`, both medium effort. Model availability, actual account usage, immutable provider revision, and semantic judge quality are not established offline. Unsupported auth modes stop before dispatch. Live execution must be separately approved and must never be used as a test.

## Verification

```sh
python3 tests/test_knowledge_maintenance.py
node tests/knowledge-maintenance-live.test.mjs
npm run test:knowledge-maintenance-runtime -- --require-runtime
python3 tests/test_retrieval_evaluation.py
git diff --check
```

The live-support test always runs its offline config, budget, fetch-wrapper, and failure-path checks; without an installed Pi SDK it reports the native serializer/coordinator section as `UNVERIFIED` and exits successfully. `npm run test:pi-runtime -- --require-runtime` requires Pi and runs both knowledge-maintenance runtime contracts. Tests use fixed synthetic annotations to exercise the exact matrix, isolation, chronological gold, current/historical citation binding, fabricated contradiction, stale labels, all-claim handling, correct abstention, omission/incomplete/null scoring, canonical duplication/churn/history metrics, quote/hash/identity/settings drift, and incompatible/missing-arm rejection. These are scorer mechanics tests, not evidence that a real model or judge is accurate. The pilot is low-power/topic-dependent; no score is a promotion or release threshold.

Design references: OpenWiki `305599f8e44203f9ca6b74bb6b38eebaf0b0405e`, `evals/ledger`, and `evals/deepswe`. No upstream code was copied.
