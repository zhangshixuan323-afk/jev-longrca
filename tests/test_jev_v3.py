"""Checks for exact approved prompts and an unchanged candidate-selection pipeline."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate as baseline
import evaluate_jev_v2 as old_runner
import evaluate_jev_v3 as runner
import jev_v2 as old
import jev_v3 as current
from test_jev_v2 import FakeClient, history, response_for


class PromptTests(unittest.TestCase):
    def test_all_six_runtime_prompts_are_exact_approved_blocks(self):
        approved = current.approved_prompts()
        self.assertEqual(current.PROMPTS, approved)
        for stage in ("direct", "recall", "reduce", "final"):
            names = ["Planner", "Worker"] if stage in ("direct", "final") else None
            questions = current.questions([5, 1, 3], stage, names)
            prior = old.questions([5, 1, 3], stage, names)
            self.assertEqual(set(questions), set(approved[stage]))
            for key, question in questions.items():
                self.assertEqual(question["instructions"], approved[stage][key])
                self.assertNotEqual(question["instructions"], prior[key]["instructions"])
                self.assertEqual(question["criteria"], prior[key]["criteria"])
                self.assertEqual(question["type"], "choice")
                self.assertFalse(question["instructions"].startswith(baseline.RULES))

    def test_only_prompts_change_under_identical_model_responses(self):
        before = copy.deepcopy(old.CONFIG)
        for records in (history(3), history(280)):
            a, b = FakeClient(), FakeClient()
            previous, new = old.predict(records, a), current.predict(records, b)
            self.assertEqual(new["protocol"], current.PROTOCOL)
            new["protocol"] = previous["protocol"]
            self.assertEqual(previous, new)
            self.assertEqual(len(a.calls), len(b.calls))
            for x, y in zip(a.calls, b.calls):
                self.assertEqual(x["tag"], y["tag"])
                self.assertEqual(x["request"]["state"], y["request"]["state"])
                self.assertEqual(x["response"], y["response"])
        self.assertEqual(old.CONFIG, before)
        self.assertEqual(old.PROTOCOL, "jev-choice-adaptive-history-v2")
        self.assertIs(old_runner.inference, old)
        self.assertIs(runner._runner.inference, current)

    def test_incomplete_approval_and_role_on_wrong_stage_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incomplete.md"
            path.write_text("## 已确认：任务（direct / root_step）\n\n```text\nOnly one prompt.\n```\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                current.approved_prompts(path)
        with self.assertRaises(ValueError):
            current.questions([1], "recall", ["Worker"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "results/jev_v3_mini"
        self.row = {"question_ID": "case_001", "history": history(3), "source": "example",
                    "mistake_step": 2, "mistake_agent": "Worker", "mistake_reason": "SECRET_GOLD"}
        self.manifest = {"revision": "test-revision", "files": []}
        for p in (patch.object(runner, "ROOT", self.root), patch.object(runner._runner, "ROOT", self.root),
                  patch.object(baseline, "load_data", return_value=([self.row], self.manifest))):
            p.start()
            self.addCleanup(p.stop)

    def opener(self, request, timeout):
        payload = json.loads(request.data)
        self.assertNotIn("SECRET_GOLD", baseline.dumps(payload))
        for key, question in payload["questions"].items():
            self.assertEqual(question["instructions"], current.PROMPTS["direct"][key])
        return io.BytesIO(json.dumps(response_for(payload)).encode())

    def test_cli_runs_new_protocol_resumes_and_verifies_offline(self):
        with patch.object(runner, "read_key", return_value="private-key"), \
                patch.object(runner._runner.urllib.request, "urlopen", side_effect=self.opener) as network, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--audit-only"]), 0)
            network.assert_not_called()
            self.assertFalse(self.out.exists())
            self.assertEqual(runner.main(["--workers", "1"]), 0)
            self.assertEqual(runner.main(["--workers", "1"]), 0)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(runner.main(["--verify-only"]), 0)
            self.assertEqual(network.call_count, 1)
        config = runner._runner.read_json(self.out / "config.json")
        call = runner._runner.read_json(self.out / "calls/case_001/direct.json")
        prediction = runner._runner.read_json(self.out / "predictions/case_001.json")
        self.assertEqual(config["prompts"], current.PROMPTS)
        for obj in (config, call, prediction):
            self.assertEqual(obj["protocol"], current.PROTOCOL)
        self.assertTrue(runner._runner.read_json(self.out / "result_audit.json")["six_runtime_prompts_match_approved_markdown"])
        self.assertNotIn(b"\r\r\n", (self.out / "predictions.csv").read_bytes())
        self.assertTrue((self.out / "report.md").exists())

    def test_changed_runtime_prompt_fails_before_any_api_request(self):
        with patch.dict(current.PROMPTS["direct"], {"root_step": "Different prompt"}), \
                patch.object(runner._runner.urllib.request, "urlopen") as network, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main(["--audit-only"])
            network.assert_not_called()

    def test_v2_result_directory_and_descendants_are_protected(self):
        old_out = self.root / "results/jev_v2_mini"
        old_out.mkdir(parents=True)
        for target in (old_out, old_out / "child"):
            with self.assertRaises(ValueError):
                runner.prepare(target, {})

    def test_compare_uses_v2_results_with_correct_pairing(self):
        reference = self.root / "results/jev_v2_mini"
        reference.mkdir(parents=True)
        (reference / "predictions.csv").write_text(
            "question_ID,reference_role,reference_step,predicted_role,predicted_step\n"
            "case_001,Worker,2,Worker,1\n", encoding="utf-8")
        runner._runner.atomic_json(reference / "metrics.json", {
            "complete": True, "dataset_revision": "test-revision", "protocol": {"protocol": old.PROTOCOL},
            "stage_recall": {"n": 0}, "successful_api_calls": 2, "input_tokens": 200})
        predictions = {"case_001": {"status": "ok", "predicted_step": 2, "predicted_role": "Planner"}}
        compared = runner.compare_v2([self.row], predictions, "mini", {"successful_api_calls": 3, "input_tokens": 250}, "test-revision")
        self.assertEqual(compared["v2"]["step_exact"], 0)
        self.assertEqual(compared["v3"]["step_exact"], 1)
        self.assertEqual(compared["paired_changes"]["step_exact"]["improved"], 1)
        self.assertEqual(compared["whole_subset_usage"]["input_tokens"]["delta"], 50)


if __name__ == "__main__":
    unittest.main()
