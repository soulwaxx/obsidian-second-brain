"""Offline mechanics tests with fixed, hash-bound fake judge annotations."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments/knowledge-maintenance"))
import evaluation as E


class KnowledgeMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = E.load_fixture()

    def _source(self, cp, predicate):
        checkpoint = next(item for item in self.fixture["checkpoints"] if item["id"] == cp)
        return next((cp, source) for source in checkpoint["sources"] if predicate(source))

    def _evidence(self, cp, source_predicate, historical=False):
        source_cp, source = self._source(cp, source_predicate)
        span = self.fixture["gold"]["sourceCoordinates"][source_cp + "/" + source["path"]]["spans"][0]
        return {"checkpoint": source_cp, "path": source["path"], "sourceHash": source["sha256"],
                "start": span["start"], "end": span["end"], "quote": span["quote"]}

    def _expected_topic(self, task_id, cp):
        return self.fixture["gold"]["expectedAnswers"][task_id][cp]

    def _wiki(self):
        snapshots, judgments = [], []
        for checkpoint in self.fixture["checkpoints"]:
            cp = checkpoint["id"]
            policy = "---\ntype: note\ntitle: Policy\n---\nRetention policy and response target.\nNo supporting detail is available for unspecified items.\n"
            ops = "---\ntype: note\ntitle: Operations\n---\nMaintenance and rehearsal status.\nNo supporting detail is available for unspecified items.\n"
            pages = [{"path": "wiki/policy.md", "content": policy, "sha256": E.sha256(policy)},
                     {"path": "wiki/operations.md", "content": ops, "sha256": E.sha256(ops)}]
            content_hash = E.canonical_hash([{"path": p["path"], "sha256": p["sha256"]} for p in sorted(pages, key=lambda p: p["path"])])
            snapshots.append({"checkpoint": cp, "status": "complete", "pages": pages, "contentHash": content_hash})
            topics = []
            for task in self.fixture["visibleTasks"]:
                name = task["topic"]
                expected = self._expected_topic(task["id"], cp)
                status = expected["outcome"]
                path = "wiki/policy.md" if name in {"retention", "response-target", "retention-history", "retention-conflict"} else "wiki/operations.md"
                page = next(p for p in pages if p["path"] == path)
                phrase = f"Synthetic statement for {name}."
                start = page["content"].find("Retention") if path.endswith("policy.md") else page["content"].find("Maintenance")
                quote = page["content"][start: start + len("Retention policy and response target." if path.endswith("policy.md") else "Maintenance and rehearsal status.")]
                if status == "abstained":
                    temporal, relation, current, historical, prior = "unknown", "not-addressed", [], [], "unknown"
                    candidate_quote = "No supporting detail is available for unspecified items."
                    start = page["content"].index(candidate_quote)
                    end = start + len(candidate_quote)
                else:
                    temporal = expected["temporal"]
                    relation = "supported"
                    historical = [self._evidence("2025-02-10", lambda s: "45 days" in s["text"])] if temporal == "historical" else []
                    current = [self._evidence(cp, lambda s: name in {"maintenance-day", "rehearsal-date"} and "operations" in s["path"] or name not in {"maintenance-day", "rehearsal-date"} and "policy" in s["path"])] if temporal == "current" else []
                    prior = "unknown"
                    candidate_quote = quote
                    start, end = start, start + len(quote)
                topics.append({"topic": name, "status": status, "path": path if status in {"supported", "abstained", "unsupported"} else None,
                               "pageHash": page["sha256"] if status in {"supported", "abstained", "unsupported"} else None,
                               "quote": candidate_quote, "start": start, "end": end,
                               "temporal": temporal, "relation": relation, "priorSupport": prior,
                               "currentEvidence": current, "historicalEvidence": historical,
                               "history": "preserved" if name == "retention-history" and status == "supported" else "not-applicable",
                               "qualifiers": "preserved" if status == "supported" else "unknown"})
            previous_hash = snapshots[-2]["contentHash"] if len(snapshots) > 1 else None
            assessment = "assessed" if previous_hash else "initialization"
            judgments.append({"schemaVersion": E.ANNOTATION_SCHEMA, "checkpoint": cp, "snapshotHash": content_hash,
                              "previousSnapshotHash": previous_hash, "changeAssessment": assessment, "complete": True, "topics": topics,
                              "unauthorizedWriteAttempts": [], "pageTopics": [{"path": "wiki/policy.md", "topics": ["retention", "response-target", "retention-history", "retention-conflict"]},
                                             {"path": "wiki/operations.md", "topics": ["maintenance-day", "rehearsal-date"]}],
                              "unnecessaryChanges": None if assessment == "initialization" else []})
        return snapshots, judgments

    def artifact(self):
        settings = {"model": "fake/model-v1", "effort": "medium", "attemptCount": 1,
                    "taskContract": E.TASK_CONTRACT, "taskContractHash": E.canonical_hash(E.TASK_CONTRACT),
                    "readOnly": True, "limits": {"maxOutputTokens": 300}}
        rubric = {"criteria": "judge every claim against provided source and gold; preserve history and qualifications"}
        judge_settings = {"model": "fake/judge-v1", "effort": "medium", "rubric": rubric,
                          "rubricHash": E.canonical_hash(rubric), "maxOutputTokens": 300}
        identities = E.identity_manifest(self.fixture, settings, E.sha256("candidate-system-v1"), judge_settings)
        snapshots, wiki_judgments = self._wiki()
        snapshot_map = {item["checkpoint"]: item for item in snapshots}
        trials = []
        for row in E.task_matrix(self.fixture):
            cp, task_id = row["checkpoint"], row["taskId"]
            expected = self._expected_topic(task_id, cp)
            answer = f"The answer is {expected['value']}." if expected["value"] else "The available evidence does not specify this."
            claims = []
            covered = []
            if expected["outcome"] == "supported":
                predicate = lambda s: ("operations" in s["path"] if task_id == "maintenance-day" else "policy" in s["path"])
                evidence = self._evidence(cp, predicate)
                hist = [self._evidence("2025-02-10", lambda s: "45 days" in s["text"])] if expected["temporal"] == "historical" else []
                current = [evidence] if expected["temporal"] == "current" else []
                claims.append({"quote": answer, "start": 0, "end": len(answer), "unitQuote": answer, "unitStart": 0, "unitEnd": len(answer),
                               "disposition": "supported", "temporal": expected["temporal"], "relation": "supported", "priorSupport": "unknown",
                               "qualifiers": "preserved", "explanation": "fixed synthetic annotation", "currentEvidence": current, "historicalEvidence": hist})
                covered = [row["topic"]]
            judgment = {"schemaVersion": E.ANNOTATION_SCHEMA, "answerHash": E.sha256(answer), "complete": True,
                        "outcome": expected["outcome"], "coveredTopics": covered,
                        "explanation": "fixed synthetic answer-level decision", "claims": claims}
            cp_sources = [] if row["condition"] == "wiki-only" else [s for c in self.fixture["checkpoints"] if c["id"] <= cp for s in c["sources"]]
            source_hash = E.canonical_hash([{"checkpoint": c["id"], "path": s["path"], "sha256": s["sha256"]}
                                            for c in self.fixture["checkpoints"] if c["id"] <= cp for s in c["sources"]]) if cp_sources else None
            trials.append({**{key: row[key] for key in ("id", "checkpoint", "taskId", "language", "condition")},
                           "status": "success", "answer": answer, "answerHash": E.sha256(answer), "answerJudgment": judgment,
                           "sourceHistoryHash": source_hash,
                           "wikiHash": None if row["condition"] == "sources" else snapshot_map[cp]["contentHash"],
                           "settings": settings, "unauthorizedWriteAttempts": []})
        return E.seal_artifact({"schemaVersion": E.ARTIFACT_SCHEMA, "identities": identities, "settings": settings,
                "judgeSettings": judge_settings, "wikiSnapshots": snapshots,
                "wikiJudgments": wiki_judgments, "trials": trials})

    def test_empty_artifact_retains_all_missing_cells_and_unknown_topics(self):
        settings = {"model": "openai/gpt-6.1-sol", "effort": "medium", "attemptCount": 1,
                   "taskContract": E.TASK_CONTRACT, "taskContractHash": E.canonical_hash(E.TASK_CONTRACT),
                   "readOnly": True, "limits": {"maxOutputTokens": 4096}}
        rubric = {"identity": "test-v2"}
        judge_settings = {"model": "openai/gpt-6.1-terra", "effort": "medium", "rubric": rubric,
                          "rubricHash": E.canonical_hash(rubric), "maxOutputTokens": 4096}
        artifact = E.empty_artifact(settings, judge_settings, E.sha256("candidate"), self.fixture)
        E.validate_artifact(artifact, self.fixture)
        report = E.score(artifact, self.fixture)
        self.assertIsNone(report["answerAccuracy"])
        self.assertEqual(report["missingOrUnknownCells"], 144)
        maintenance = report["maintenance"]["checkpoints"]
        self.assertEqual(len(maintenance), 4)
        self.assertTrue(all(item["accuracy"] is None and item["coverage"] == 0 for item in maintenance))
        self.assertEqual(maintenance[0]["changeAssessment"], "initialization")
        self.assertIsNone(maintenance[0]["unnecessaryChanges"])
        self.assertIsNone(maintenance[0]["unnecessaryChangeCount"], "initialization is not scored as zero unnecessary changes")
        self.assertTrue(all(len(judgment["topics"]) == 6 for judgment in artifact["wikiJudgments"]))

    def test_fixture_matrix_solver_view_and_gold_time_scope(self):
        fixture = self.fixture
        self.assertEqual(len(E.task_matrix(fixture)), 144)
        source = fixture["checkpoints"][0]["sources"][0]
        self.assertIn("\r\n", source["text"])
        self.assertIn("café 🧭", source["text"])
        self.assertNotIn("not human-reviewed", "".join(s["text"] for cp in fixture["checkpoints"] for s in cp["sources"]))
        for checkpoint in fixture["checkpoints"]:
            for source in checkpoint["sources"]:
                spans = fixture["gold"]["sourceCoordinates"][checkpoint["id"] + "/" + source["path"]]["spans"]
                for span in spans:
                    self.assertEqual(source["text"][span["start"]:span["end"]], span["quote"])
        self.assertEqual(fixture["gold"]["expectedCurrent"]["retention"]["2025-03-10"], "withdrawn; no current numeric period")
        self.assertEqual(self._expected_topic("retention-history", "2025-01-10")["outcome"], "abstained")
        view = E.solver_view("2025-01-10", "retention-history", "en", "sources", fixture=fixture)
        self.assertNotIn("maintenance", json.dumps(view))
        self.assertNotIn("evidence", json.dumps(view))
        self.assertEqual({item["checkpoint"] for item in view["sourceHistory"]}, {"2025-01-10"})
        self.assertEqual(set(view["sourceHistory"][0]), {"checkpoint", "path", "text", "sha256"})
        maintenance = E.maintenance_view("2025-01-10", "retention-current", fixture=fixture)
        self.assertIn("maintenanceTask", maintenance)
        self.assertNotIn("gold", json.dumps(maintenance))
        self.assertEqual({item["checkpoint"] for item in maintenance["sourceHistory"]}, {"2025-01-10"})
        self.assertEqual(E.solver_view("2025-04-10", "retention-current", "en", "wiki-only", {"wiki/policy.md": "x"}, fixture)["sourceHistory"], [])

    def test_all_claims_count_second_contradiction_cannot_hide_behind_supported_first(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-current" and t["checkpoint"] == "2025-03-10")
        second = copy.deepcopy(trial["answerJudgment"]["claims"][0])
        second.update(disposition="unsupported", relation="contradicted", priorSupport="unsupported",
                      currentEvidence=[self._evidence("2025-03-10", lambda s: "no longer retains" in s["text"])], historicalEvidence=[])
        trial["answerJudgment"]["claims"].append(second)
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "unsupported/unknown claim"):
            E.validate_artifact(artifact, self.fixture)
        trial["answerJudgment"]["outcome"] = "unsupported"
        artifact = E.seal_artifact(artifact)
        scored = next(a for a in E.score(artifact, self.fixture)["answers"] if a["id"] == trial["id"])
        self.assertFalse(scored["correct"])
        self.assertIn("contradicted-without-historical-support", scored["claimLabels"])

    def test_temporal_evidence_is_bound_and_prior_support_unknown_is_not_false(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-current" and t["checkpoint"] == "2025-03-10")
        claim = trial["answerJudgment"]["claims"][0]
        trial["answerJudgment"]["outcome"] = "unsupported"
        claim.update(relation="contradicted", disposition="unsupported", currentEvidence=[self._evidence("2025-03-10", lambda s: "no longer retains" in s["text"])])
        claim["historicalEvidence"] = [self._evidence("2025-03-10", lambda s: "no longer retains" in s["text"])]
        claim["priorSupport"] = "supported"
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "before the active checkpoint"):
            E.validate_artifact(artifact, self.fixture)
        claim["historicalEvidence"] = []
        claim["priorSupport"] = "unknown"
        artifact = E.seal_artifact(artifact)
        E.validate_artifact(artifact, self.fixture)
        self.assertEqual(E._answer_label(trial, self._expected_topic("retention-current", "2025-03-10")), "unsupported")
        self.assertEqual(E._claim_label(claim), "unverified")

    def test_dropped_qualifier_is_not_supported_and_historical_explanation_is_separate(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "response-qualified" and t["checkpoint"] == "2025-04-10")
        answer = "Response target: 3 days."
        trial["answer"] = answer
        trial["answerHash"] = E.sha256(answer)
        judgment = trial["answerJudgment"]
        judgment["answerHash"] = trial["answerHash"]
        claim = judgment["claims"][0]
        claim.update(quote=answer, start=0, end=len(answer), unitQuote=answer, unitStart=0, unitEnd=len(answer), qualifiers="dropped")
        artifact = E.seal_artifact(artifact)
        scored = next(row for row in E.score(artifact, self.fixture)["answers"] if row["id"] == trial["id"])
        self.assertFalse(scored["correct"])
        self.assertEqual(scored["observedOutcome"], "unsupported")

        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-current" and t["checkpoint"] == "2025-04-10")
        answer = "The current rule was withdrawn on 2025-03-10. On 2025-02-10 it was 45 days."
        trial["answer"], trial["answerHash"] = answer, E.sha256(answer)
        judgment = trial["answerJudgment"]
        judgment["answerHash"] = trial["answerHash"]
        current_quote = "The current rule was withdrawn on 2025-03-10."
        historical_quote = "On 2025-02-10 it was 45 days."
        first = judgment["claims"][0]
        first.update(quote=current_quote, start=0, end=len(current_quote), unitQuote=current_quote,
                     unitStart=0, unitEnd=len(current_quote))
        second = copy.deepcopy(first)
        start = answer.index(historical_quote)
        second.update(quote=historical_quote, start=start, end=start + len(historical_quote),
                      unitQuote=historical_quote, unitStart=start, unitEnd=start + len(historical_quote),
                      temporal="historical", historicalEvidence=[self._evidence("2025-02-10", lambda s: "45 days" in s["text"])],
                      currentEvidence=[], priorSupport="unknown")
        judgment["claims"].append(second)
        artifact = E.seal_artifact(artifact)
        scored = next(row for row in E.score(artifact, self.fixture)["answers"] if row["id"] == trial["id"])
        self.assertTrue(scored["correct"])
        self.assertEqual(scored["claimLabels"], ["supported", "historical"])

        only_historical = self.artifact()
        current_trial = next(t for t in only_historical["trials"] if t["taskId"] == "retention-current" and t["checkpoint"] == "2025-02-10")
        old_claim = copy.deepcopy(current_trial["answerJudgment"]["claims"][0])
        answer = "45 days was previously stated."
        old_claim.update(quote=answer, start=0, end=len(answer), unitQuote=answer, unitStart=0, unitEnd=len(answer),
                         temporal="historical", currentEvidence=[], historicalEvidence=[self._evidence("2025-02-10", lambda s: "45 days" in s["text"])])
        current_trial.update(answer=answer, answerHash=E.sha256(answer))
        current_trial["answerJudgment"].update(answerHash=current_trial["answerHash"], claims=[old_claim])
        only_historical = E.seal_artifact(only_historical)
        scored = next(row for row in E.score(only_historical, self.fixture)["answers"] if row["id"] == current_trial["id"])
        self.assertIsNone(scored["correct"])

    def test_historical_evidence_cannot_come_from_checkpoint_after_trial(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-history" and t["checkpoint"] == "2025-01-10")
        answer = "45 days."
        source = next(t for t in self.artifact()["trials"] if t["taskId"] == "retention-history" and t["checkpoint"] == "2025-02-10")
        claim = copy.deepcopy(source["answerJudgment"]["claims"][0])
        claim.update(quote=answer, start=0, end=len(answer), unitQuote=answer, unitStart=0, unitEnd=len(answer),
                     historicalEvidence=[self._evidence("2025-02-10", lambda s: "45 days" in s["text"])])
        trial["answer"], trial["answerHash"] = answer, E.sha256(answer)
        trial["answerJudgment"] = {"schemaVersion": E.ANNOTATION_SCHEMA, "answerHash": trial["answerHash"],
                                   "complete": True, "outcome": "supported", "coveredTopics": ["retention-history"],
                                   "explanation": "future source intentionally injected", "claims": [claim]}
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "future checkpoint"):
            E.validate_artifact(artifact, self.fixture)

    def test_fabricated_numeric_candidate_is_byte_bound_and_scored_as_failure(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-current" and t["checkpoint"] == "2025-03-10")
        answer = "Acorn retains requests for 90 days."
        trial["answer"], trial["answerHash"] = answer, E.sha256(answer)
        judgment = trial["answerJudgment"]
        judgment.update(answerHash=trial["answerHash"], outcome="unsupported")
        claim = judgment["claims"][0]
        claim.update(quote=answer, start=0, end=len(answer), unitQuote=answer, unitStart=0, unitEnd=len(answer),
                     disposition="unsupported", relation="contradicted", priorSupport="unsupported",
                     currentEvidence=[self._evidence("2025-03-10", lambda s: "no longer retains" in s["text"])], historicalEvidence=[])
        artifact = E.seal_artifact(artifact)
        scored = next(row for row in E.score(artifact, self.fixture)["answers"] if row["id"] == trial["id"])
        self.assertFalse(scored["correct"])
        self.assertEqual(scored["claimLabels"], ["contradicted-without-historical-support"])

    def test_stale_claim_needs_both_active_contradiction_and_established_prior_support(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-current" and t["checkpoint"] == "2025-04-10")
        claim = trial["answerJudgment"]["claims"][0]
        claim.update(disposition="unsupported", relation="contradicted", priorSupport="supported",
                     currentEvidence=[self._evidence("2025-04-10", lambda s: "no longer retains" in s["text"])],
                     historicalEvidence=[self._evidence("2025-02-10", lambda s: "45 days" in s["text"])])
        trial["answerJudgment"]["outcome"] = "unsupported"
        artifact = E.seal_artifact(artifact)
        result = E.score(artifact, self.fixture)
        scored = next(a for a in result["answers"] if a["id"] == trial["id"])
        self.assertEqual(scored["claimLabels"], ["stale"])
        self.assertFalse(scored["correct"])
        claim["historicalEvidence"] = [self._evidence("2025-04-10", lambda s: "no longer retains" in s["text"])]
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "before the active checkpoint"):
            E.validate_artifact(artifact, self.fixture)

    def test_historical_answer_is_credited_only_with_historical_support_and_no_current_substitution(self):
        artifact = self.artifact()
        trial = next(t for t in artifact["trials"] if t["taskId"] == "retention-history" and t["checkpoint"] == "2025-02-10")
        self.assertTrue(next(a for a in E.score(artifact, self.fixture)["answers"] if a["id"] == trial["id"])["correct"])
        claim = trial["answerJudgment"]["claims"][0]
        claim["currentEvidence"] = [self._evidence("2025-02-10", lambda s: "45 days" in s["text"])]
        claim["historicalEvidence"] = []
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "current evidence must support"):
            E.validate_artifact(artifact, self.fixture)
        trial["answerJudgment"]["claims"][0]["currentEvidence"] = []
        trial["answerJudgment"]["claims"][0]["historicalEvidence"] = [self._evidence("2025-02-10", lambda s: "45 days" in s["text"])]
        trial["answerJudgment"]["claims"][0]["temporal"] = "unknown"
        artifact = E.seal_artifact(artifact)
        self.assertIsNone(E._answer_label(trial, self._expected_topic("retention-history", "2025-02-10")))

    def test_correct_abstention_omissions_empty_accuracy_and_bad_spans(self):
        artifact = self.artifact()
        result = E.score(artifact, self.fixture)
        self.assertEqual(result["answerAccuracy"], 1.0)
        self.assertTrue(next(a for a in result["answers"] if a["taskId"] == "rehearsal-date")["correct"])
        partial = copy.deepcopy(artifact)
        omitted = next(t for t in partial["trials"] if t["taskId"] == "retention-current" and t["condition"] == "sources")
        omitted["answerJudgment"].update(outcome="unknown", claims=[], coveredTopics=[])
        partial = E.seal_artifact(partial)
        result = E.score(partial, self.fixture)
        self.assertEqual(result["answerAccuracy"], 1.0)
        self.assertEqual(result["scoredCells"], 143)
        self.assertEqual(result["missingOrUnknownCells"], 1)
        missing = copy.deepcopy(artifact)
        for trial in missing["trials"]:
            trial.update(status="missing", answer=None, answerHash=None, answerJudgment=None)
        missing = E.seal_artifact(missing)
        result = E.score(missing, self.fixture)
        self.assertIsNone(result["answerAccuracy"])
        self.assertEqual(result["missingOrUnknownCells"], 144)
        empty = self.artifact()
        trial = next(t for t in empty["trials"] if t["taskId"] == "rehearsal-date")
        trial.update(answer="", answerHash=E.sha256(""))
        trial["answerJudgment"].update(answerHash=trial["answerHash"], outcome="abstained", claims=[], coveredTopics=[])
        empty = E.seal_artifact(empty)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            E.validate_artifact(empty, self.fixture)
        bad = self.artifact()
        bad["trials"][0]["answerJudgment"]["claims"][0]["quote"] = "invented"
        bad = E.seal_artifact(bad)
        with self.assertRaisesRegex(ValueError, "candidate quote"):
            E.validate_artifact(bad, self.fixture)
        bad = self.artifact()
        bad["trials"] = []
        bad = E.seal_artifact(bad)
        with self.assertRaisesRegex(ValueError, "144"):
            E.validate_artifact(bad, self.fixture)

    def test_maintenance_stale_requires_active_contradiction_and_dated_prior_support(self):
        artifact = self.artifact()
        topic = next(item for item in artifact["wikiJudgments"][3]["topics"] if item["topic"] == "retention")
        topic.update(status="unsupported", temporal="current", relation="contradicted", priorSupport="supported",
                     currentEvidence=[], historicalEvidence=[self._evidence("2025-02-10", lambda source: "45 days" in source["text"])])
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "wiki current contradiction requires active evidence"):
            E.validate_artifact(artifact, self.fixture)
        topic["currentEvidence"] = [self._evidence("2025-04-10", lambda source: "no longer retains" in source["text"])]
        topic["historicalEvidence"] = []
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "wiki established prior support requires dated evidence"):
            E.validate_artifact(artifact, self.fixture)
        topic["historicalEvidence"] = [self._evidence("2025-02-10", lambda source: "45 days" in source["text"])]
        artifact = E.seal_artifact(artifact)
        E.validate_artifact(artifact, self.fixture)
        self.assertEqual(E._wiki_topic_label(topic), "stale")

    def test_maintenance_stale_candidate_is_byte_bound_and_diagnostic(self):
        artifact = self.artifact()
        cp = artifact["wikiSnapshots"][3]
        page = next(item for item in cp["pages"] if item["path"] == "wiki/policy.md")
        stale_text = "Acorn retains requests for 45 days."
        content = page["content"] + stale_text + "\n"
        page.update(content=content, sha256=E.sha256(content))
        cp["contentHash"] = E.canonical_hash([{"path": p["path"], "sha256": p["sha256"]} for p in sorted(cp["pages"], key=lambda p: p["path"])])
        judgment = artifact["wikiJudgments"][3]
        judgment["snapshotHash"] = cp["contentHash"]
        topic = next(item for item in judgment["topics"] if item["topic"] == "retention")
        start = content.index(stale_text)
        topic.update(status="unsupported", path="wiki/policy.md", pageHash=page["sha256"], quote=stale_text, start=start, end=start + len(stale_text),
                     temporal="current", relation="contradicted", priorSupport="supported",
                     currentEvidence=[self._evidence("2025-04-10", lambda s: "no longer retains" in s["text"])],
                     historicalEvidence=[self._evidence("2025-02-10", lambda s: "45 days" in s["text"])])
        for item in judgment["topics"]:
            if item.get("path") == "wiki/policy.md":
                item["pageHash"] = page["sha256"]
        for trial in artifact["trials"]:
            if trial["checkpoint"] == cp["checkpoint"] and trial["condition"] != "sources":
                trial["wikiHash"] = cp["contentHash"]
        artifact = E.seal_artifact(artifact)
        report = E.score_maintenance(artifact, self.fixture)["checkpoints"][3]
        self.assertEqual(report["topicLabels"]["retention"], "stale")
        self.assertLess(report["accuracy"], 1.0)

    def test_decided_wiki_annotations_require_bytes_and_failed_snapshots_cannot_score(self):
        artifact = self.artifact()
        topic = next(t for t in artifact["wikiJudgments"][0]["topics"] if t["topic"] == "retention")
        topic.update(status="unsupported", path=None, pageHash=None, quote="invented unsupported claim", start=0, end=25)
        artifact = E.seal_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "bound snapshot page"):
            E.validate_artifact(artifact, self.fixture)

        failed = self.artifact()
        failed["wikiSnapshots"][0]["status"] = "failed"
        failed = E.seal_artifact(failed)
        with self.assertRaisesRegex(ValueError, "incomplete unknown-only"):
            E.validate_artifact(failed, self.fixture)

    def test_maintenance_scoring_canonical_reuse_duplication_churn_history_and_omission(self):
        artifact = self.artifact()
        result = E.score_maintenance(artifact, self.fixture)
        self.assertEqual(result["checkpoints"][0]["coverage"], 1.0)
        self.assertEqual(result["checkpoints"][0]["accuracy"], 1.0)
        self.assertEqual(result["checkpoints"][1]["historyPreserved"]["retention-history"], "preserved")
        incomplete_topic = copy.deepcopy(artifact)
        row = next(t for t in incomplete_topic["wikiJudgments"][0]["topics"] if t["topic"] == "retention")
        row["qualifiers"] = "dropped"
        incomplete_topic = E.seal_artifact(incomplete_topic)
        degraded = E.score_maintenance(incomplete_topic, self.fixture)["checkpoints"][0]
        self.assertEqual(degraded["topicLabels"]["retention"], "unverified")
        self.assertEqual(degraded["accuracy"], 1.0)
        self.assertEqual(degraded["scoredTopics"], 5)
        self.assertEqual(degraded["unknownOrOmitted"], 1)
        self.assertEqual(degraded["droppedQualifiers"], ["retention"])
        # A second page labeled for the same topic is mechanically counted as duplicate.
        cp = artifact["wikiSnapshots"][2]
        duplicate_content = "---\ntype: note\n---\nDuplicate retention page.\n"
        cp["pages"].append({"path": "wiki/retention-copy.md", "content": duplicate_content, "sha256": E.sha256(duplicate_content)})
        cp["contentHash"] = E.canonical_hash([{"path": p["path"], "sha256": p["sha256"]} for p in sorted(cp["pages"], key=lambda p: p["path"])])
        artifact["wikiJudgments"][2]["snapshotHash"] = cp["contentHash"]
        artifact["wikiJudgments"][3]["previousSnapshotHash"] = cp["contentHash"]
        artifact["wikiJudgments"][2]["pageTopics"].append({"path": "wiki/retention-copy.md", "topics": ["retention"]})
        for trial in artifact["trials"]:
            if trial["checkpoint"] == cp["checkpoint"] and trial["condition"] != "sources":
                trial["wikiHash"] = cp["contentHash"]
        artifact = E.seal_artifact(artifact)
        report = E.score_maintenance(artifact, self.fixture)["checkpoints"][2]
        self.assertEqual(report["duplicateTopicPages"]["retention"], ["wiki/policy.md", "wiki/retention-copy.md"])
        self.assertEqual(report["changeAssessment"], "assessed")
        self.assertEqual(report["unnecessaryChangeCount"], 0)
        self.assertIn("wiki/retention-copy.md", report["changedOrRemovedPaths"])
        duplicate_topic = self.artifact()
        duplicate_topic["wikiJudgments"][0]["pageTopics"][0]["topics"].append("retention")
        duplicate_topic = E.seal_artifact(duplicate_topic)
        with self.assertRaisesRegex(ValueError, "invalid page topic inventory"):
            E.validate_artifact(duplicate_topic, self.fixture)
        duplicate_path = self.artifact()
        duplicate_path["wikiJudgments"][0]["pageTopics"].append(copy.deepcopy(duplicate_path["wikiJudgments"][0]["pageTopics"][0]))
        duplicate_path = E.seal_artifact(duplicate_path)
        with self.assertRaisesRegex(ValueError, "duplicate page path"):
            E.validate_artifact(duplicate_path, self.fixture)
        incomplete = copy.deepcopy(artifact)
        incomplete["wikiJudgments"][0]["complete"] = False
        incomplete = E.seal_artifact(incomplete)
        self.assertIsNone(E.score_maintenance(incomplete, self.fixture)["checkpoints"][0]["accuracy"])

    def test_answer_citation_inventory_resolves_only_applicable_frozen_local_targets(self):
        artifact = self.artifact()
        trial = next(item for item in artifact["trials"] if item["taskId"] == "rehearsal-date" and item["checkpoint"] == "2025-01-10" and item["condition"] == "sources+wiki")
        answer = "🧭 I cannot determine that. [Policy](sources/policy.md) [[wiki/policy.md|policy]] [wiki/missing.md] [2025-04-10/sources/policy.md]"
        trial["answer"] = answer
        trial["answerHash"] = E.sha256(answer)
        trial["answerJudgment"]["answerHash"] = trial["answerHash"]
        artifact = E.seal_artifact(artifact)
        report = E.score(artifact, self.fixture)
        detail = next(item for item in report["answers"] if item["id"] == trial["id"])
        citations = detail["answerCitations"]
        self.assertEqual([item["resolution"] for item in citations], ["resolved", "resolved", "missing", "future"])
        self.assertEqual(detail["unresolvedAnswerCitations"], 2)
        self.assertEqual(detail["unresolvedJudgeEvidenceClaims"], 0)
        self.assertTrue(all(answer[item["start"]:item["end"]] == item["quote"] for item in citations))
        self.assertTrue(all(answer[item["start"]:item["end"]].startswith(("[", "[[")) for item in citations))
        self.assertIsNotNone(detail["correct"], "citation inventory does not turn answer-citation absence into a quality-false result")
        uncited = next(item for item in report["answers"] if item["id"] != trial["id"] and item["answerCitations"] == [])
        self.assertEqual(uncited["unresolvedAnswerCitations"], 0)
        self.assertIn("syntactic inventory", report["citationInventoryNote"])
        self.assertNotIn("unresolvedCitations", detail)

    def test_unnecessary_change_can_identify_deleted_prior_page_but_not_unknown_path(self):
        artifact = self.artifact()
        index = 1
        previous = artifact["wikiSnapshots"][index - 1]
        background = "---\ntype: note\ntitle: Background\n---\nUseful historical background.\n"
        deleted = {"path": "wiki/background.md", "content": background, "sha256": E.sha256(background)}
        previous["pages"].append(deleted)
        previous["contentHash"] = E.canonical_hash([{"path": page["path"], "sha256": page["sha256"]}
                                                     for page in sorted(previous["pages"], key=lambda page: page["path"])])
        prior_judgment = artifact["wikiJudgments"][index - 1]
        prior_judgment["snapshotHash"] = previous["contentHash"]
        prior_judgment["pageTopics"].append({"path": deleted["path"], "topics": []})
        for trial in artifact["trials"]:
            if trial["checkpoint"] == previous["checkpoint"] and trial["condition"] != "sources":
                trial["wikiHash"] = previous["contentHash"]
        judgment = artifact["wikiJudgments"][index]
        judgment["previousSnapshotHash"] = previous["contentHash"]
        judgment["unnecessaryChanges"] = [{"path": deleted["path"], "reason": "Removed useful background context."}]
        artifact = E.seal_artifact(artifact)
        E.validate_artifact(artifact, self.fixture)
        scored = E.score_maintenance(artifact, self.fixture)["checkpoints"][index]
        self.assertEqual(scored["unnecessaryChanges"], [{"path": deleted["path"], "reason": "Removed useful background context."}])
        self.assertIn(deleted["path"], scored["changedOrRemovedPaths"])

        unknown = copy.deepcopy(artifact)
        unknown["wikiJudgments"][index]["unnecessaryChanges"] = [{"path": "wiki/never-existed.md", "reason": "Unknown path."}]
        unknown = E.seal_artifact(unknown)
        with self.assertRaisesRegex(ValueError, "invalid unnecessary-change judgment"):
            E.validate_artifact(unknown, self.fixture)

    def test_transition_assessment_unknown_is_not_scored_as_zero_churn(self):
        artifact = self.artifact()
        first = artifact["wikiJudgments"][0]
        self.assertEqual(first["changeAssessment"], "initialization")
        self.assertIsNone(first["unnecessaryChanges"])
        bad_initialization = copy.deepcopy(artifact)
        bad_initialization["wikiJudgments"][0]["unnecessaryChanges"] = []
        bad_initialization = E.seal_artifact(bad_initialization)
        with self.assertRaisesRegex(ValueError, "initialization cannot claim"):
            E.validate_artifact(bad_initialization, self.fixture)
        unknown = copy.deepcopy(artifact)
        unknown["wikiJudgments"][1].update(changeAssessment="unknown", unnecessaryChanges=None)
        unknown = E.seal_artifact(unknown)
        E.validate_artifact(unknown, self.fixture)
        scored = E.score_maintenance(unknown, self.fixture)["checkpoints"][1]
        self.assertEqual(scored["changeAssessment"], "unknown")
        self.assertIsNone(scored["unnecessaryChanges"])
        self.assertIsNone(scored["unnecessaryChangeCount"])
        bad_unknown = copy.deepcopy(unknown)
        bad_unknown["wikiJudgments"][1]["unnecessaryChanges"] = []
        bad_unknown = E.seal_artifact(bad_unknown)
        with self.assertRaisesRegex(ValueError, "unknown transition assessment"):
            E.validate_artifact(bad_unknown, self.fixture)
        bad_incomplete = copy.deepcopy(artifact)
        bad_incomplete["wikiJudgments"][1]["complete"] = False
        bad_incomplete = E.seal_artifact(bad_incomplete)
        with self.assertRaisesRegex(ValueError, "assessed transition requires a complete judgment"):
            E.validate_artifact(bad_incomplete, self.fixture)
        bad_binding = copy.deepcopy(artifact)
        bad_binding["wikiJudgments"][1]["previousSnapshotHash"] = E.sha256("wrong previous")
        bad_binding = E.seal_artifact(bad_binding)
        with self.assertRaisesRegex(ValueError, "previous snapshot binding"):
            E.validate_artifact(bad_binding, self.fixture)

    def test_full_identity_settings_bindings_and_compatibility(self):
        left = self.artifact()
        E.validate_artifact(left, self.fixture)
        right = copy.deepcopy(left)
        right["identities"] = E.identity_manifest(self.fixture, right["settings"], E.sha256("other-system"), right["judgeSettings"])
        right = E.seal_artifact(right)
        E.compare_compatibility(left, right, self.fixture)
        different_judge = copy.deepcopy(left)
        different_judge["judgeSettings"]["effort"] = "high"
        different_judge["identities"] = E.identity_manifest(self.fixture, different_judge["settings"],
            different_judge["identities"]["candidateSystemHash"], different_judge["judgeSettings"])
        different_judge = E.seal_artifact(different_judge)
        with self.assertRaisesRegex(ValueError, "not comparable"):
            E.compare_compatibility(left, different_judge, self.fixture)
        bad = copy.deepcopy(left)
        bad["identities"]["goldHash"] = "arbitrary"
        bad = E.seal_artifact(bad)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            E.validate_artifact(bad, self.fixture)
        bad = copy.deepcopy(left)
        bad["trials"][1]["settings"] = {**bad["settings"], "effort": "high"}
        bad = E.seal_artifact(bad)
        with self.assertRaisesRegex(ValueError, "settings drift"):
            E.validate_artifact(bad, self.fixture)
        bad = copy.deepcopy(left)
        bad["judgeSettings"]["rubricHash"] = "0" * 64
        bad["identities"] = E.identity_manifest(self.fixture, bad["settings"], bad["identities"]["candidateSystemHash"],
                                                {**bad["judgeSettings"], "rubricHash": E.canonical_hash(bad["judgeSettings"]["rubric"])})
        bad = E.seal_artifact(bad)
        with self.assertRaisesRegex(ValueError, "rubric hash"):
            E.validate_artifact(bad, self.fixture)
        bad = copy.deepcopy(left)
        bad["trials"].pop()
        bad = E.seal_artifact(bad)
        with self.assertRaisesRegex(ValueError, "144"):
            E.compare_compatibility(left, bad, self.fixture)


if __name__ == "__main__":
    unittest.main()
