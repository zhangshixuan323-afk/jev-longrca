"""Offline behavioral tests; no paid API requests or downloaded datasets required."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate as baseline
import evaluate_jev_v2 as runner
import jev_v2 as v2


def history(n):
    return [{"step": i, "name": "Planner (-> Worker)" if i == 0 else "Worker",
             "role": "assistant", "content": "Original logged evidence for step %d. 汉字" % i,
             "mistake_reason": "SECRET_GOLD"} for i in range(n)]


def answer(ids, conf=0.1):
    ordered = sorted(ids, reverse=True)
    total = len(ids) * (len(ids) + 1) / 2
    return {"type": "choice", "choice": str(ordered[0]), "confidence": conf,
            "probabilities": {str(s): (len(ids) - i) / total for i, s in enumerate(ordered)}}


def response_for(payload, conf=0.1):
    answers = {}
    for name, question in payload["questions"].items():
        options = list(question["criteria"])
        if name == "root_step":
            answers[name] = answer([int(s) for s in options], conf)
        else:
            selected = options[0]
            answers[name] = {"type": "choice", "choice": selected, "confidence": 0.8,
                             "probabilities": {s: float(s == selected) for s in options}}
    return {"model": v2.MODEL, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 10}}


class FakeClient:
    def __init__(self, conf=0.1):
        self.conf, self.calls = conf, []

    def call(self, state, questions, tag):
        payload = {"state": state, "questions": questions}
        result = response_for(payload, self.conf)
        self.calls.append(copy.deepcopy({"tag": tag, "request": payload, "response": result}))
        return result


class InferenceTests(unittest.TestCase):
    def test_original_role_prompt_and_common_rules_preserved(self):
        original = baseline.questions([0], ["Planner"])["responsible_role"]
        for stage in ("direct", "final"):
            q = v2.questions([0], stage, ["Planner"])
            self.assertEqual(q["responsible_role"]["criteria"], original["criteria"])
            self.assertEqual(q["responsible_role"]["instructions"],
                             original["instructions"] + " " + v2.ROLE_APPENDIX)
        for stage in v2.STAGE_INSTRUCTIONS:
            q = v2.questions([1, 2], stage)
            self.assertTrue(q["root_step"]["instructions"].startswith(baseline.RULES))
            self.assertIn(v2.STAGE_INSTRUCTIONS[stage], q["root_step"]["instructions"])
            self.assertEqual(set(q), {"root_step"})
        self.assertEqual(len(set(v2.STAGE_INSTRUCTIONS.values())), 4)
        with self.assertRaises(ValueError):
            v2.questions([1], "recall", ["Planner"])

    def test_confidence_boundaries_and_invalid_values(self):
        ids, cfg = list(range(8)), v2.settings()
        for confidence, expected in [(0, 6), (0.2999, 6), (0.3, 4), (0.6999, 4), (0.7, 3), (1, 3),
                                     (None, 6), ("0.8", 6), (True, 6), (-1, 6), (1.1, 6),
                                     (float("nan"), 6), (float("inf"), 6)]:
            for stage in ("recall", "reduce"):
                with self.subTest(confidence=confidence, stage=stage):
                    result = v2.retain(answer(ids, confidence), ids, stage, cfg)
                    self.assertEqual(result["actual_keep"], expected)
                    self.assertEqual(result["kept_steps"][0], 7)
                    if not result["confidence_valid"]:
                        self.assertIsNone(result["question_confidence"])
                        self.assertEqual(result["confidence_tier"], "low")
                        self.assertIn("invalid", result["retention_reason"])
        a = answer(ids)
        del a["confidence"]
        self.assertEqual(v2.retain(a, ids, "recall", cfg)["actual_keep"], 6)

    def test_small_groups_and_choice_first_tie_break(self):
        cfg = v2.settings()
        self.assertEqual(v2.retain(answer([0, 1]), [0, 1], "recall", cfg)["kept_steps"], [1, 0])
        for n in range(4, 9):
            event = v2.retain(answer(list(range(n))), list(range(n)), "reduce", cfg)
            self.assertEqual(event["actual_keep"], min(6, n - 1))
        a = {"choice": "3", "confidence": 0.8, "probabilities": {str(i): 0.25 for i in range(4)}}
        self.assertEqual(v2.retain(a, [0, 1, 2, 3], "recall", cfg)["kept_steps"], [3, 0, 1])
        for bad in ({"choice": "99", "probabilities": {}},
                    {"choice": "0", "probabilities": {"0": float("nan")}},
                    {"choice": "0", "probabilities": {}}):
            with self.assertRaises(ValueError):
                v2.ranked_steps(bad, [0])

    def test_policies_independent_and_do_not_mutate_baseline(self):
        previous = copy.deepcopy(baseline.CONFIG)
        cfg = v2.settings({"recall": {"low_threshold": 0.1}})
        self.assertEqual(v2.retain(answer(list(range(8)), 0.2), list(range(8)), "recall", cfg)["actual_keep"], 4)
        self.assertEqual(v2.retain(answer(list(range(8)), 0.2), list(range(8)), "reduce", cfg)["actual_keep"], 6)
        v2.predict(history(3), FakeClient(), cfg)
        self.assertEqual(baseline.CONFIG, previous)
        for overrides in ({"chunk_bytes": 100}, {"recall": {"low_threshold": 0.9}},
                          {"reduce": {"high_keep": 6}}, {"reduce_group_size": 3},
                          {"final_candidate_limit": 9}):
            with self.assertRaises(ValueError):
                v2.settings(overrides)

    def test_multiround_low_confidence_converges_with_prior_only_history(self):
        client = FakeClient()
        h = history(280)  # Forces the unchanged segmented path without large fixture files.
        prediction = v2.predict(h, client)
        self.assertEqual(prediction["method"], "segmented_choice")
        self.assertLessEqual(len(prediction["final_candidates"]), 8)
        self.assertTrue(any(e["stage"] == "reduce_bypass" for e in prediction["selection_trace"]))
        self.assertGreater(len({c["tag"].split("_")[1] for c in client.calls if c["tag"].startswith("reduce_")}), 1)
        seen = {}
        for call in client.calls:
            request, tag = call["request"], call["tag"]
            self.assertNotIn("SECRET_GOLD", baseline.dumps(request))
            self.assertNotIn("mistake_", baseline.dumps(request))
            ids = {int(s) for s in request["questions"]["root_step"]["criteria"]}
            if tag.startswith("recall_"):
                self.assertNotIn("selection_history", request["state"])
                self.assertEqual(set(request["questions"]), {"root_step"})
            else:
                self.assertLessEqual(len(ids), 8)
                summary = request["state"]["selection_history"]
                self.assertEqual({c["step"] for c in summary["candidates"]}, ids)
                self.assertNotIn("probabilities", baseline.dumps(summary))
                for previous_tag, record in summary["calls"].items():
                    self.assertIn(previous_tag, seen)
                    self.assertTrue(set(map(int, record["candidate_ranks"])) <= ids)
                    source = seen[previous_tag]
                    self.assertEqual(record["question_confidence"], source["response"]["answers"]["root_step"]["confidence"])
                    self.assertEqual(record["option_count"], len(source["request"]["questions"]["root_step"]["criteria"]))
                for candidate in summary["candidates"]:
                    refs = [k for k in ("first_recall", "latest_reduction") if k in candidate]
                    self.assertLessEqual(len(refs), 2)
            seen[tag] = call
        self.assertEqual(set(client.calls[-1]["request"]["questions"]), {"root_step", "responsible_role"})
        self.assertEqual(prediction["predicted_role"], "Planner")
        self.assertEqual(h[prediction["predicted_step"]]["name"], "Worker")
        self.assertEqual(prediction["step_confidence"], 0.1)
        self.assertEqual(prediction["role_confidence"], 0.8)

    def test_handoff_dedup_first_recall_and_latest_reduction(self):
        ledger = v2.SelectionHistory()
        for tag, stage in [("recall_000", "recall"), ("recall_001", "recall"),
                           ("reduce_00_000", "reduce"), ("reduce_01_000", "reduce")]:
            event = dict(v2.retain(answer([1, 2, 3, 4]), [1, 2, 3, 4], stage, v2.settings()), tag=tag)
            ledger.observe(event)
        ledger.handoff(0, 4)
        summary = ledger.summary([0, 3, 4])
        self.assertEqual(summary["candidates"][0], {"step": 0, "origin": "handoff_expansion", "source_step": 4})
        self.assertEqual(set(summary["calls"]), {"recall_000", "reduce_01_000"})
        for record in summary["calls"].values():
            self.assertEqual(set(record["candidate_ranks"]), {"3", "4"})
        self.assertEqual(summary["candidates"][1]["first_recall"], "recall_000")

    def test_direct_and_segmented_without_reduction(self):
        direct = FakeClient()
        v2.predict(history(3), direct)
        self.assertEqual([c["tag"] for c in direct.calls], ["direct"])
        self.assertNotIn("selection_history", direct.calls[0]["request"]["state"])
        h = history(4)
        for row in h:
            row["content"] = "x" * 12000
        segmented = FakeClient()
        result = v2.predict(h, segmented)
        self.assertEqual(result["method"], "segmented_choice")
        self.assertFalse(any(c["tag"].startswith("reduce_") for c in segmented.calls))
        self.assertIn("selection_history", segmented.calls[-1]["request"]["state"])
        rebuilt = {}
        for c in segmented.calls[:-1]:
            for record in c["request"]["state"]["segment"]:
                rebuilt[record["step"]] = rebuilt.get(record["step"], "") + record["content"]
        self.assertEqual(rebuilt, {r["step"]: r["content"] for r in h})

    def test_previous_low_confidence_example_keeps_step_26(self):
        a = {"choice": "6", "confidence": 0.15,
             "probabilities": {"35": 0.18, "17": 0.01, "6": 0.26, "26": 0.05,
                               "24": 0.03, "36": 0.22, "4": 0.05, "2": 0.20}}
        result = v2.retain(a, [2, 4, 6, 17, 24, 26, 35, 36], "reduce", v2.settings())
        self.assertEqual(result["kept_steps"], [6, 36, 2, 35, 4, 26])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "results/jev_v2_mini"
        self.row = {"question_ID": "case_001", "source": "example", "history": history(3),
                    "mistake_agent": "Worker", "mistake_step": 2, "mistake_reason": "SECRET_GOLD"}
        self.manifest = {"revision": "test-revision", "files": []}
        self.cfg = v2.settings()
        self.root_patch = patch.object(runner, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    @staticmethod
    def opener(request, timeout):
        payload = json.loads(request.data)
        return io.BytesIO(json.dumps(response_for(payload)).encode())

    def prepare(self):
        frozen = runner.run_config([self.row], self.manifest, "mini", False, self.cfg)
        runner.prepare(self.out, frozen)
        return runner.digest(frozen), runner.RunControl(self.out)

    def test_end_to_end_cache_replay_and_tampering_rejected(self):
        fingerprint, control = self.prepare()
        with patch.object(runner.urllib.request, "urlopen", side_effect=self.opener) as network:
            first = runner.process_row(self.row, "private-key", self.out, control, self.cfg, fingerprint)
            second = runner.process_row(self.row, None, self.out, control, self.cfg, fingerprint)
            self.assertEqual(first, second)
            self.assertEqual(network.call_count, 1)
        self.assertEqual(first["status"], "ok")
        self.assertTrue((self.out / "decisions/case_001/direct.json").exists())
        call = self.out / "calls/case_001/direct.json"
        self.assertNotIn("private-key", call.read_text(encoding="utf-8"))
        saved = runner.read_json(call)
        saved["request"]["state"]["known_outcome"] = "tampered"
        runner.atomic_json(call, saved)
        with patch.object(runner.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "Cached request"):
                runner.process_row(self.row, None, self.out, control, self.cfg, fingerprint)
            network.assert_not_called()

    def test_missing_call_and_wrong_prediction_provenance_fail_offline(self):
        fingerprint, control = self.prepare()
        with patch.object(runner.urllib.request, "urlopen", side_effect=self.opener):
            runner.process_row(self.row, "private-key", self.out, control, self.cfg, fingerprint)
        path = self.out / "predictions/case_001.json"
        saved = runner.read_json(path)
        saved["config_sha256"] = "v1"
        runner.atomic_json(path, saved)
        with patch.object(runner.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "provenance"):
                runner.process_row(self.row, None, self.out, control, self.cfg, fingerprint)
            saved["config_sha256"] = fingerprint
            runner.atomic_json(path, saved)
            (self.out / "calls/case_001/direct.json").unlink()
            with self.assertRaisesRegex(ValueError, "missing its original call"):
                runner.process_row(self.row, None, self.out, control, self.cfg, fingerprint)
            network.assert_not_called()

    def test_output_and_configuration_isolation(self):
        frozen = runner.run_config([self.row], self.manifest, "mini", False, self.cfg)
        with self.assertRaises(ValueError):
            runner.prepare(self.root / "results/full", frozen)
        unknown = self.root / "unknown"
        unknown.mkdir()
        (unknown / "old.txt").write_text("baseline")
        with self.assertRaises(ValueError):
            runner.prepare(unknown, frozen)
        runner.prepare(self.out, frozen)
        changed = copy.deepcopy(frozen)
        changed["inference"]["recall"]["low_keep"] = 7
        with self.assertRaises(ValueError):
            runner.prepare(self.out, changed)
        self.assertEqual(runner.read_json(self.out / "config.json"), frozen)

    def test_invalid_confidence_survives_raw_archive_and_replays_as_null(self):
        fingerprint, control = self.prepare()
        def malformed_confidence(request, timeout):
            result = response_for(json.loads(request.data), float("nan"))
            return io.BytesIO(json.dumps(result).encode())
        with patch.object(runner.urllib.request, "urlopen", side_effect=malformed_confidence) as network:
            prediction = runner.process_row(self.row, "private-key", self.out, control, self.cfg, fingerprint)
            self.assertEqual(prediction["status"], "ok")
            self.assertIsNone(prediction["step_confidence"])
            self.assertEqual(runner.process_row(self.row, None, self.out, control, self.cfg, fingerprint), prediction)
            self.assertEqual(network.call_count, 1)

    def test_baseline_comparison_only_compares_whole_subset_usage(self):
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "phase1_predictions.csv").write_text(
            "question_ID,reference_role,reference_step,predicted_role,predicted_step\n"
            "case_001,Worker,2,Worker,1\n", encoding="utf-8")
        runner.atomic_json(reports / "phase1_metrics.json", {
            "dataset_revision": "test-revision", "successful_api_calls": 2, "input_tokens": 200})
        predictions = {"case_001": {"status": "ok", "predicted_role": "Planner", "predicted_step": 2}}
        result = runner.baseline_comparison([self.row], predictions, "mini",
                                           {"successful_api_calls": 3, "input_tokens": 250}, "test-revision")
        self.assertEqual(result["baseline"]["step_exact"], 0)
        self.assertEqual(result["v2"]["step_exact"], 1)
        self.assertEqual(result["whole_subset_usage"]["input_tokens"]["delta"], 50)
        partial = runner.baseline_comparison([self.row], {}, "mini", {}, "test-revision")
        self.assertNotIn("whole_subset_usage", partial)

    def test_balance_stop_blocks_other_clients_and_retries(self):
        fingerprint, control = self.prepare()
        clients = [runner.Client("private-key", self.out / "calls" / name, control, fingerprint)
                   for name in ("one", "two")]
        error = urllib.error.HTTPError("https://example.invalid", 402, "Payment Required", {},
                                       io.BytesIO(b'{"error":"insufficient_balance"}'))
        with patch.object(runner.urllib.request, "urlopen", side_effect=error) as network:
            for client in clients:
                with self.assertRaises(runner.BillingStop):
                    client.call({}, v2.questions([0], "direct", ["Worker"]), "direct")
            self.assertEqual(network.call_count, 1)
        self.assertTrue(control.stop.is_set())
        self.assertEqual(runner.read_json(self.out / "balance_stop.json")["http_status"], 402)

    def test_transient_retry_then_success_and_cancelled_backoff(self):
        fingerprint, control = self.prepare()
        client = runner.Client("private-key", self.out / "calls/one", control, fingerprint)
        q = v2.questions([0], "direct", ["Worker"])
        result = response_for({"questions": q})
        error = urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, io.BytesIO(b"retry"))
        with patch.object(runner.urllib.request, "urlopen", side_effect=[error, io.BytesIO(json.dumps(result).encode())]), \
                patch.object(control.stop, "wait", return_value=False) as wait:
            self.assertEqual(client.call({}, q, "direct"), result)
            wait.assert_called_once()
        self.assertEqual(client.calls[0]["attempts"], 2)
        client2 = runner.Client("private-key", self.out / "calls/two", control, fingerprint)
        error2 = urllib.error.HTTPError("https://example.invalid", 429, "Rate limit", {}, io.BytesIO(b"slow down"))
        with patch.object(runner.urllib.request, "urlopen", side_effect=error2) as network, \
                patch.object(control.stop, "wait", side_effect=lambda _: (control.stop.set() or True)):
            with self.assertRaises(runner.BillingStop):
                client2.call({}, q, "direct")
            self.assertEqual(network.call_count, 1)

    def test_cli_mini_full_smoke_audit_reports_and_resume(self):
        args = ["--workers", "1"]
        with patch.object(baseline, "load_data", return_value=([self.row], self.manifest)), \
                patch.object(runner, "api_key", return_value="private-key"), \
                patch.object(runner.urllib.request, "urlopen", side_effect=self.opener) as network, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(args + ["--audit-only"]), 0)
            self.assertFalse(self.out.exists())
            network.assert_not_called()
            self.assertEqual(runner.main(args), 0)
            self.assertEqual(runner.main(args), 0)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(runner.main(args + ["--smoke"]), 0)
        summary = runner.read_json(self.out / "summary.json")
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["protocol"]["protocol"], v2.PROTOCOL)
        self.assertTrue((self.out / "predictions.csv").exists())
        self.assertFalse((self.out / ".run.lock").exists())
        self.assertTrue((self.root / "results/jev_v2_mini_smoke/config.json").exists())
        with patch.object(runner, "load_full", return_value=([self.row], self.manifest)) as load, \
                patch.object(baseline, "load_data") as mini, \
                patch.object(runner, "api_key", return_value="private-key"), \
                patch.object(runner.urllib.request, "urlopen", side_effect=self.opener), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--subset", "full", "--workers", "1"]), 0)
            load.assert_called_once()
            mini.assert_not_called()

    def test_cli_balance_stop_writes_partial_report_then_resumes(self):
        error = urllib.error.HTTPError("https://example.invalid", 403, "Denied", {},
                                       io.BytesIO(b'{"error":"not enough credits"}'))
        with patch.object(baseline, "load_data", return_value=([self.row], self.manifest)), \
                patch.object(runner, "api_key", return_value="private-key"), \
                contextlib.redirect_stdout(io.StringIO()):
            with patch.object(runner.urllib.request, "urlopen", side_effect=error) as network:
                self.assertEqual(runner.main(["--workers", "1"]), 2)
                self.assertEqual(network.call_count, 1)
            summary = runner.read_json(self.out / "summary.json")
            self.assertFalse(summary["complete"])
            self.assertEqual(summary["pending_n"], 1)
            self.assertFalse((self.out / "predictions/case_001.json").exists())
            with patch.object(runner.urllib.request, "urlopen", side_effect=self.opener):
                self.assertEqual(runner.main(["--workers", "1"]), 0)
            self.assertEqual(runner.read_json(self.out / "balance_stop.json")["status"], "resolved")

    def test_stage_metrics_measure_pruning_loss_and_conditional_accuracy(self):
        rows, predictions = [], {}
        for i, final, chosen in [(0, [2, 3], 2), (1, [2, 3], 3), (2, [1, 3], 3)]:
            row = dict(self.row, question_ID=str(i))
            rows.append(row)
            predictions[str(i)] = {"method": "segmented_choice", "recall_candidates": [1, 2, 3],
                                   "expanded_candidates": [0, 1, 2, 3], "final_candidates": final,
                                   "predicted_step": chosen}
        metrics = runner.stage_metrics(rows, predictions)
        self.assertEqual(metrics["reduction_lost"], 1)
        self.assertEqual(metrics["final_accuracy_given_root_present"], 0.5)


if __name__ == "__main__":
    unittest.main()
