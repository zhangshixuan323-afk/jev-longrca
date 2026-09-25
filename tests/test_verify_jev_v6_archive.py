"""Integrity checks for the read-only historical v6 replay path."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_jev_v6_archive as replay


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent
        self.root = parent / (".tmp_v6_archive_" + uuid.uuid4().hex)
        self.root.mkdir()
        def cleanup():
            assert self.root.resolve().parent == parent
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)

    def test_inventory_detects_tampering_missing_and_extra_files(self):
        path = self.root / "config.json"
        path.write_bytes(b'{}\n')
        expected = {"config.json": {"bytes": 3, "sha256": hashlib.sha256(b'{}\n').hexdigest()}}
        replay.verify_files(self.root, expected)
        path.write_bytes(b'[]\n')
        with self.assertRaises(ValueError):
            replay.verify_files(self.root, expected)
        path.write_bytes(b'{}\n')
        (self.root / "extra.json").write_bytes(b'{}')
        with self.assertRaises(ValueError):
            replay.verify_files(self.root, expected)
        (self.root / "extra.json").unlink()
        path.unlink()
        with self.assertRaises(ValueError):
            replay.verify_files(self.root, expected)

    def test_paths_cannot_escape_the_archive(self):
        with self.assertRaises(ValueError):
            replay.checked_path(self.root, "../outside.json")

    def test_requests_are_order_sensitive_and_no_network_fallback_exists(self):
        state = {"history": [{"step": 0, "content": "record"}]}
        questions = replay.inference.questions([0, 1], "recall")
        payload = {"model": replay.inference.MODEL, "state": state, "questions": questions}
        saved = {"protocol": replay.inference.PROTOCOL, "config_sha256": "frozen", "tag": "recall_000",
                 "request_sha256": replay.runner.digest(payload), "request": payload,
                 "response": {"model": replay.inference.MODEL, "answers": {}}}
        path = self.root / "calls/case/recall_000.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(saved), encoding="utf-8")
        client = replay.Replay(self.root, "case", "frozen")
        with patch.object(replay.runner.urllib.request, "urlopen") as network:
            self.assertEqual(client.call(state, questions, "recall_000"), saved["response"])
            changed = copy.deepcopy(questions)
            changed["root_step"]["criteria"] = dict(reversed(list(changed["root_step"]["criteria"].items())))
            with self.assertRaises(ValueError):
                client.call(state, changed, "recall_000")
            with self.assertRaises(FileNotFoundError):
                client.call(state, questions, "missing")
            network.assert_not_called()

    def test_validation_output_cannot_modify_original_archive(self):
        with patch.object(replay, "verify", return_value={"status": "passed"}), self.assertRaises(ValueError):
            replay.main(["--archive", str(self.root), "--output", str(self.root / "audit.json")])
        self.assertFalse((self.root / "audit.json").exists())


if __name__ == "__main__":
    unittest.main()
