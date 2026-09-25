"""Verify the original v6 archive and replay it with the ported engine, offline."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import evaluate as baseline
import evaluate_jev_v6 as runner
import jev_v6 as inference

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "reproducibility/jev-v6-mini"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def checked_path(directory, name):
    path = directory / name
    if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
        raise ValueError("Unsafe archive path: " + name)
    return path


def verify_files(directory, expected):
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != set(expected):
        raise ValueError("Archive file inventory differs: " + str(directory))
    for name, entry in expected.items():
        raw = checked_path(directory, name).read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("Archive bytes differ: " + name)


class Replay:
    """No transport or network fallback exists in this client."""
    def __init__(self, directory, qid, config_hash):
        self.directory, self.qid, self.config_hash = directory, qid, config_hash
        self.calls, self.decisions = [], set()

    def call(self, state, questions, tag):
        saved = read(self.directory / "calls" / self.qid / (tag + ".json"))
        payload = {"model": inference.MODEL, "state": state, "questions": questions}
        if saved.get("protocol") != inference.PROTOCOL or saved.get("config_sha256") != self.config_hash \
                or saved.get("tag") != tag or saved.get("request_sha256") != runner.digest(payload) \
                or baseline.dumps(saved.get("request")) != baseline.dumps(payload) \
                or saved["response"].get("model") != inference.MODEL:
            raise ValueError("Request/provenance differs: " + self.qid + "/" + tag)
        self.calls.append(saved)
        return saved["response"]

    def record_decision(self, event):
        saved = read(self.directory / "decisions" / self.qid / (event["tag"] + ".json"))
        if saved != event:
            raise ValueError("Decision differs: " + self.qid + "/" + event["tag"])
        if event["tag"] in self.decisions:
            raise ValueError("Duplicate decision")
        self.decisions.add(event["tag"])


def verify(archive, data_root, record_root):
    started = time.monotonic()
    inventory = read(record_root / "archive_manifest.json")
    verify_files(archive, inventory["files"])
    verify_files(record_root / "source", inventory["original_source_files"])
    for name, digest in inventory["portable_results"].items():
        if baseline.sha(checked_path(record_root, name).read_bytes()) != digest:
            raise ValueError("Portable historical result differs: " + name)
    config = read(archive / "config.json")
    config_hash = runner.digest(config)
    if config_hash != inventory["original_config_sha256"] or config != read(record_root / "config.json"):
        raise ValueError("Original frozen config differs")
    for name, digest in config["source_sha256"].items():
        if baseline.sha(checked_path(record_root / "source/scripts", name).read_bytes()) != digest:
            raise ValueError("Original source fingerprint differs: " + name)
    for name, key in ((config["prompt_file"], "prompt_file_sha256"),
                      ("reports/jev_v6_prompts.md", "approved_prompt_sha256")):
        if baseline.sha(checked_path(record_root / "source", name).read_bytes()) != config[key]:
            raise ValueError("Original prompt fingerprint differs")
    if config["inference"] != inference.settings() or config["prompts"] != inference.PROMPTS \
            or inference.approved_prompts() != inference.PROMPTS:
        raise ValueError("Ported settings or prompts differ from the historical run")
    previous_root = baseline.ROOT
    try:
        baseline.ROOT = data_root
        rows, manifest = baseline.load_data()
    finally:
        baseline.ROOT = previous_root
    if runner.digest(manifest) != config["manifest_sha256"] or manifest["revision"] != config["dataset_revision"]:
        raise ValueError("Dataset manifest differs")
    if {p.stem for p in (archive / "predictions").glob("*.json")} != {r["question_ID"] for r in rows}:
        raise ValueError("Prediction coverage differs")
    predictions, call_count, decision_count, covered = {}, 0, 0, 0
    for index, row in enumerate(rows, 1):
        qid = row["question_ID"]
        if runner.digest(row) != config["sample_sha256"][qid]:
            raise ValueError("Sample differs: " + qid)
        saved = read(archive / "predictions" / (qid + ".json"))
        if saved.get("status") != "ok" or saved.get("question_ID") != qid \
                or saved.get("config_sha256") != config_hash \
                or saved.get("history_sha256") != runner.digest([baseline.record(h) for h in row["history"]]):
            raise ValueError("Prediction provenance differs: " + qid)
        client = Replay(archive, qid, config_hash)
        prediction = inference.predict(row["history"], client)
        for key, value in dict(prediction, **runner.usage(client.calls)).items():
            if saved.get(key) != value:
                raise ValueError("Prediction differs: " + qid + "/" + key)
        if {p.stem for p in (archive / "calls" / qid).glob("*.json")} != {c["tag"] for c in client.calls} \
                or {p.stem for p in (archive / "decisions" / qid).glob("*.json")} != client.decisions:
            raise ValueError("Unreplayed calls/decisions: " + qid)
        parts = {}
        for call in client.calls:
            stage = call["tag"].split("_")[0]
            if stage in ("direct", "recall"):
                for record in call["request"]["state"]["history" if stage == "direct" else "segment"]:
                    parts.setdefault(record["step"], []).append(record["content"])
        if set(parts) != set(range(len(row["history"]))) or any("".join(parts[h["step"]]) != h["content"] for h in row["history"]):
            raise ValueError("Original log coverage differs: " + qid)
        predictions[qid] = saved
        call_count += len(client.calls)
        decision_count += len(client.decisions)
        covered += len(row["history"])
        if index % 50 == 0:
            print("Replayed %d/%d trajectories offline" % (index, len(rows)), flush=True)
    summary = read(archive / "summary.json")
    metrics = read(archive / "metrics.json")
    recalculated = baseline.summarize(rows, predictions, manifest)
    for key in ("overall", "by_source", "by_method"):
        if summary[key] != recalculated[key] or metrics[key] != recalculated[key]:
            raise ValueError("Scoring differs: " + key)
    if summary["stage_recall"] != runner.stage_metrics(rows, predictions) \
            or call_count != summary["successful_api_calls"] \
            or sum(p["input_tokens"] for p in predictions.values()) != summary["input_tokens"] \
            or sum(p["output_tokens"] for p in predictions.values()) != summary["output_tokens"]:
        raise ValueError("Stage or token accounting differs")
    return {"status": "passed", "offline": True, "network_calls": 0,
            "trajectories": len(rows), "requests": call_count, "decisions": decision_count,
            "history_records": covered, "all_requests_identical": True,
            "all_decisions_identical": True, "all_prediction_fields_identical": True,
            "all_original_archive_bytes_identical": True, "original_config_sha256": config_hash,
            "ported_source_sha256": {name: baseline.sha((ROOT / "scripts" / name).read_bytes())
                                     for name in runner.SOURCE_FILES + (Path(__file__).name,)},
            "seconds": round(time.monotonic() - started, 2), "utc": datetime.now(timezone.utc).isoformat(),
            "note": "Behavioral archive replay only. Original fingerprints are preserved, not rewritten. Fresh inference must use a new output directory."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "results/jev_v6_mini")
    parser.add_argument("--data-root", type=Path, default=ROOT)
    parser.add_argument("--record-root", type=Path, default=RECORD)
    parser.add_argument("--output", type=Path, default=RECORD / "migration_validation.json")
    args = parser.parse_args(argv)
    result = verify(args.archive.resolve(), args.data_root.resolve(), args.record_root.resolve())
    if args.output.resolve().is_relative_to(args.archive.resolve()):
        raise ValueError("Validation output must not modify the original archive")
    runner.atomic_json(args.output, result)
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
