"""Behavioral v6 regression tests; no network or historical-version imports."""
import contextlib
import copy
import csv
import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate as baseline
import evaluate_jev_v6 as runner
import jev_v6 as current


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
    return {"model": current.MODEL, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 10}}


class FakeClient:
    def __init__(self, conf=0.1):
        self.conf, self.calls = conf, []

    def call(self, state, questions, tag):
        payload = {"state": state, "questions": questions}
        result = response_for(payload, self.conf)
        self.calls.append(copy.deepcopy({"tag": tag, "request": payload, "response": result}))
        return result


class InferenceTests(unittest.TestCase):
    def test_original_v6_requests_and_predictions_are_unchanged(self):
        reference = json.loads(Path(__file__).with_name("jev_v6_reference.json").read_text(encoding="utf-8"))
        def digest(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for case in reference["cases"]:
            with self.subTest(case=case):
                records = history(case["steps"])
                if case["fragmented"]:
                    for record in records:
                        record["content"] = "x" * 12000
                client = FakeClient(conf=case["confidence"])
                prediction = current.predict(records, client)
                self.assertEqual(digest(prediction), case["prediction_sha256"])
                self.assertEqual(digest(client.calls), case["calls_sha256"])

    def test_prompt_assets_match_original_baseline_and_approved_text(self):
        self.assertEqual(current.PROMPTS, current.approved_prompts())
        for stage in ("direct", "recall", "reduce", "final"):
            names = ["Planner", "Worker"] if stage in ("direct", "final") else None
            self.assertEqual(current.questions([5, 1, 3], stage, names), baseline.questions([5, 1, 3], names))

    def test_retention_boundaries_invalid_confidence_and_choice_priority(self):
        ids, config = list(range(8)), current.settings()
        for value, recall, reduce in ((0, 6, 6), (.2999, 6, 6), (.3, 5, 5), (.6999, 5, 5), (.7, 3, 3), (1, 3, 3),
                                      (None, 6, 6), (True, 6, 6), (".8", 6, 6), (-1, 6, 6), (float("nan"), 6, 6)):
            for stage, count in (("recall", recall), ("reduce", reduce)):
                event = current.retain(answer(ids, value), ids, stage, config)
                self.assertEqual(event["actual_keep"], count)
                self.assertEqual(event["kept_steps"][0], 7)
        tie = {"choice": "3", "confidence": .8, "probabilities": {str(i): .25 for i in range(4)}}
        self.assertEqual(current.retain(tie, list(range(4)), "recall", config)["kept_steps"], [3, 0, 1])
        for n in range(4, 9):
            self.assertEqual(current.retain(answer(list(range(n)), .5), list(range(n)), "reduce", config)["actual_keep"], min(5, n - 1))

    def test_history_is_bounded_and_annotations_never_enter_requests(self):
        client = FakeClient(conf=.1)
        prediction = current.predict(history(280), client)
        self.assertLessEqual(len(prediction["final_candidates"]), 8)
        previous_tags = set()
        for call in client.calls:
            payload = call["request"]
            self.assertNotIn("SECRET_GOLD", baseline.dumps(payload))
            self.assertNotIn("mistake_", baseline.dumps(payload))
            ledger = payload["state"].get("selection_history")
            if ledger:
                self.assertTrue(set(ledger["calls"]).issubset(previous_tags))
                self.assertNotIn("probabilities", baseline.dumps(ledger))
                for candidate in ledger["candidates"]:
                    self.assertLessEqual(len(set(candidate) & {"first_recall", "latest_reduction"}), 2)
            previous_tags.add(call["tag"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent
        self.root = parent / (".tmp_v6_" + uuid.uuid4().hex)
        self.root.mkdir()
        def cleanup():
            assert self.root.resolve().parent == parent
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.out = self.root / "results/jev_v6_mini"
        self.rows = [{"question_ID": "case_%03d" % n, "history": history(n), "source": "example",
                      "mistake_step": n - 1, "mistake_agent": "Worker", "mistake_reason": "SECRET_GOLD"}
                     for n in (3, 280)]
        self.manifest = {"revision": "test", "files": []}
        for item in (patch.object(runner, "ROOT", self.root),
                     patch.object(baseline, "load_data", return_value=(self.rows, self.manifest))):
            item.start()
            self.addCleanup(item.stop)

    def opener(self, request, timeout):
        payload = json.loads(request.data)
        self.assertNotIn("SECRET_GOLD", baseline.dumps(payload))
        ids = list(map(int, payload["questions"]["root_step"]["criteria"]))
        names = list(payload["questions"].get("responsible_role", {}).get("criteria", {})) or None
        self.assertEqual(payload["questions"], baseline.questions(ids, names))
        return io.BytesIO(json.dumps(response_for(payload, conf=.5)).encode())

    def args(self):
        return ["--workers", "1", "--output", str(self.out)]

    def test_full_run_resume_and_offline_verify_do_not_repeat_calls(self):
        with patch.object(runner, "read_key", return_value="private-test-key"), \
                patch.object(runner.urllib.request, "urlopen", side_effect=self.opener) as network, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.args() + ["--audit-only"]), 0)
            network.assert_not_called()
            self.assertFalse(self.out.exists())
            self.assertEqual(runner.main(self.args()), 0)
            count = network.call_count
            self.assertGreater(count, 2)
            self.assertEqual(runner.main(self.args()), 0)
            self.assertEqual(runner.main(self.args() + ["--verify-only"]), 0)
            self.assertEqual(network.call_count, count)
        frozen = runner.read_json(self.out / "config.json")
        self.assertEqual(frozen["inference"]["reduce"]["medium_keep"], 5)
        self.assertEqual(frozen["inference"]["recall"]["medium_keep"], 5)
        self.assertEqual(frozen["prompts"], current.PROMPTS)
        audit = runner.read_json(self.out / "result_audit.json")
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["history_records_covered"], 283)
        self.assertNotIn(b"\r\r\n", (self.out / "predictions.csv").read_bytes())
        for path in (self.out / "calls").glob("*/*.json"):
            self.assertEqual(runner.read_json(path)["protocol"], current.PROTOCOL)
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-test-key", path.read_bytes())

    def test_partial_failure_is_not_reported_as_full_success(self):
        error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, io.BytesIO(b'blocked'))
        with patch.object(runner, "read_key", return_value="private-test-key"), \
                patch.object(runner.urllib.request, "urlopen", side_effect=error), \
                patch.object(runner.threading.Event, "wait", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.args()), 1)
        with patch.object(runner.urllib.request, "urlopen") as network, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.args() + ["--verify-only"]), 0)
            network.assert_not_called()
        metrics = runner.read_json(self.out / "metrics.json")
        self.assertFalse(metrics["complete"])
        self.assertEqual(metrics["failed_n"], 2)
        self.assertEqual(runner.read_json(self.out / "result_audit.json")["status"], "partial_passed")

    def test_provenance_and_all_previous_outputs_protected(self):
        with patch.dict(current.PROMPTS["reduce"], {"root_step": "unexpected"}), \
                patch.object(runner.urllib.request, "urlopen") as network, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.main(self.args() + ["--audit-only"])
        network.assert_not_called()
        for name in ("jev_v2_mini", "jev_v3_mini_reduce_origin", "jev_v4_mini", "jev_v5_mini"):
            path = self.root / "results" / name
            path.mkdir(parents=True, exist_ok=True)
            for target in (path, path / "nested"):
                with self.assertRaises(ValueError):
                    runner.prepare(target, {})

    def test_legacy_v1_pairing_does_not_mix_partial_and_full_stage_counts(self):
        csv_path, metric_path = runner.legacy_paths("mini")
        csv_path.parent.mkdir(parents=True)
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["question_ID", "reference_step", "reference_role", "predicted_step", "predicted_role"])
            writer.writeheader()
            for row in self.rows:
                writer.writerow({"question_ID": row["question_ID"], "reference_step": row["mistake_step"],
                                 "reference_role": "Worker", "predicted_step": 0, "predicted_role": "Worker"})
        runner.atomic_json(metric_path, {"dataset_revision": "test", "successful_api_calls": 20,
                                                "input_tokens": 2000, "stage_recall": {"n": 1, "expanded_candidates": 1, "final_candidates": 1}})
        row = self.rows[0]
        predictions = {row["question_ID"]: {"status": "ok", "method": "full_context",
                                           "predicted_step": 2, "predicted_role": "Worker"}}
        result = runner.compare_v1(self.rows, predictions, self.manifest, "mini")
        self.assertEqual(result["paired_n"], 1)
        self.assertEqual(result["changes"]["step_exact"]["improved"], 1)
        self.assertIsNone(result["stage_a"])
        self.assertIsNone(result["paired_usage"])

    def test_cached_request_tampering_is_rejected_without_network(self):
        with patch.object(runner, "read_key", return_value="private-key"), \
                patch.object(runner.urllib.request, "urlopen", side_effect=self.opener), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.args()), 0)
        path = next((self.out / "calls").glob("*/*.json"))
        saved = runner.read_json(path)
        saved["request"]["state"]["known_outcome"] = "tampered"
        runner.atomic_json(path, saved)
        with patch.object(runner.urllib.request, "urlopen") as network, self.assertRaises(ValueError):
            runner.main(self.args() + ["--verify-only"])
        network.assert_not_called()

    def test_balance_stop_blocks_new_calls_and_pending_retries(self):
        self.out.mkdir(parents=True)
        control = runner.RunControl(self.out)
        client = runner.Client("key", self.out / "calls/case", control, "config")
        error = urllib.error.HTTPError("https://example.invalid", 402, "Payment", {}, io.BytesIO(b"insufficient credits"))
        with patch.object(runner.urllib.request, "urlopen", side_effect=error) as network:
            with self.assertRaises(runner.BillingStop):
                client.call({}, current.questions([0, 1], "recall"), "first")
            with self.assertRaises(runner.BillingStop):
                client.call({}, current.questions([0, 1], "recall"), "second")
            self.assertEqual(network.call_count, 1)
        self.assertTrue((self.out / "balance_stop.json").is_file())

    def test_disconnect_retries_preserve_payload_and_are_bounded(self):
        self.out.mkdir(parents=True)
        control = runner.RunControl(self.out)
        client = runner.Client("key", self.out / "calls/case", control, "config")
        with patch.object(runner.urllib.request, "urlopen", side_effect=http.client.RemoteDisconnected()) as network, \
                patch.object(control.stop, "wait", return_value=False), self.assertRaises(RuntimeError):
            client.call({}, current.questions([0, 1], "recall"), "first")
        self.assertEqual(network.call_count, 4)
        self.assertEqual(len({c.args[0].data for c in network.call_args_list}), 1)


if __name__ == "__main__":
    unittest.main()
