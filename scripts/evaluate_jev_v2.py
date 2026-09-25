"""Independent, resumable JEV v2 Mini/Full runner (Python standard library only)."""
import argparse
import collections
import concurrent.futures
from datetime import datetime, timezone
import csv
import io
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import evaluate as baseline
from evaluate_full import load_full
import jev_v2 as inference
from resume_full import insufficient_balance


ROOT = baseline.ROOT
SOURCE_FILES = ("evaluate_jev_v2.py", "jev_v2.py", "evaluate.py", "evaluate_full.py", "resume_full.py")


def atomic_json(path, value, allow_nan=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=allow_nan) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return baseline.sha(baseline.dumps(value).encode("utf-8"))


class BillingStop(Exception):
    """Stops this run without turning a balance failure into a prediction."""


class RunControl:
    def __init__(self, out):
        self.out = out
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def check(self):
        if self.stop.is_set():
            raise BillingStop("Balance stop triggered; no new requests")

    def balance_stop(self, status):
        with self.lock:
            self.stop.set()
            atomic_json(self.out / "balance_stop.json", {
                "status": "paused_insufficient_balance", "http_status": status,
                "utc": datetime.now(timezone.utc).isoformat(),
                "note": "New and retry requests stopped. In-flight requests may finish. "
                        "Use the same command after replenishing the account or updating its API configuration.",
            })

    def retry(self, event):
        with self.lock:
            with (self.out / "transport_retries.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(baseline.dumps(event) + "\n")


class Client:
    def __init__(self, key, directory, control, config_sha256, cache_only=False):
        self.key, self.directory, self.control = key, directory, control
        self.config_sha256, self.cache_only = config_sha256, cache_only
        self.calls = []

    def _check_response(self, result):
        if result.get("model") != inference.MODEL or not isinstance(result.get("answers"), dict):
            raise ValueError("Unexpected model or missing API answers")

    def record_decision(self, event):
        path = self.directory.parent.parent / "decisions" / self.directory.name / (event["tag"] + ".json")
        if self.cache_only:
            if not path.exists() or read_json(path) != event:
                raise ValueError("Cached selection decision does not match replay: " + event["tag"])
        else:
            atomic_json(path, event)

    def call(self, state, questions, tag):
        payload = {"model": inference.MODEL, "state": state, "questions": questions}
        request_hash = digest(payload)
        path = self.directory / (tag + ".json")
        if path.exists():
            saved = read_json(path)
            if saved.get("protocol") != inference.PROTOCOL or saved.get("config_sha256") != self.config_sha256 \
                    or saved.get("request_sha256") != request_hash or saved.get("request") != payload:
                raise ValueError("Cached request/configuration differs: " + tag)
            self._check_response(saved["response"])
            self.calls.append(saved)
            return saved["response"]
        if self.cache_only:
            raise ValueError("Prediction cache is missing its original call: " + tag)
        if not self.key:
            raise ValueError("API key required for an uncached request")
        self.control.check()
        request = urllib.request.Request(baseline.ENDPOINT, data=baseline.dumps(payload).encode("utf-8"),
                                        headers={"Authorization": "Bearer " + self.key,
                                                 "Content-Type": "application/json"})
        started, errors = time.monotonic(), []
        for attempt in range(5):
            self.control.check()
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    result = json.load(response)
                self._check_response(result)
                saved = {"protocol": inference.PROTOCOL, "config_sha256": self.config_sha256,
                         "tag": tag, "request_sha256": request_hash, "request": payload,
                         "response": result, "elapsed_seconds": time.monotonic() - started,
                         "attempts": attempt + 1, "retry_errors": errors}
                # Preserve even an invalid non-finite confidence in the raw archive.
                # Inference normalizes it to null and uses the low-confidence policy.
                atomic_json(path, saved, allow_nan=True)
                self.calls.append(saved)
                return result
            except urllib.error.HTTPError as error:
                status = error.code
                body = error.read().decode("utf-8", "replace")
                retry_after = error.headers.get("Retry-After", "0") if error.headers else "0"
                error.close()
                if insufficient_balance(status, body):
                    self.control.balance_stop(status)
                    raise BillingStop("Insufficient balance; saved successful calls") from None
                message = body.replace(self.key, "[REDACTED]")[:1500]
                transient = status in (403, 429, 500, 502, 503, 504, 529) or (
                    status == 400 and "Unknown model: " + inference.MODEL in body)
                event = {"status": status, "message": message}
                failure = "HTTP %d: %s" % (status, message)
                try:
                    server_delay = max(0, float(retry_after))
                except (TypeError, ValueError):
                    server_delay = 0
                delay = min(30, max(server_delay, 2 ** attempt))
            except (urllib.error.URLError, TimeoutError) as error:
                transient, delay = True, 2 ** attempt
                event = {"type": type(error).__name__}
                failure = "API connection failed"
            errors.append(event)
            self.control.retry(dict(event, question_ID=self.directory.name, tag=tag,
                                    attempt=attempt + 1, retrying=transient and attempt < 4))
            if not transient or attempt == 4:
                raise RuntimeError(failure)
            if self.control.stop.wait(delay):
                raise BillingStop("Balance stop cancelled a pending retry")
        raise RuntimeError("Unreachable retry state")


def run_config(rows, manifest, subset, smoke, config):
    return {"protocol": inference.PROTOCOL, "inference": config, "subset": subset, "smoke": smoke,
            "dataset_revision": manifest["revision"], "manifest_sha256": digest(manifest),
            "sample_sha256": {row["question_ID"]: digest(row) for row in rows},
            "source_sha256": {name: baseline.sha((Path(__file__).parent / name).read_bytes())
                              for name in SOURCE_FILES},
            "prompts": {"common": baseline.RULES, "stages": inference.STAGE_INSTRUCTIONS,
                        "original_role": baseline.questions([0], ["role"])["responsible_role"]["instructions"],
                        "role_appendix": inference.ROLE_APPENDIX}}


def prepare(out, config):
    out = out.resolve()
    # Also protect ancestors/descendants so a typo cannot mix v2 with old outputs.
    for name in ("phase1", "full", "laya_full", "laya_smoke"):
        old = (ROOT / "results" / name).resolve()
        if out == old or old in out.parents or out in old.parents:
            raise ValueError("Choose a separate JEV v2 output directory")
    if out == ROOT.resolve() or out in ROOT.resolve().parents:
        raise ValueError("Output must be a dedicated experiment directory")
    path = out / "config.json"
    if path.exists():
        if read_json(path) != config:
            raise ValueError("Output belongs to another configuration/code/data version")
    elif out.exists() and any(out.iterdir()):
        raise ValueError("Refusing a nonempty output directory without a matching v2 config")
    out.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        except FileExistsError:
            if read_json(path) != config:
                raise ValueError("Another run created a different output configuration")
    for name in ("predictions", "calls", "decisions", "errors"):
        (out / name).mkdir(exist_ok=True)


def usage(calls):
    return {"api_calls": len(calls),
            "input_tokens": sum(c["response"].get("usage", {}).get("input_tokens", 0) for c in calls),
            "output_tokens": sum(c["response"].get("usage", {}).get("output_tokens", 0) for c in calls)}


def process_row(row, key, out, control, config, config_hash):
    qid = row["question_ID"]
    path = out / "predictions" / (qid + ".json")
    history_hash = digest([baseline.record(h) for h in row["history"]])
    if path.exists():
        saved = read_json(path)
        if saved.get("status") != "ok" or saved.get("protocol") != inference.PROTOCOL \
                or saved.get("config_sha256") != config_hash or saved.get("history_sha256") != history_hash \
                or saved.get("question_ID") != qid:
            raise ValueError("Prediction cache provenance mismatch: " + qid)
        # Replay every decision without network access to validate cached payloads and outputs.
        client = Client(key, out / "calls" / qid, control, config_hash, cache_only=True)
        replay = inference.predict(row["history"], client, config)
        if any(saved.get(k) != v for k, v in dict(replay, **usage(client.calls)).items()):
            raise ValueError("Prediction differs from original call replay: " + qid)
        return saved
    control.check()
    client = Client(key, out / "calls" / qid, control, config_hash)
    started = time.monotonic()
    try:
        prediction = dict(inference.predict(row["history"], client, config), status="ok")
    except BillingStop:
        raise
    except Exception as error:
        message = str(error).replace(key, "[REDACTED]") if key else str(error)
        prediction = {"protocol": inference.PROTOCOL, "status": "error", "error": message}
    prediction.update(question_ID=qid, history_sha256=history_hash, config_sha256=config_hash,
                      elapsed_seconds=time.monotonic() - started, **usage(client.calls))
    target = path if prediction["status"] == "ok" else out / "errors" / (qid + ".json")
    atomic_json(target, prediction)
    return prediction


def stage_metrics(rows, predictions):
    eligible = [r for r in rows if predictions.get(r["question_ID"], {}).get("method") == "segmented_choice"]
    n = len(eligible)
    counts = {key: sum(r["mistake_step"] in predictions[r["question_ID"]][key] for r in eligible)
              for key in ("recall_candidates", "expanded_candidates", "final_candidates")}
    exact = sum(predictions[r["question_ID"]]["predicted_step"] == r["mistake_step"] for r in eligible)
    return dict(counts, n=n, exact=exact,
                recall_rates={key: value / n if n else None for key, value in counts.items()},
                reduction_lost=counts["expanded_candidates"] - counts["final_candidates"],
                reduction_loss_rate=(counts["expanded_candidates"] - counts["final_candidates"]) /
                                    counts["expanded_candidates"] if counts["expanded_candidates"] else None,
                final_accuracy_given_root_present=exact / counts["final_candidates"]
                                                  if counts["final_candidates"] else None)


def baseline_comparison(rows, predictions, subset, current_usage, revision):
    """Only compare matching successful sample IDs; never treat partial runs as Full."""
    path = ROOT / "reports" / ("phase1_predictions.csv" if subset == "mini" else "full_predictions.csv")
    if not path.exists():
        return {"available": False}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reference = {r["question_ID"]: r for r in csv.DictReader(stream)}
    matched = [r for r in rows if r["question_ID"] in reference
               and predictions.get(r["question_ID"], {}).get("status") == "ok"]
    old, new, disagreements = [], [], 0
    for row in matched:
        r = reference[row["question_ID"]]
        if int(r["reference_step"]) != row["mistake_step"] or \
                baseline.normalize_role(r["reference_role"]) != baseline.normalize_role(row["mistake_agent"]):
            raise ValueError("Historical CSV labels do not match selected dataset")
        previous = {"predicted_role": r["predicted_role"], "predicted_step": int(r["predicted_step"])}
        current = predictions[row["question_ID"]]
        old.append(baseline.score_row(row, previous))
        new.append(baseline.score_row(row, current))
        disagreements += previous["predicted_step"] != current["predicted_step"]
    old_metrics, new_metrics = baseline.metrics(old), baseline.metrics(new)
    result = {"available": True, "paired_n": len(matched), "reference_csv": str(path),
              "baseline": old_metrics, "v2": new_metrics, "step_disagreements": disagreements,
              "note": "Historical CSV comparison on matching successful IDs; no causal attribution to individual changes."}
    metrics_path = path.with_name("phase1_metrics.json" if subset == "mini" else "full_metrics.json")
    if {r["question_ID"] for r in matched} == set(reference) and len(matched) == len(rows) and metrics_path.exists():
        previous = read_json(metrics_path)
        if previous.get("dataset_revision") == revision:
            result["whole_subset_usage"] = {}
            for key in ("successful_api_calls", "input_tokens"):
                old_value = previous[key]
                new_value = current_usage[key]
                result["whole_subset_usage"][key] = {
                    "baseline": old_value, "v2": new_value, "delta": new_value - old_value,
                    "ratio": new_value / old_value if old_value else None}
            result["baseline_stage_recall"] = previous.get("stage_recall")
    return result


def write_reports(rows, predictions, manifest, out, config, subset, elapsed, stopped):
    summary = baseline.summarize(rows, predictions, manifest)
    summary["protocol"] = config
    summary.update(expected_n=len(rows), successful_n=sum(p.get("status") == "ok" for p in predictions.values()),
                   failed_n=sum(p.get("status") == "error" for p in predictions.values()),
                   pending_n=len(rows) - len(predictions), balance_stopped=stopped,
                   run_wall_seconds=elapsed, stage_recall=stage_metrics(rows, predictions))
    summary["complete"] = summary["successful_n"] == len(rows)
    summary["cost_note"] = "Estimate using historical reference price $0.042/M input tokens, not a current bill. " \
                           "Usage includes saved calls reused by this run, excludes unsuccessful HTTP attempts."
    # Include successful intermediate calls belonging to cases stopped by billing.
    calls = [read_json(p) for p in (out / "calls").glob("*/*.json")]
    summary.update(successful_api_calls=len(calls), input_tokens=usage(calls)["input_tokens"],
                   output_tokens=usage(calls)["output_tokens"])
    summary["estimated_input_cost_usd"] = summary["input_tokens"] * 0.042 / 1000000
    summary["calls_by_stage"] = dict(collections.Counter(c["tag"].split("_")[0] for c in calls))
    decisions = [read_json(p) for p in (out / "decisions").glob("*/*.json")]
    summary["retention_tiers"] = {stage: dict(collections.Counter(
        d["confidence_tier"] for d in decisions if d["stage"] == stage)) for stage in ("recall", "reduce")}
    summary["baseline_comparison"] = baseline_comparison(rows, predictions, subset, summary, manifest["revision"])
    atomic_json(out / "summary.json", summary)
    atomic_json(out / "metrics.json", {k: v for k, v in summary.items() if k != "details"})
    fields = ["question_ID", "source", "steps", "status", "method", "reference_role", "predicted_role",
              "reference_step", "predicted_step", "role_correct", "step_exact", "step_within_5",
              "absolute_step_error", "role_confidence", "step_confidence", "api_calls", "input_tokens",
              "output_tokens", "recall_candidate_count", "expanded_candidate_count", "final_candidate_count"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for detail in summary["details"]:
        p = detail["prediction"]
        merged = dict(p, **{k: v for k, v in detail.items() if k != "prediction"})
        merged.setdefault("status", "pending")
        for stage in ("recall", "expanded", "final"):
            merged[stage + "_candidate_count"] = len(p.get(stage + "_candidates", []))
        writer.writerow({key: merged.get(key) for key in fields})
    temp = out / "predictions.csv.tmp"
    temp.write_text(stream.getvalue(), encoding="utf-8-sig")
    temp.replace(out / "predictions.csv")
    return summary


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(("TYPESAFE_API_KEY=", "JEV_API_KEY=")):
                key = line.split("=", 1)[1].strip().strip("\"'")
                break
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", choices=("mini", "full"), default="mini")
    parser.add_argument("--output", help="Dedicated output directory; default results/jev_v2_<subset>[_smoke]")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--smoke", action="store_true", help="Shortest trajectory per source")
    parser.add_argument("--audit-only", action="store_true", help="Validate dataset/config without API calls or writes")
    parser.add_argument("--retention-config", type=Path, help="JSON overrides for recall/reduce policies and group limits")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    try:
        config = inference.settings(read_json(args.retention_config) if args.retention_config else None)
        rows, manifest = baseline.load_data() if args.subset == "mini" else load_full()
    except (ValueError, OSError) as error:
        parser.error("Cannot load dataset/config: %s. Download the dataset using download_mini.py / download_full.py." % error)
    for row in rows:
        qid = row["question_ID"]
        if not isinstance(qid, str) or not qid or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in qid):
            parser.error("Unsafe sample ID in dataset")
    if args.smoke:
        rows = [min((r for r in rows if r["source"] == source),
                    key=lambda r: len(baseline.dumps(r["history"]).encode()))
                for source in sorted({r["source"] for r in rows})]
    frozen = run_config(rows, manifest, args.subset, args.smoke, config)
    if args.audit_only:
        print(json.dumps({"protocol": inference.PROTOCOL, "subset": args.subset,
                          "n": len(rows), "configuration_sha256": digest(frozen)}, indent=2))
        return 0
    out = ROOT / (args.output or ("results/jev_v2_" + args.subset + ("_smoke" if args.smoke else "")))
    prepare(out, frozen)
    key = api_key()
    if not key and any(not (out / "predictions" / (r["question_ID"] + ".json")).exists() for r in rows):
        parser.error("Set TYPESAFE_API_KEY or JEV_API_KEY; no API calls were made")
    # Exclusive run lock prevents two writers from changing caches concurrently.
    lock_path = out / ".run.lock"
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        parser.error("Output is locked by another run. If it crashed, verify it stopped before removing .run.lock")
    os.close(lock_fd)
    control, predictions, started = RunControl(out), {}, time.monotonic()
    config_hash = digest(frozen)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process_row, row, key, out, control, config, config_hash): row for row in rows}
            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                try:
                    p = future.result()
                except BillingStop:
                    continue
                predictions[row["question_ID"]] = p
                print("%d/%d %s %s" % (len(predictions), len(rows), row["question_ID"], p["status"]), flush=True)
        summary = write_reports(rows, predictions, manifest, out, config, args.subset,
                                time.monotonic() - started, control.stop.is_set())
        if summary["complete"] and not control.stop.is_set():
            # An old billing marker is historical; mark its resolution explicitly.
            marker = out / "balance_stop.json"
            if marker.exists():
                event = read_json(marker)
                event.update(status="resolved", resolved_utc=datetime.now(timezone.utc).isoformat())
                atomic_json(marker, event)
        print(json.dumps({k: summary[k] for k in ("complete", "successful_n", "failed_n", "pending_n",
                                                "balance_stopped", "overall", "stage_recall")}, indent=2))
        return 0 if summary["complete"] else 2 if control.stop.is_set() else 1
    finally:
        lock_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
