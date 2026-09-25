"""Offline checks for exact v4 prompts, 3/5/6 reduction, and end-to-end isolation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import urllib.error
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate as baseline
import evaluate_jev_v3 as prior_runner
import evaluate_jev_v4 as runner
import jev_v2 as v2
import jev_v3 as prior
import jev_v4 as current
from test_jev_v2 import FakeClient, answer, history, response_for


class PromptPolicyTests(unittest.TestCase):
    def test_exact_two_deletions_other_five_prompts_unchanged(self):
        self.assertEqual(current.PROMPTS, current.approved_prompts())
        differences = []
        for stage, group in prior.PROMPTS.items():
            for name, text in group.items():
                if current.PROMPTS[stage][name] != text:
                    differences.append((stage, name))
        self.assertEqual(differences, [("reduce", "root_step")])
        text = prior.PROMPTS["reduce"]["root_step"]
        expected = text.replace(current.WARNING, "").replace(current.OLD_COMPARISON, current.NEW_COMPARISON)
        self.assertEqual(current.PROMPTS["reduce"]["root_step"], expected)
        self.assertIn("earliest origin", expected)
        for stage in ("direct", "recall", "reduce", "final"):
            roles = ["Planner", "Worker"] if stage in ("direct", "final") else None
            old, new = prior.questions([3, 1, 2], stage, roles), current.questions([3, 1, 2], stage, roles)
            for name in old:
                self.assertEqual(old[name]["criteria"], new[name]["criteria"])
                self.assertEqual(new[name]["instructions"], current.PROMPTS[stage][name])

    def test_reduction_policy_boundaries_and_caps(self):
        cfg = current.settings()
        self.assertEqual(cfg["recall"], prior.settings()["recall"])
        self.assertEqual(cfg["reduce"]["medium_keep"], 5)
        for conf, expected in ((0, 6), (.2999, 6), (.3, 5), (.6999, 5), (.7, 3), (1, 3), (None, 6)):
            event = v2.retain(answer(list(range(8)), conf), list(range(8)), "reduce", cfg)
            self.assertEqual(event["actual_keep"], expected)
        self.assertEqual(v2.retain(answer(list(range(8)), .5), list(range(8)), "recall", cfg)["actual_keep"], 4)
        for n in range(4, 9):
            self.assertEqual(v2.retain(answer(list(range(n)), .5), list(range(n)), "reduce", cfg)["actual_keep"], min(5, n - 1))

    def test_multiround_pipeline_matches_v3_with_only_policy_override(self):
        old_cfg, old_prompts = copy.deepcopy(prior.CONFIG), copy.deepcopy(prior.PROMPTS)
        for records in (history(3), history(280)):
            a, b = FakeClient(conf=.5), FakeClient(conf=.5)
            previous = prior.predict(records, a, prior.settings({"reduce": {"medium_keep": 5}}))
            new = current.predict(records, b)
            self.assertEqual(new["protocol"], current.PROTOCOL)
            new["protocol"] = previous["protocol"]
            self.assertEqual(new, previous)
            self.assertEqual(len(a.calls), len(b.calls))
            self.assertLessEqual(len(new["final_candidates"]), 8)
            for x, y in zip(a.calls, b.calls):
                self.assertEqual(x["tag"], y["tag"])
                self.assertEqual(x["request"]["state"], y["request"]["state"])
                self.assertEqual(x["response"], y["response"])
                self.assertNotIn("SECRET_GOLD", baseline.dumps(y["request"]))
                if y["tag"].startswith("reduce") or y["tag"] == "final":
                    self.assertIn("selection_history", y["request"]["state"])
            if len(records) > 255:
                self.assertTrue(any(c["tag"].startswith("reduce_01") for c in b.calls))
        self.assertEqual(prior.CONFIG, old_cfg)
        self.assertEqual(prior.PROMPTS, old_prompts)
        self.assertEqual(v2.CONFIG["reduce"]["medium_keep"], 4)
        self.assertIs(prior_runner._runner.inference, prior)
        self.assertIs(runner._runner.inference, current)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent
        self.root = test_root / (".tmp_v4_" + uuid.uuid4().hex)
        self.root.mkdir()
        def cleanup():
            assert self.root.resolve().parent == test_root
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.out = self.root / "results/jev_v4_mini"
        self.rows = [{"question_ID": "case_%03d" % n, "history": history(n), "source": "example",
                      "mistake_step": n - 1, "mistake_agent": "Worker", "mistake_reason": "SECRET_GOLD"}
                     for n in (3, 280)]
        self.manifest = {"revision": "test", "files": []}
        for item in (patch.object(runner, "ROOT", self.root), patch.object(runner._runner, "ROOT", self.root),
                     patch.object(baseline, "load_data", return_value=(self.rows, self.manifest))):
            item.start()
            self.addCleanup(item.stop)

    def opener(self, request, timeout):
        payload = json.loads(request.data)
        self.assertNotIn("SECRET_GOLD", baseline.dumps(payload))
        text = payload["questions"]["root_step"]["instructions"]
        self.assertIn(text, [group["root_step"] for group in current.PROMPTS.values()])
        if text == current.PROMPTS["reduce"]["root_step"]:
            self.assertNotIn(current.WARNING, text)
            self.assertIn("selection_history", payload["state"])
        return io.BytesIO(json.dumps(response_for(payload, conf=.5)).encode())

    def args(self):
        return ["--workers", "1", "--output", str(self.out)]

    def test_full_pipeline_cache_resume_and_verify(self):
        with patch.object(runner, "read_key", return_value="private-test-key"), \
                patch.object(runner._runner.urllib.request, "urlopen", side_effect=self.opener) as network, \
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
        config = runner._runner.read_json(self.out / "config.json")
        self.assertEqual(config["inference"]["reduce"]["medium_keep"], 5)
        self.assertEqual(config["inference"]["recall"]["medium_keep"], 4)
        self.assertEqual(config["prompts"], current.PROMPTS)
        audit = runner._runner.read_json(self.out / "result_audit.json")
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["history_records_covered"], 283)
        self.assertNotIn(b"\r\r\n", (self.out / "predictions.csv").read_bytes())
        for path in (self.out / "calls").glob("*/*.json"):
            self.assertEqual(runner._runner.read_json(path)["protocol"], current.PROTOCOL)
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-test-key", path.read_bytes())

    def test_partial_failure_is_reported_and_verifiable_without_network(self):
        error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, io.BytesIO(b'blocked'))
        with patch.object(runner, "read_key", return_value="private-test-key"), \
                patch.object(runner._runner.urllib.request, "urlopen", side_effect=error), \
                patch.object(runner._runner.threading.Event, "wait", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.args()), 1)
        with patch.object(runner._runner.urllib.request, "urlopen") as network, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.args() + ["--verify-only"]), 0)
            network.assert_not_called()
        metrics = runner._runner.read_json(self.out / "metrics.json")
        self.assertFalse(metrics["complete"])
        self.assertEqual(metrics["failed_n"], 2)
        self.assertEqual(runner._runner.read_json(self.out / "result_audit.json")["status"], "partial_passed")

    def test_prompt_mismatch_and_old_output_paths_rejected(self):
        with patch.dict(current.PROMPTS["reduce"], {"root_step": "unexpected"}), \
                patch.object(runner._runner.urllib.request, "urlopen") as network, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.main(self.args() + ["--audit-only"])
        network.assert_not_called()
        for label in ("jev_v2_mini", "jev_v3_mini", "jev_v3_mini_reduce_origin"):
            path = self.root / "results" / label
            path.mkdir(parents=True, exist_ok=True)
            for target in (path, path / "nested"):
                with self.assertRaises(ValueError):
                    runner.prepare(target, {})

    def test_historical_pairing_uses_shared_successes(self):
        reference = self.root / "reference"
        (reference / "predictions").mkdir(parents=True)
        cfg = {"sample_sha256": {r["question_ID"]: runner._runner.digest(r) for r in self.rows}}
        runner._runner.atomic_json(reference / "config.json", cfg)
        direct = self.rows[0]
        previous = {"status": "ok", "config_sha256": runner._runner.digest(cfg), "method": "full_context",
                    "predicted_step": 0, "predicted_role": "Worker", "api_calls": 1, "input_tokens": 10}
        runner._runner.atomic_json(reference / "predictions" / (direct["question_ID"] + ".json"), previous)
        current_predictions = {direct["question_ID"]: dict(previous, predicted_step=2)}
        compared = runner.compare_reference(self.rows, current_predictions, reference)
        self.assertEqual(compared["paired_n"], 1)
        self.assertEqual(compared["changes"]["step_exact"]["improved"], 1)
        self.assertEqual(compared["stage_a"]["n"], 0)


if __name__ == "__main__":
    unittest.main()
