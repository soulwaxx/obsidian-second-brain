#!/usr/bin/env python3
"""Service-free fixture, frozen artifact, and annotation scorer for #40/#41."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_PATH = HERE / "fixtures.json"
ARTIFACT_SCHEMA = 2
ANNOTATION_SCHEMA = 2
CONDITIONS = ("sources", "sources+wiki", "wiki-only")
LANGUAGES = ("en", "it")
TASK_CONTRACT = "Answer only the assigned question from the explicitly provided knowledge; preserve temporal and stated qualifications; state uncertainty or abstain rather than inventing; cite only evidence you received. Do not write files."
PROTOCOL = {
    "id": "knowledge-maintenance-paired-v1",
    "taskContract": TASK_CONTRACT,
    "arms": list(CONDITIONS), "languages": list(LANGUAGES), "checkpoints": 4,
    "oneAttemptPerCell": True, "coordinates": "unicode-code-points",
    "temporalEvidence": "current-at-active-checkpoint; historical-no-later-than-as-of",
    "emptyCoverage": "null-accuracy-and-explicit-missing-counts",
}
SETTING_KEYS = {"model", "effort", "attemptCount", "taskContract", "taskContractHash", "readOnly", "limits"}
JUDGE_SETTING_KEYS = {"model", "effort", "rubric", "rubricHash", "maxOutputTokens"}


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _obj(value, name):
    _require(isinstance(value, dict), f"{name} must be an object")
    return value


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    try:
        fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load fixture: {exc}") from exc
    validate_fixture(fixture)
    return fixture


def validate_fixture(fixture: dict) -> None:
    try:
        _validate_fixture(fixture)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed fixture shape: {exc}") from exc


def _validate_fixture(fixture: dict) -> None:
    _obj(fixture, "fixture")
    _require(fixture.get("schemaVersion") == 1, "unsupported fixture schema")
    checkpoints, tasks, gold = fixture.get("checkpoints"), fixture.get("visibleTasks"), fixture.get("gold")
    _require(isinstance(checkpoints, list) and len(checkpoints) == 4, "fixture must have four checkpoints")
    _require(isinstance(tasks, list) and len(tasks) == 6, "fixture must have six intent families")
    _obj(gold, "gold")
    _require(gold.get("actor") == "agent-authored development fixture; not human-reviewed", "gold actor must state it is not human-reviewed")
    topics = {task.get("topic") for task in tasks if isinstance(task, dict)}
    _require(topics == set(gold.get("topics", [])) and len(topics) == 6, "gold topics must cover all six tasks")
    ids, last, observed_coordinates = set(), "", {}
    for cp in checkpoints:
        _obj(cp, "checkpoint")
        _require(set(cp) == {"id", "sources"}, "checkpoint has unknown/missing fields")
        cp_id = cp.get("id")
        _require(isinstance(cp_id, str) and cp_id > last, "checkpoints must be strictly chronological")
        last = cp_id
        _require(isinstance(cp.get("sources"), list), "checkpoint sources must be a list")
        seen_paths = set()
        for source in cp["sources"]:
            _obj(source, "source")
            _require(set(source) == {"path", "text", "sha256"}, "solver-visible source has unknown/evaluator fields")
            path, text = source.get("path"), source.get("text")
            _require(isinstance(path, str) and path and not path.startswith("/") and ".." not in Path(path).parts and isinstance(text, str), "invalid source path/text")
            _require(path not in seen_paths, "duplicate source path at checkpoint")
            seen_paths.add(path)
            digest = sha256(text)
            pinned = gold.get("sourceCoordinates", {}).get(cp_id + "/" + path)
            _obj(pinned, "gold source coordinates")
            spans = pinned.get("spans")
            _require(source.get("sha256") == digest and pinned.get("sha256") == digest and isinstance(spans, list), f"source/gold hash invalid: {path} at {cp_id}")
            for span in spans:
                _obj(span, "source span")
                start, end, quote = span.get("start"), span.get("end"), span.get("quote")
                _require(type(start) is int and type(end) is int and 0 <= start < end <= len(text) and text[start:end] == quote, f"source span drift: {path} at {cp_id}")
            observed_coordinates[cp_id + "/" + path] = {"sha256": digest, "spans": spans}
    _require(gold.get("sourceCoordinates") == observed_coordinates, "gold source coordinate pin mismatch")
    expected, canonical = gold.get("expectedAnswers"), gold.get("canonicalPages")
    _require(isinstance(expected, dict) and set(expected) == {task.get("id") for task in tasks}, "gold expectedAnswers must cover all tasks")
    _require(isinstance(canonical, dict) and set(canonical) == topics, "gold canonical page map must cover all topics")
    for task in tasks:
        _obj(task, "task")
        _require(set(task) == {"id", "intent", "topic", "question", "maintenance"}, "visible task has unknown/missing fields")
        task_id = task.get("id")
        _require(isinstance(task_id, str) and task_id not in ids and task.get("intent") in {"current", "historical", "insufficient", "conflict"}, "invalid/duplicate task")
        ids.add(task_id)
        questions = task.get("question")
        _require(isinstance(questions, dict) and set(questions) == set(LANGUAGES) and all(isinstance(v, str) and v.strip() for v in questions.values()), "each task needs paired questions")
        row = expected.get(task_id)
        _require(isinstance(row, dict) and set(row) == {cp["id"] for cp in checkpoints}, f"gold incomplete for task {task_id}")
        for cp_id, answer in row.items():
            _obj(answer, "expected answer")
            _require(answer.get("outcome") in {"supported", "abstained"} and answer.get("temporal") in {"current", "historical", "unknown"}, "invalid expected answer")
            _require((answer["outcome"] == "abstained") == (answer.get("value") is None), "gold abstention/value mismatch")


def _fixture_hash(fixture):
    visible = {key: value for key, value in fixture.items() if key != "gold"}
    return canonical_hash(visible)


def identity_manifest(fixture: dict, settings: dict, candidate_system_hash: str, judge_settings: dict) -> dict:
    validate_fixture(fixture)
    _validate_settings(settings)
    _validate_judge_settings(judge_settings)
    _require(isinstance(candidate_system_hash, str) and len(candidate_system_hash) == 64
             and all(char in "0123456789abcdef" for char in candidate_system_hash), "candidate system SHA-256 required")
    return {"fixtureHash": _fixture_hash(fixture), "goldHash": canonical_hash(fixture["gold"]),
            "evaluatorHash": sha256(Path(__file__).read_bytes()), "protocolHash": canonical_hash(PROTOCOL),
            "candidateSystemHash": candidate_system_hash, "settingsHash": canonical_hash(settings),
            "judgeSettingsHash": canonical_hash(judge_settings)}


def _validate_judge_settings(settings):
    _obj(settings, "judgeSettings")
    _require(set(settings) == JUDGE_SETTING_KEYS, "judgeSettings require exactly model, effort, rubricHash and maxOutputTokens")
    _require(isinstance(settings["model"], str) and settings["model"] and isinstance(settings["effort"], str) and settings["effort"], "judge identity/effort required")
    _obj(settings["rubric"], "judge rubric")
    _require(settings["rubricHash"] == canonical_hash(settings["rubric"]), "judge rubric hash mismatch")
    _require(type(settings["maxOutputTokens"]) is int and settings["maxOutputTokens"] > 0, "judge output limit required")


def seal_artifact(artifact: dict) -> dict:
    """Return a copy with SHA-256 over the canonical artifact excluding artifactHash."""
    sealed = dict(artifact)
    sealed.pop("artifactHash", None)
    sealed["artifactHash"] = canonical_hash(sealed)
    return sealed


def _validate_write_attempts(value):
    _require(isinstance(value, list) and all(item in {"write", "delete", "shell-write", "out-of-scope", "host-path"} for item in value),
             "unauthorized write attempts must be a list of stable nonsecret codes")


def _validate_settings(settings):
    _obj(settings, "settings")
    _require(set(settings) == SETTING_KEYS, "settings require exactly model, effort, attemptCount, taskContractHash, readOnly and limits")
    _require(isinstance(settings["model"], str) and settings["model"], "model identity required")
    _require(isinstance(settings["effort"], str) and settings["effort"], "effort required")
    _require(type(settings["attemptCount"]) is int and settings["attemptCount"] == 1, "exactly one attempt per cell required")
    _require(settings["taskContract"] == TASK_CONTRACT, "task contract drift")
    _require(settings["taskContractHash"] == canonical_hash(TASK_CONTRACT), "task contract hash mismatch")
    _require(settings["readOnly"] is True, "answer sessions must be read-only")
    _require(isinstance(settings["limits"], dict) and bool(settings["limits"]), "explicit limits required")


def trial_id(checkpoint: str, task: dict, language: str, condition: str) -> str:
    return f"{checkpoint}/{task['id']}/{language}/{condition}"


def task_matrix(fixture: dict | None = None) -> list[dict]:
    fixture = load_fixture() if fixture is None else fixture
    validate_fixture(fixture)
    rows = []
    for cp in fixture["checkpoints"]:
        for task in fixture["visibleTasks"]:
            for language in LANGUAGES:
                for condition in CONDITIONS:
                    rows.append({"id": trial_id(cp["id"], task, language, condition), "checkpoint": cp["id"],
                                 "taskId": task["id"], "intent": task["intent"], "topic": task["topic"],
                                 "language": language, "condition": condition, "question": task["question"][language]})
    return rows


def maintenance_view(checkpoint_id: str, task_id: str, prior_wiki: dict[str, str] | None = None,
                     fixture: dict | None = None) -> dict:
    """Maintenance-only task view with source history through the active checkpoint."""
    fixture = load_fixture() if fixture is None else fixture
    validate_fixture(fixture)
    _require(checkpoint_id in _checkpoint_map(fixture), "unknown maintenance checkpoint")
    task = next((task for task in fixture["visibleTasks"] if task["id"] == task_id), None)
    _require(task is not None, "unknown maintenance task")
    sources = [{"checkpoint": cp["id"], "path": source["path"], "text": source["text"], "sha256": source["sha256"]}
               for cp in fixture["checkpoints"] if cp["id"] <= checkpoint_id for source in cp["sources"]]
    return {"checkpoint": checkpoint_id, "maintenanceTask": task["maintenance"],
            "wikiConventions": "Use concise OKF Markdown pages with YAML type frontmatter; preserve one canonical page per topic, revise or retract changed facts, and retain dated history and qualifications.",
            "sourceHistory": sources, "priorWiki": dict(prior_wiki or {})}


def solver_view(checkpoint_id: str, task_id: str, language: str, condition: str,
                prior_wiki: dict[str, str] | None = None, fixture: dict | None = None) -> dict:
    """Answer-only view. It exposes no maintenance prompt, gold, evidence annotations, or future source."""
    fixture = load_fixture() if fixture is None else fixture
    validate_fixture(fixture)
    selected = next((r for r in task_matrix(fixture) if (r["checkpoint"], r["taskId"], r["language"], r["condition"]) == (checkpoint_id, task_id, language, condition)), None)
    _require(selected is not None, "unknown answer task cell")
    sources = []
    if condition != "wiki-only":
        for cp in fixture["checkpoints"]:
            if cp["id"] > checkpoint_id:
                break
            sources.extend({"checkpoint": cp["id"], "path": s["path"], "text": s["text"], "sha256": s["sha256"]} for s in cp["sources"])
    return {"task": {"question": selected["question"], "language": language, "condition": condition},
            "sourceHistory": sources, "wiki": dict(prior_wiki or {}) if condition != "sources" else {}}


def _checkpoint_map(fixture):
    return {cp["id"]: cp for cp in fixture["checkpoints"]}


def _source_map(fixture):
    return {(cp["id"], s["path"]): s for cp in fixture["checkpoints"] for s in cp["sources"]}


def _validate_evidence(group, kind, claim, trial, fixture):
    _require(isinstance(group, list), f"{kind}Evidence must be a list")
    sources, cps = _source_map(fixture), _checkpoint_map(fixture)
    target_date = fixture["gold"].get("historicalAsOf", {}).get(trial["taskId"], trial["checkpoint"])
    for item in group:
        _obj(item, "evidence item")
        _require(set(item) == {"checkpoint", "path", "sourceHash", "start", "end", "quote"}, "evidence item has unknown/missing fields")
        source = sources.get((item.get("checkpoint"), item.get("path")))
        _require(source is not None and source.get("sha256") == item.get("sourceHash"), "evidence source/hash mismatch")
        _require(item["checkpoint"] <= trial["checkpoint"], "evidence comes from a future checkpoint")
        start, end, quote = item.get("start"), item.get("end"), item.get("quote")
        text = source["text"]
        _require(type(start) is int and type(end) is int and 0 <= start < end <= len(text) and text[start:end] == quote, "evidence quote/span mismatch")
        if kind == "current":
            _require(item["checkpoint"] == trial["checkpoint"] and claim.get("temporal") == "current", "current evidence must support a current claim from the active checkpoint")
        else:
            _require(item["checkpoint"] <= target_date, "historical evidence is later than requested as-of date")
            if claim.get("temporal") == "current" and claim.get("priorSupport") == "supported":
                _require(item["checkpoint"] < trial["checkpoint"], "prior support must come from before the active checkpoint")


def _validate_claim(claim, answer, trial, fixture):
    _obj(claim, "claim annotation")
    _require(set(claim) == {"quote", "start", "end", "unitQuote", "unitStart", "unitEnd", "disposition", "temporal", "relation", "priorSupport", "qualifiers", "explanation", "currentEvidence", "historicalEvidence"}, "claim has unknown/missing fields")
    quote, start, end = claim.get("quote"), claim.get("start"), claim.get("end")
    unit_quote, unit_start, unit_end = claim.get("unitQuote"), claim.get("unitStart"), claim.get("unitEnd")
    _require(isinstance(quote, str) and type(start) is int and type(end) is int and 0 <= start < end <= len(answer) and answer[start:end] == quote,
             "candidate quote/span invalid")
    _require(isinstance(unit_quote, str) and type(unit_start) is int and type(unit_end) is int and 0 <= unit_start <= start < end <= unit_end <= len(answer) and answer[unit_start:unit_end] == unit_quote,
             "candidate containing unit invalid")
    _require(claim.get("disposition") in {"supported", "unsupported", "unknown"}, "claim disposition invalid")
    _require(claim.get("temporal") in {"current", "historical", "mixed", "unknown"}, "claim temporal classification invalid")
    _require(claim.get("relation") in {"supported", "contradicted", "not-addressed", "unknown"}, "claim relation invalid")
    _require(claim.get("priorSupport") in {"supported", "unsupported", "unknown"}, "claim prior support invalid")
    _require(claim.get("qualifiers") in {"preserved", "dropped", "not-applicable", "unknown"} and isinstance(claim.get("explanation"), str), "claim qualification/explanation missing")
    current, historical = claim.get("currentEvidence"), claim.get("historicalEvidence")
    _validate_evidence(current, "current", claim, trial, fixture)
    _validate_evidence(historical, "historical", claim, trial, fixture)
    if claim["disposition"] == "supported":
        _require(claim["relation"] == "supported" and claim["temporal"] in {"current", "historical"}, "supported claim must have a definite temporal scope and supported relation")
        _require(bool(current if claim["temporal"] == "current" else historical), "supported claim requires scope-matched source evidence")
    if claim["relation"] == "contradicted":
        _require(bool(current), "current contradiction requires active source evidence")
    if claim["priorSupport"] == "supported":
        _require(bool(historical), "established prior support requires dated historical evidence")
    if claim["temporal"] == "historical":
        _require(not current, "historical claim cannot be certified by current evidence")
        if claim["disposition"] == "supported":
            _require(bool(historical), "supported historical claim requires historical evidence")
    elif historical:
        _require(claim["priorSupport"] == "supported", "historical evidence on a current claim only establishes explicitly supported prior history")


def _validate_wiki(artifact, fixture):
    snapshots, judgments = artifact.get("wikiSnapshots"), artifact.get("wikiJudgments")
    _require(isinstance(snapshots, list) and len(snapshots) == 4, "four frozen wiki snapshots required")
    _require(isinstance(judgments, list) and len(judgments) == 4, "four maintenance judgments required")
    cps, topics = _checkpoint_map(fixture), set(fixture["gold"]["topics"])
    snapshot_map, judgment_map = {}, {}
    for snapshot in snapshots:
        _obj(snapshot, "wiki snapshot")
        _require(set(snapshot) == {"checkpoint", "status", "pages", "contentHash"}, "wiki snapshot has unknown/missing fields")
        cp_id, pages = snapshot.get("checkpoint"), snapshot.get("pages")
        _require(cp_id in cps and cp_id not in snapshot_map and snapshot.get("status") in {"complete", "failed", "missing"}, "invalid/duplicate wiki snapshot")
        _require(isinstance(pages, list), "wiki pages must be a list")
        page_map = {}
        for page in pages:
            _obj(page, "wiki page")
            _require(set(page) == {"path", "content", "sha256"}, "wiki page has unknown/missing fields")
            path, content = page.get("path"), page.get("content")
            _require(isinstance(path, str) and path.startswith("wiki/") and ".." not in Path(path).parts and isinstance(content, str), "invalid candidate wiki page")
            _require(path not in page_map and page.get("sha256") == sha256(content), "duplicate page or wiki page hash mismatch")
            page_map[path] = page
        calculated = canonical_hash([{ "path": path, "sha256": page_map[path]["sha256"]} for path in sorted(page_map)])
        _require(snapshot.get("contentHash") == calculated, "wiki snapshot content hash mismatch")
        snapshot_map[cp_id] = {"record": snapshot, "pages": page_map}
    _require(set(snapshot_map) == set(cps), "wiki snapshot checkpoints incomplete")
    for judgment in judgments:
        _obj(judgment, "wiki judgment")
        _require(set(judgment) == {"schemaVersion", "checkpoint", "snapshotHash", "complete", "topics", "pageTopics", "unnecessaryChanges", "unauthorizedWriteAttempts"}, "wiki judgment has unknown/missing fields")
        _validate_write_attempts(judgment.get("unauthorizedWriteAttempts"))
        cp_id = judgment.get("checkpoint")
        _require(cp_id in cps and cp_id not in judgment_map and judgment.get("schemaVersion") == ANNOTATION_SCHEMA
                 and judgment.get("snapshotHash") == snapshot_map.get(cp_id, {}).get("record", {}).get("contentHash"), "wiki judgment schema/snapshot binding mismatch")
        _require(type(judgment.get("complete")) is bool and isinstance(judgment.get("topics"), list), "wiki judgment completeness/topics invalid")
        snapshot_status = snapshot_map[cp_id]["record"]["status"]
        _require(snapshot_status == "complete" or (judgment["complete"] is False and all(topic.get("status") == "unknown" for topic in judgment["topics"])),
                 "failed/missing wiki snapshots require incomplete unknown-only judgments")
        seen = set()
        for topic in judgment["topics"]:
            _obj(topic, "wiki topic judgment")
            _require(set(topic) == {"topic", "status", "path", "pageHash", "quote", "start", "end", "temporal", "relation", "priorSupport", "currentEvidence", "historicalEvidence", "history", "qualifiers"}, "wiki topic has unknown/missing fields")
            name, status = topic.get("topic"), topic.get("status")
            _require(name in topics and name not in seen and status in {"supported", "abstained", "unsupported", "unknown"}, "invalid/duplicate wiki topic judgment")
            seen.add(name)
            path = topic.get("path")
            _require(path is None or path in snapshot_map[cp_id]["pages"], "wiki annotation references absent page")
            page = snapshot_map[cp_id]["pages"].get(path) if path else None
            candidate_fields = (path, topic.get("pageHash"), topic.get("quote"), topic.get("start"), topic.get("end"))
            has_candidate = any(value is not None for value in candidate_fields)
            if status in {"supported", "unsupported", "abstained"} or has_candidate:
                _require(page is not None and topic.get("pageHash") == page["sha256"], "decided wiki judgment requires a bound snapshot page")
                quote, start, end = topic.get("quote"), topic.get("start"), topic.get("end")
                content = page["content"]
                _require(isinstance(quote, str) and bool(quote.strip()) and type(start) is int and type(end) is int
                         and 0 <= start < end <= len(content) and content[start:end] == quote,
                         "wiki candidate quote/span mismatch")
            else:
                _require(all(value is None for value in candidate_fields), "unbound partial wiki candidate citation")
            _require(topic.get("history") in {"preserved", "lost", "not-applicable", "unknown"}, "wiki history judgment missing")
            _require(topic.get("qualifiers") in {"preserved", "dropped", "not-applicable", "unknown"}, "wiki qualifier judgment missing")
            _require(topic.get("relation") in {"supported", "contradicted", "not-addressed", "unknown"}
                     and topic.get("priorSupport") in {"supported", "unsupported", "unknown"}, "wiki semantic relation/prior support missing")
            topic_task = next(task["id"] for task in fixture["visibleTasks"] if task["topic"] == name)
            judgment_trial = {"checkpoint": cp_id, "taskId": topic_task}
            _validate_evidence(topic.get("currentEvidence"), "current", {"temporal": topic.get("temporal", "current"), "priorSupport": topic["priorSupport"]}, judgment_trial, fixture)
            _validate_evidence(topic.get("historicalEvidence"), "historical", {"temporal": topic.get("temporal", "current"), "priorSupport": topic["priorSupport"]}, judgment_trial, fixture)
            if status == "abstained":
                _require(topic.get("temporal") == "unknown" and topic.get("relation") in {"not-addressed", "unknown"}
                         and not topic["currentEvidence"] and not topic["historicalEvidence"], "wiki abstention must be an explicit non-factual decision")
            if status == "supported":
                expected_temporal = fixture["gold"]["expectedAnswers"][topic_task][cp_id]["temporal"]
                _require(topic.get("relation") == "supported" and topic.get("temporal") == expected_temporal,
                         "supported wiki judgment must match expected temporal scope and relation")
                if expected_temporal == "current":
                    _require(bool(topic.get("currentEvidence")), "supported current wiki topic requires active source evidence")
                elif expected_temporal == "historical":
                    _require(bool(topic.get("historicalEvidence")), "supported historical wiki topic requires dated historical evidence")
        _require(seen == topics, "wiki judgment must explicitly cover all six topics")
        inventory = judgment.get("pageTopics")
        _require(isinstance(inventory, list), "wiki page-topic inventory required for duplicate scoring")
        listed = set()
        for item in inventory:
            _obj(item, "page topic inventory item")
            _require(set(item) == {"path", "topics"}, "page-topic inventory item has unknown/missing fields")
            path, values = item.get("path"), item.get("topics")
            _require(path in snapshot_map[cp_id]["pages"] and isinstance(values, list) and set(values) <= topics, "invalid page topic inventory")
            listed.add(path)
        _require(listed == set(snapshot_map[cp_id]["pages"]), "page-topic inventory must include every candidate page")
        _require(isinstance(judgment.get("unnecessaryChanges"), list), "unnecessaryChanges must be explicit")
        for item in judgment["unnecessaryChanges"]:
            _obj(item, "change judgment")
            _require(set(item) == {"path", "reason"}, "change judgment has unknown/missing fields")
            _require(item.get("path") in snapshot_map[cp_id]["pages"] and isinstance(item.get("reason"), str), "invalid unnecessary-change judgment")
        judgment_map[cp_id] = judgment
    _require(set(judgment_map) == set(cps), "maintenance judgments incomplete")
    return snapshot_map, judgment_map


def validate_artifact(artifact: dict, fixture: dict | None = None) -> None:
    try:
        _validate_artifact(artifact, load_fixture() if fixture is None else fixture)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed artifact shape: {exc}") from exc


def _validate_artifact(artifact: dict, fixture: dict) -> None:
    _obj(artifact, "artifact")
    _require(artifact.get("schemaVersion") == ARTIFACT_SCHEMA, "unsupported frozen artifact schema")
    _require(set(artifact) == {"schemaVersion", "identities", "settings", "judgeSettings", "wikiSnapshots", "wikiJudgments", "trials", "artifactHash"}, "artifact has unknown/missing root fields")
    _require(artifact.get("artifactHash") == canonical_hash({key: value for key, value in artifact.items() if key != "artifactHash"}), "whole artifact hash mismatch")
    identities = _obj(artifact.get("identities"), "identities")
    settings, judge_settings = artifact.get("settings"), artifact.get("judgeSettings")
    _validate_settings(settings)
    _validate_judge_settings(judge_settings)
    expected_ids = identity_manifest(fixture, settings, identities.get("candidateSystemHash"), judge_settings)
    _require(identities == expected_ids, "artifact fixture/gold/evaluator/protocol/settings identity mismatch")
    snapshot_map, _ = _validate_wiki(artifact, fixture)
    trials = artifact.get("trials")
    _require(isinstance(trials, list) and len(trials) == 144, "artifact must contain exactly 144 trial cells")
    expected_rows = {row["id"]: row for row in task_matrix(fixture)}
    seen = set()
    for trial in trials:
        _obj(trial, "trial")
        _require(set(trial) == {"id", "checkpoint", "taskId", "language", "condition", "status", "answer", "answerHash", "answerJudgment", "sourceHistoryHash", "wikiHash", "settings", "unauthorizedWriteAttempts"}, "trial has unknown/missing fields")
        _validate_write_attempts(trial.get("unauthorizedWriteAttempts"))
        trial_id_value = trial.get("id")
        _require(trial_id_value in expected_rows and trial_id_value not in seen, "unexpected/duplicate trial cell")
        row = expected_rows[trial_id_value]
        _require(all(trial.get(key) == row[key] for key in ("checkpoint", "taskId", "language", "condition")), "trial metadata does not match frozen cell ID")
        seen.add(trial_id_value)
        _require(trial.get("status") in {"success", "failed", "missing"}, "explicit trial status required")
        cp_id, condition = row["checkpoint"], row["condition"]
        sources = [] if condition == "wiki-only" else [s for cp in fixture["checkpoints"] if cp["id"] <= cp_id for s in cp["sources"]]
        source_hash = canonical_hash([{ "checkpoint": cp["id"], "path": s["path"], "sha256": s["sha256"]} for cp in fixture["checkpoints"] if cp["id"] <= cp_id for s in cp["sources"]]) if sources else None
        wiki_hash = None if condition == "sources" else snapshot_map[cp_id]["record"]["contentHash"]
        _require(trial.get("sourceHistoryHash") == source_hash and trial.get("wikiHash") == wiki_hash, "trial source/wiki content binding mismatch")
        cell_settings = trial.get("settings")
        _require(cell_settings == settings, "model/effort/task settings drift between cells")
        if trial["status"] != "success":
            _require(trial.get("answer") is None and trial.get("answerHash") is None and trial.get("answerJudgment") is None,
                     "failed/missing cell cannot carry answer judgment")
            continue
        answer = trial.get("answer")
        _require(isinstance(answer, str) and bool(answer.strip()) and trial.get("answerHash") == sha256(answer), "successful answer must be nonempty and hash-matched")
        judgment = _obj(trial.get("answerJudgment"), "answer judgment")
        _require(set(judgment) == {"schemaVersion", "answerHash", "complete", "outcome", "coveredTopics", "explanation", "claims"}, "answer judgment has unknown/missing fields")
        _require(judgment.get("schemaVersion") == ANNOTATION_SCHEMA and judgment.get("answerHash") == trial["answerHash"], "answer judgment is not hash-bound")
        _require(judgment.get("outcome") in {"supported", "abstained", "unsupported", "unknown"}, "answer outcome invalid")
        _require(type(judgment.get("complete")) is bool and isinstance(judgment.get("explanation"), str), "answer completeness/explanation required")
        _require(isinstance(judgment.get("coveredTopics"), list) and len(judgment["coveredTopics"]) == len(set(judgment["coveredTopics"])) and set(judgment["coveredTopics"]) <= {row["topic"]}, "covered topics invalid")
        claims = judgment.get("claims")
        _require(isinstance(claims, list), "all extracted claims must be annotated; claims cannot be omitted")
        for claim in claims:
            _validate_claim(claim, answer, row, fixture)
        if judgment["outcome"] == "supported":
            _require(row["topic"] in judgment["coveredTopics"] and bool(claims), "supported task outcome requires explicit topic/claim coverage")
            _require(all(c["disposition"] == "supported" for c in claims), "unsupported/unknown claim cannot accompany a supported outcome")
        if judgment["outcome"] == "abstained":
            _require(not claims and not judgment["coveredTopics"], "abstention must not hide extracted factual claims")
    _require(seen == set(expected_rows), "trial matrix has missing cells")


def _claim_label(claim):
    if claim["temporal"] == "historical":
        return "historical" if (claim["disposition"] == "supported" and claim["relation"] == "supported"
                                and claim["historicalEvidence"] and claim["qualifiers"] in {"preserved", "not-applicable"}) else "unverified"
    if claim["temporal"] != "current":
        return "unverified"
    if (claim["relation"] == "supported" and claim["disposition"] == "supported" and claim["currentEvidence"]
            and claim["qualifiers"] in {"preserved", "not-applicable"}):
        return "supported"
    if claim["relation"] == "contradicted" and claim["priorSupport"] == "supported" and claim["historicalEvidence"]:
        return "stale"
    if claim["relation"] == "contradicted" and claim["priorSupport"] == "unsupported":
        return "contradicted-without-historical-support"
    return "unverified"


def _answer_label(trial, expected):
    if trial["status"] != "success":
        return None
    judgment = trial["answerJudgment"]
    if not judgment["complete"] or judgment["outcome"] == "unknown":
        return None
    if judgment["outcome"] == "unsupported":
        return "unsupported"
    # Every claim has already been mechanically checked; all claims contribute.
    if judgment["outcome"] == "supported":
        claims = judgment["claims"]
        if not claims or any(c["disposition"] != "supported" for c in claims):
            return None
        if any(c["qualifiers"] == "dropped" for c in claims):
            return "unsupported"
        if any(_claim_label(claim) == "unverified" for claim in claims):
            return None
        temporal = expected["temporal"]
        if temporal == "historical" and not any(_claim_label(c) == "historical" for c in claims):
            return None
        if temporal == "current" and not any(c["temporal"] == "current" and _claim_label(c) == "supported" for c in claims):
            return None
    return judgment["outcome"]


def score(artifact: dict, fixture: dict | None = None) -> dict:
    fixture = load_fixture() if fixture is None else fixture
    validate_artifact(artifact, fixture)
    expected_gold = fixture["gold"]["expectedAnswers"]
    expected_rows = {r["id"]: r for r in task_matrix(fixture)}
    scored, details = 0, []
    for trial in artifact["trials"]:
        row = expected_rows[trial["id"]]
        expected = expected_gold[row["taskId"]][row["checkpoint"]]
        observed = _answer_label(trial, expected)
        expected_outcome = expected["outcome"]
        correct = None if observed is None else observed == expected_outcome
        if correct is not None:
            scored += 1
        judgment = trial.get("answerJudgment", {}) if trial["status"] == "success" else {}
        claims = judgment.get("claims", [])
        labels = [_claim_label(claim) for claim in claims]
        details.append({"id": trial["id"], "checkpoint": row["checkpoint"], "taskId": row["taskId"],
                        "topic": row["topic"], "intent": row["intent"], "language": row["language"],
                        "condition": row["condition"], "expectedOutcome": expected_outcome,
                        "observedOutcome": observed, "correct": correct, "claimLabels": labels,
                        "unresolvedCitations": sum(not (claim["currentEvidence"] or claim["historicalEvidence"]) for claim in claims),
                        "unauthorizedWriteAttempts": len(trial.get("unauthorizedWriteAttempts", [])),
                        "abstained": observed == "abstained", "covered": bool(judgment.get("coveredTopics")),
                        "complete": bool(judgment.get("complete"))})
    scored_cells = sum(row["correct"] is not None for row in details)
    denominator = scored_cells
    matrix = {(r["checkpoint"], r["taskId"], r["language"], r["condition"]): r for r in details}
    paired = {arm: [] for arm in CONDITIONS[1:]}
    for cp in fixture["checkpoints"]:
        for task in fixture["visibleTasks"]:
            for language in LANGUAGES:
                base = matrix[(cp["id"], task["id"], language, "sources")]
                for arm in paired:
                    trial = matrix[(cp["id"], task["id"], language, arm)]
                    delta = None if base["correct"] is None or trial["correct"] is None else int(trial["correct"]) - int(base["correct"])
                    paired[arm].append({"checkpoint": cp["id"], "taskId": task["id"], "topic": task["topic"],
                                        "intent": task["intent"], "language": language, "difference": delta})
    by = {}
    for r in details:
        by.setdefault((r["checkpoint"], r["language"], r["intent"], r["condition"]), []).append(r)
    summaries = []
    for key, rows in sorted(by.items()):
        scored_local = sum(r["correct"] is not None for r in rows)
        denominator_local = scored_local
        summaries.append({"checkpoint": key[0], "language": key[1], "intent": key[2], "condition": key[3],
                          "expectedTasks": len(rows), "missingOrUnknown": sum(r["correct"] is None for r in rows),
                          "coverage": sum(r["covered"] for r in rows) / len(rows) if rows else None,
                          "accuracy": sum(r["correct"] is True for r in rows) / denominator_local if denominator_local else None,
                          "abstentions": sum(r["abstained"] for r in rows),
                          "staleClaims": sum(labels.count("stale") for labels in (r["claimLabels"] for r in rows)),
                          "contradictedWithoutHistoricalSupport": sum(labels.count("contradicted-without-historical-support") for labels in (r["claimLabels"] for r in rows)),
                          "unverifiedClaims": sum(labels.count("unverified") for labels in (r["claimLabels"] for r in rows)),
                          "droppedQualifiers": sum(claim.get("qualifiers") == "dropped" for trial in artifact["trials"] if trial["id"] in {r["id"] for r in rows} and trial["status"] == "success" for claim in trial["answerJudgment"]["claims"]),
                          "unresolvedCitations": sum(r["unresolvedCitations"] for r in rows),
                          "unauthorizedWriteAttempts": sum(r["unauthorizedWriteAttempts"] for r in rows),
                          "scored": scored_local, "trials": len(rows)})
    maintenance = score_maintenance(artifact, fixture)
    return {"schemaVersion": 2, "judgmentCaveat": "Semantic annotations are model judgments, not independent factual verification; extraction completeness is model-reported.",
            "answerSummaries": summaries, "answerAccuracy": sum(r["correct"] is True for r in details) / denominator if denominator else None,
            "expectedCells": len(details), "scoredCells": scored_cells, "missingOrUnknownCells": len(details) - scored_cells,
            "pairedDifferences": paired, "answers": details, "maintenance": maintenance,
            "efficiency": {"allTrials": None, "successfulOnly": None, "preparation": None,
                           "note": "Requires actual runner-measured usage and durations; no estimates are inferred."}}


def _wiki_topic_label(topic):
    if topic["status"] == "abstained":
        return "abstained"
    if topic["status"] == "unknown":
        return "unverified"
    return _claim_label({"disposition": "supported" if topic["status"] == "supported" else "unsupported",
                         "temporal": topic["temporal"], "relation": topic["relation"],
                         "priorSupport": topic["priorSupport"], "qualifiers": topic["qualifiers"],
                         "currentEvidence": topic["currentEvidence"], "historicalEvidence": topic["historicalEvidence"]})


def score_maintenance(artifact, fixture: dict | None = None) -> dict:
    fixture = load_fixture() if fixture is None else fixture
    validate_artifact(artifact, fixture)
    snapshots, judgments = _validate_wiki(artifact, fixture)
    topics = fixture["gold"]["topics"]
    canonical = fixture["gold"]["canonicalPages"]
    result = []
    previous = {}
    for cp in fixture["checkpoints"]:
        cp_id = cp["id"]
        snap = snapshots[cp_id]
        judgment = judgments[cp_id]
        page_map = snap["pages"]
        topic_map = {item["topic"]: item for item in judgment["topics"]}
        page_topics = {topic: [] for topic in topics}
        for item in judgment["pageTopics"]:
            for topic in item["topics"]:
                page_topics[topic].append(item["path"])
        changes = {path for path in set(previous) | set(page_map) if previous.get(path) != (page_map.get(path) or {}).get("sha256")}
        topic_labels = {topic: _wiki_topic_label(topic_map[topic]) for topic in topics}
        covered = sum(label in {"supported", "historical", "abstained", "unsupported", "stale", "contradicted-without-historical-support"} for label in topic_labels.values())
        correct = []
        for topic_name in topics:
            task_id = next(task["id"] for task in fixture["visibleTasks"] if task["topic"] == topic_name)
            expected = fixture["gold"]["expectedAnswers"][task_id][cp_id]
            label = topic_labels[topic_name]
            observed = ("abstained" if label == "abstained" else
                        "supported" if label in {"supported", "historical"} else
                        "unsupported" if label in {"unsupported", "stale", "contradicted-without-historical-support"} else None)
            correct.append(observed == expected["outcome"] if judgment["complete"] and observed is not None else None)
        scored = [value for value in correct if value is not None]
        result.append({"checkpoint": cp_id, "snapshotHash": snap["record"]["contentHash"],
                       "expectedTopics": len(topics), "coveredTopics": covered,
                       "coverage": covered / len(topics),
                       "accuracy": sum(value is True for value in scored) / len(scored) if scored else None,
                       "scoredTopics": len(scored),
                       "topicLabels": topic_labels,
                       "supported": sum(label == "supported" for label in topic_labels.values()),
                       "historical": sum(label == "historical" for label in topic_labels.values()),
                       "stale": sum(label == "stale" for label in topic_labels.values()),
                       "contradictedWithoutHistoricalSupport": sum(label == "contradicted-without-historical-support" for label in topic_labels.values()),
                       "unverified": sum(label == "unverified" for label in topic_labels.values()),
                       "unsupported": sum(label == "unsupported" for label in topic_labels.values()),
                       "unknownOrOmitted": sum(label == "unverified" for label in topic_labels.values()),
                       "abstentions": sum(label == "abstained" for label in topic_labels.values()),
                       "unauthorizedWriteAttempts": len(judgment["unauthorizedWriteAttempts"]),
                       "canonicalReuse": {t: canonical[t] in page_topics[t] for t in topics},
                       "duplicateTopicPages": {t: sorted(paths) for t, paths in page_topics.items() if len(paths) > 1},
                       "historyPreserved": {t: topic_map[t]["history"] for t in topics},
                       "droppedQualifiers": [t for t in topics if topic_map[t]["qualifiers"] == "dropped"],
                       "changedOrRemovedPaths": sorted(changes),
                       "judgeMarkedUnnecessaryChanges": judgment["unnecessaryChanges"],
                       "complete": judgment["complete"]})
        previous = {path: page["sha256"] for path, page in page_map.items()}
    return {"checkpoints": result, "judgmentCaveat": "Maintenance quality labels are model judgments; page hashes and cited spans are mechanically checked."}


def compare_compatibility(left: dict, right: dict, fixture: dict | None = None) -> None:
    fixture = load_fixture() if fixture is None else fixture
    validate_artifact(left, fixture)
    validate_artifact(right, fixture)
    stable = ("fixtureHash", "goldHash", "evaluatorHash", "protocolHash", "settingsHash", "judgeSettingsHash")
    a, b = left["identities"], right["identities"]
    _require(all(a[key] == b[key] for key in stable), "artifacts are not comparable: protocol/scorer/settings drift")
    _require(len(left["trials"]) == len(right["trials"]) == 144, "artifacts are not comparable: missing arms")


def empty_artifact(settings: dict, judge_settings: dict, candidate_system_hash: str, fixture: dict | None = None) -> dict:
    """Create a complete v2 pilot artifact whose not-yet-run cells are explicit missing/unknown."""
    fixture = load_fixture() if fixture is None else fixture
    identities = identity_manifest(fixture, settings, candidate_system_hash, judge_settings)
    topics = fixture["gold"]["topics"]
    snapshots, wiki_judgments = [], []
    for cp in fixture["checkpoints"]:
        snapshot_hash = canonical_hash([])
        snapshots.append({"checkpoint": cp["id"], "status": "missing", "pages": [], "contentHash": snapshot_hash})
        wiki_judgments.append({
            "schemaVersion": ANNOTATION_SCHEMA, "checkpoint": cp["id"], "snapshotHash": snapshot_hash,
            "complete": False,
            "topics": [{"topic": topic, "status": "unknown", "path": None, "pageHash": None, "quote": None,
                        "start": None, "end": None, "temporal": "unknown", "relation": "unknown",
                        "priorSupport": "unknown", "currentEvidence": [], "historicalEvidence": [],
                        "history": "unknown", "qualifiers": "unknown"} for topic in topics],
            "pageTopics": [], "unnecessaryChanges": [], "unauthorizedWriteAttempts": [],
        })
    source_hashes = {}
    for cp in fixture["checkpoints"]:
        source_hashes[cp["id"]] = canonical_hash([{"checkpoint": item["id"], "path": source["path"], "sha256": source["sha256"]}
            for item in fixture["checkpoints"] if item["id"] <= cp["id"] for source in item["sources"]])
    snapshot_hashes = {snapshot["checkpoint"]: snapshot["contentHash"] for snapshot in snapshots}
    trials = []
    for row in task_matrix(fixture):
        source_history_hash = None if row["condition"] == "wiki-only" else source_hashes[row["checkpoint"]]
        wiki_hash = None if row["condition"] == "sources" else snapshot_hashes[row["checkpoint"]]
        trials.append({"id": row["id"], "checkpoint": row["checkpoint"], "taskId": row["taskId"],
            "language": row["language"], "condition": row["condition"], "status": "missing", "answer": None,
            "answerHash": None, "answerJudgment": None, "sourceHistoryHash": source_history_hash,
            "wikiHash": wiki_hash, "settings": settings, "unauthorizedWriteAttempts": []})
    return seal_artifact({"schemaVersion": ARTIFACT_SCHEMA, "identities": identities, "settings": settings,
        "judgeSettings": judge_settings, "wikiSnapshots": snapshots, "wikiJudgments": wiki_judgments,
        "trials": trials})


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid artifact JSON: {exc}") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", nargs="?", help="frozen artifact JSON; stdin when omitted")
    args = parser.parse_args()
    try:
        print(json.dumps(score(_read_json(args.artifact)), ensure_ascii=False, indent=2))
    except ValueError as exc:
        parser.error(str(exc))
