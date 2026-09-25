"""JEV Choice v6 Mini/Full runner with checkpoint replay and paired reporting."""
import argparse
import collections
import concurrent.futures
import copy
from datetime import datetime, timezone
import csv
import http.client
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
from resume_full import insufficient_balance
import jev_v6 as inference

ROOT = baseline.ROOT
SOURCE_FILES = ("evaluate_jev_v6.py", "jev_v6.py", "evaluate.py", "evaluate_full.py", "resume_full.py")



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


class _HTTPClient:
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


class Client(_HTTPClient):
    """Bounded disconnect retry, while retaining the frozen transport's other safeguards."""
    def call(self, state, questions, tag):
        disconnects = []
        for attempt in range(4):
            try:
                response = super().call(state, questions, tag)
                if disconnects:
                    self.calls[-1]["disconnect_retries"] = disconnects
                    atomic_json(self.directory / (tag + ".json"), self.calls[-1], allow_nan=True)
                return response
            except (http.client.RemoteDisconnected, http.client.IncompleteRead, ConnectionResetError,
                    ConnectionAbortedError, BrokenPipeError) as error:
                self.control.check()
                event = {"question_ID": self.directory.name, "tag": tag, "type": type(error).__name__,
                         "disconnect_attempt": attempt + 1, "retrying": attempt < 3}
                disconnects.append(event)
                self.control.retry(event)
                if attempt == 3:
                    raise RuntimeError("Connection closed after four attempts") from None
                if self.control.stop.wait((1, 2, 4)[attempt]):
                    raise BillingStop("Balance stop cancelled disconnect retry")
        raise RuntimeError("Unreachable retry state")


def references(subset):
    return {"v2": ROOT / "results" / ("jev_v2_" + subset),
            "v3_original": ROOT / "results" / ("jev_v3_" + subset),
            "v3_current": ROOT / "results" / ("jev_v3_" + subset + "_reduce_origin"),
            "v4": ROOT / "results" / ("jev_v4_" + subset),
            "v5": ROOT / "results" / ("jev_v5_" + subset)}


def legacy_paths(subset):
    prefix = "phase1" if subset == "mini" else "full"
    return (ROOT / "reports" / (prefix + "_predictions.csv"),
            ROOT / "reports" / (prefix + "_metrics.json"))


def run_config(rows, manifest, subset, smoke, config):
    if inference.approved_prompts() != inference.PROMPTS or inference.PROMPTS != inference.expected_prompts():
        raise ValueError("v6 runtime prompt text differs from the approved exact intervention")
    return {"protocol": inference.PROTOCOL, "inference": config, "subset": subset, "smoke": smoke,
            "dataset_revision": manifest["revision"], "manifest_sha256": digest(manifest),
            "sample_sha256": {r["question_ID"]: digest(r) for r in rows},
            "source_sha256": {name: baseline.sha((Path(__file__).parent / name).read_bytes()) for name in SOURCE_FILES},
            "prompt_file": "prompts/jev_v6.json", "prompt_file_sha256": baseline.sha(inference.PROMPT_FILE.read_bytes()),
            "approved_prompt_sha256": baseline.sha(inference.APPROVED_FILE.read_bytes()),
            "prompts": copy.deepcopy(inference.PROMPTS),
            "changes": {"all_prompts": "exact original v1 RULES plus root/role questions, no appendices",
                        "selection_history": "retain v5 input fields in reduction and final requests",
                        "relative_to_v5": {"recall.medium_keep": [4, 5]},
                        "reduce_keep_high_medium_low": [3, 5, 6], "recall_keep_high_medium_low": [3, 5, 6]},
            "legacy_v1_sha256": {path.name: baseline.sha(path.read_bytes()) for path in legacy_paths(subset) if path.exists()},
            "reference_sha256": {label: {name: baseline.sha((path / name).read_bytes())
                                         for name in ("config.json", "summary.json", "predictions.csv") if (path / name).exists()}
                                 for label, path in references(subset).items()},
            "disconnect_retry_delays_seconds": [1, 2, 4]}


def prepare(out, config):
    target = out.resolve()
    for pattern in ("jev_v2*", "jev_v3*", "jev_v4*", "jev_v5*"):
        for path in (ROOT / "results").glob(pattern):
            if path.is_dir():
                source = path.resolve()
                if target == source or source in target.parents or target in source.parents:
                    raise ValueError("v6 must use an output directory separate from all earlier results")
    out = out.resolve()
    # Also protect ancestors/descendants so a typo cannot mix v6 with old outputs.
    for name in ("phase1", "full", "laya_full", "laya_smoke"):
        old = (ROOT / "results" / name).resolve()
        if out == old or old in out.parents or out in old.parents:
            raise ValueError("Choose a separate JEV v6 output directory")
    if out == ROOT.resolve() or out in ROOT.resolve().parents:
        raise ValueError("Output must be a dedicated experiment directory")
    path = out / "config.json"
    if path.exists():
        if read_json(path) != config:
            raise ValueError("Output belongs to another configuration/code/data version")
    elif out.exists() and any(out.iterdir()):
        raise ValueError("Refusing a nonempty output directory without a matching v6 config")
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


def compare_reference(rows, predictions, directory, sample_hashes=None):
    config_path = directory / "config.json"
    if not config_path.exists():
        return {"available": False}
    old_config = read_json(config_path)
    old_hash = digest(old_config)
    sample_hashes = sample_hashes or {r["question_ID"]: digest(r) for r in rows}
    previous = {}
    for row in rows:
        qid = row["question_ID"]
        if old_config.get("sample_sha256", {}).get(qid) != sample_hashes[qid]:
            raise ValueError("Reference dataset differs: " + qid)
        path = directory / "predictions" / (qid + ".json")
        if path.exists():
            item = read_json(path)
            if item.get("status") == "ok":
                if item.get("config_sha256") != old_hash:
                    raise ValueError("Reference prediction/config mismatch")
                previous[qid] = item
    matched = [r for r in rows if r["question_ID"] in previous and
               predictions.get(r["question_ID"], {}).get("status") == "ok"]
    a = [baseline.score_row(r, previous[r["question_ID"]]) for r in matched]
    b = [baseline.score_row(r, predictions[r["question_ID"]]) for r in matched]
    details = [{"question_ID": r["question_ID"], "source": r["source"],
                "reference_step": r["mistake_step"], "reference_role": r["mistake_agent"],
                "a_step": previous[r["question_ID"]]["predicted_step"],
                "b_step": predictions[r["question_ID"]]["predicted_step"],
                "a_role": previous[r["question_ID"]]["predicted_role"],
                "b_role": predictions[r["question_ID"]]["predicted_role"],
                "a": sa, "b": sb} for r, sa, sb in zip(matched, a, b)]
    def metrics_for(method):
        subset = [r for r in matched if predictions[r["question_ID"]].get("method") == method]
        return {"n": len(subset),
                "a": baseline.metrics([baseline.score_row(r, previous[r["question_ID"]]) for r in subset]),
                "b": baseline.metrics([baseline.score_row(r, predictions[r["question_ID"]]) for r in subset])}
    return {"available": True, "directory": str(directory), "paired_n": len(matched),
            "a": baseline.metrics(a), "b": baseline.metrics(b),
            "changes": {k: {"improved": sum(not x[k] and y[k] for x, y in zip(a, b)),
                            "regressed": sum(x[k] and not y[k] for x, y in zip(a, b))}
                        for k in ("role_correct", "step_exact", "step_within_5")},
            "stage_a": stage_metrics(matched, previous),
            "stage_b": stage_metrics(matched, predictions),
            "direct": metrics_for("full_context"), "long": metrics_for("segmented_choice"),
            "paired_usage": {side: {k: sum(p[r["question_ID"]].get(k, 0) for r in matched)
                                    for k in ("api_calls", "input_tokens", "output_tokens")}
                             for side, p in (("a", previous), ("b", predictions))},
            "details": details, "note": "Historical single-run comparison. Relative to v5 only recall medium-confidence retention changes from four to five; all stages rerun."}


def compare_v1(rows, predictions, manifest, subset):
    """Legacy v1 only has an archived CSV and aggregate stage/usage statistics."""
    csv_path, metric_path = legacy_paths(subset)
    if not csv_path.exists() or not metric_path.exists():
        return {"available": False}
    archived = read_json(metric_path)
    if archived["dataset_revision"] != manifest["revision"]:
        raise ValueError("v1 reference dataset revision differs")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        old = {row["question_ID"]: row for row in csv.DictReader(stream)}
    matched = [r for r in rows if r["question_ID"] in old and predictions.get(r["question_ID"], {}).get("status") == "ok"]
    previous = {}
    for row in matched:
        saved = old[row["question_ID"]]
        if int(saved["reference_step"]) != row["mistake_step"] or \
                baseline.normalize_role(saved["reference_role"]) != baseline.normalize_role(row["mistake_agent"]):
            raise ValueError("v1 CSV labels differ from current dataset")
        previous[row["question_ID"]] = {"predicted_step": int(saved["predicted_step"]),
                                        "predicted_role": saved["predicted_role"]}
    a = [baseline.score_row(r, previous[r["question_ID"]]) for r in matched]
    b = [baseline.score_row(r, predictions[r["question_ID"]]) for r in matched]
    details = [{"question_ID": r["question_ID"], "source": r["source"],
                "reference_step": r["mistake_step"], "reference_role": r["mistake_agent"],
                "a_step": previous[r["question_ID"]]["predicted_step"], "b_step": predictions[r["question_ID"]]["predicted_step"],
                "a_role": previous[r["question_ID"]]["predicted_role"], "b_role": predictions[r["question_ID"]]["predicted_role"],
                "a": sa, "b": sb} for r, sa, sb in zip(matched, a, b)]
    whole = len(matched) == len(old) == len(rows)
    stage_a = copy.deepcopy(archived.get("stage_recall")) if whole else None
    if stage_a is not None:
        stage_a["reduction_lost"] = stage_a["expanded_candidates"] - stage_a["final_candidates"]
    usage = None
    if whole:
        usage = {"a": {"api_calls": archived["successful_api_calls"], "input_tokens": archived["input_tokens"]},
                 "b": {key: sum(predictions[r["question_ID"]].get(key, 0) for r in matched)
                       for key in ("api_calls", "input_tokens")}}
    return {"available": True, "paired_n": len(matched), "a": baseline.metrics(a), "b": baseline.metrics(b),
            "changes": {key: {"improved": sum(not x[key] and y[key] for x, y in zip(a, b)),
                              "regressed": sum(x[key] and not y[key] for x, y in zip(a, b))}
                        for key in ("role_correct", "step_exact", "step_within_5")},
            "stage_a": stage_a, "stage_b": stage_metrics(matched, predictions),
            "paired_usage": usage, "details": details,
            "note": "v1 CSV paired on successful IDs. Legacy per-case candidate sets and usage are unavailable; aggregate stage/usage comparison is only valid for the entire dataset."}


def _write_summary(rows, predictions, manifest, out, config, subset, elapsed, stopped):
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
    summary["baseline_comparison"] = {"available": False, "note": "See v6 comparisons instead."}
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
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(stream.getvalue())
    temp.replace(out / "predictions.csv")
    return summary


def write_reports(rows, predictions, manifest, out, config, subset, elapsed, stopped):
    summary = _write_summary(rows, predictions, manifest, out, config, subset, elapsed, stopped)
    sample_hashes = {r["question_ID"]: digest(r) for r in rows}
    comparisons = {"v1": compare_v1(rows, predictions, manifest, subset),
                   **{label: compare_reference(rows, predictions, directory, sample_hashes)
                      for label, directory in references(subset).items()}}
    summary["comparisons"] = {label: {k: v for k, v in result.items() if k != "details"}
                              for label, result in comparisons.items()}
    summary["successful_only"] = baseline.metrics([baseline.score_row(r, predictions[r["question_ID"]])
                                                   for r in rows if predictions.get(r["question_ID"], {}).get("status") == "ok"])
    atomic_json(out / "comparisons.json", comparisons)
    atomic_json(out / "summary.json", summary)
    atomic_json(out / "metrics.json", {k: v for k, v in summary.items() if k != "details"})
    for label, comparison in comparisons.items():
        if not comparison.get("available") or not comparison["details"]:
            continue
        flat = []
        for row in comparison["details"]:
            item = {k: v for k, v in row.items() if k not in ("a", "b")}
            for side in ("a", "b"):
                item.update({side + "_" + k: v for k, v in row[side].items()})
            flat.append(item)
        with (out / ("paired_" + label + ".csv")).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    lines = ["# JEV v6：Mini/Full 完整流程评测", "",
             "完成状态：%d/%d 条成功，失败 %d 条，未完成 %d 条；%s。" %
             (summary["successful_n"], len(rows), summary["failed_n"], summary["pending_n"],
              "全部完成" if summary["complete"] else "部分结果，不能视为全部样本成功"), "",
             "v6 的六份 prompt 与 v5 逐字一致，继续使用 v1 原文。初筛与缩减的高/中/低置信档都采用 3/5/6，阈值仍为 0.30/0.70。相对 v5，仅初筛中置信档保留数从四改为五，输入字段和其他筛选规则不变。", "",
             "本轮从头执行各阶段，不使用旧版本预测或调用缓存。历史摘要继续提供给缩减与最终请求，最终候选上限仍为八。", "",
             "## 全部指定样本", "",
             "下列准确率按全部指定样本计，失败或缺失预测计为未命中；MAE 仅统计有效预测。", "",
             "| 指标 | v6 |", "|---|---:|"]
    overall = summary["overall"]
    for key, label in (("role_correct", "角色准确率"), ("step_exact", "根因精确命中"),
                       ("step_within_5", "根因 ±5 命中"), ("valid_step", "有效步骤输出率")):
        lines.append("| %s | %.2f%% |" % (label, 100 * (overall[key] or 0)))
    lines += ["| 有效预测根因 MAE | %s |" % ("%.2f 步" % overall["valid_output_root_mae"]
                                                           if overall["valid_output_root_mae"] is not None else "无有效预测"), ""]
    for label, comparison in comparisons.items():
        if not comparison.get("available") or not comparison["paired_n"]:
            continue
        lines += ["## 与 %s 的同范围比较" % label, "",
                  "仅比较双方均成功的同 %d 条轨迹。" % comparison["paired_n"], "",
                  "| 指标 | 历史版本 | v6 | 原错现对 | 原对现错 |", "|---|---:|---:|---:|---:|"]
        for key, metric_label in (("role_correct", "角色准确率"), ("step_exact", "根因精确"), ("step_within_5", "根因 ±5")):
            lines.append("| %s | %.2f%% | %.2f%% | %d | %d |" %
                         (metric_label, comparison["a"][key] * 100, comparison["b"][key] * 100,
                          comparison["changes"][key]["improved"], comparison["changes"][key]["regressed"]))
        lines += ["", "根因 MAE：%.2f → %.2f 步。" %
                  (comparison["a"]["valid_output_root_mae"], comparison["b"]["valid_output_root_mae"]), ""]
        if comparison.get("stage_a") is not None:
            lines += ["| 长轨迹阶段 | 历史版本 | v6 |", "|---|---:|---:|"]
            for key, metric_label in (("n", "长轨迹数"), ("recall_candidates", "初筛包含真根因"),
                                      ("expanded_candidates", "交接扩展后包含真根因"),
                                      ("final_candidates", "最终候选包含真根因"),
                                      ("reduction_lost", "缩减丢失真根因"), ("exact", "最终精确选对")):
                lines.append("| %s | %d | %d |" % (metric_label, comparison["stage_a"][key], comparison["stage_b"][key]))
        else:
            lines += ["v1 缺少逐条候选和用量档案，部分样本范围不与旧版全量阶段统计混比。"]
        if comparison.get("paired_usage") is not None:
            lines += ["", "同范围调用数：%d → %d；输入 tokens：%s → %s。" %
                      (comparison["paired_usage"]["a"]["api_calls"], comparison["paired_usage"]["b"]["api_calls"],
                       format(comparison["paired_usage"]["a"]["input_tokens"], ","),
                       format(comparison["paired_usage"]["b"]["input_tokens"], ",")), ""]
    lines += ["## 分来源表现", "", "| 来源 | 样本数 | 角色准确率 | 根因精确 | ±5 命中 |",
              "|---|---:|---:|---:|---:|"]
    for source, item in summary["by_source"].items():
        lines.append("| %s | %d | %.2f%% | %.2f%% | %.2f%% |" %
                     (source, item["n"], item["role_correct"] * 100, item["step_exact"] * 100, item["step_within_5"] * 100))
    failures = [(qid, p.get("error", "pending")) for qid, p in predictions.items() if p.get("status") != "ok"]
    if failures:
        lines += ["", "## 未完成样本", ""]
        for qid, reason in failures:
            message = "Cloudflare HTTP 403（重试后仍受阻）" if "HTTP 403" in reason and "cloudflare" in reason.lower() else (reason.splitlines() or ["未返回错误详情"])[0][:160]
            lines.append("- `%s`：%s；原始错误见 errors/。" % (qid, message))
    lines += ["", "## 用量和解释范围", "",
              "成功保存调用 %d 次，输入 %s tokens，输出 %s tokens；包含未完成样本已成功的中间调用。" %
              (summary["successful_api_calls"], format(summary["input_tokens"], ","), format(summary["output_tokens"], ",")), "",
              "相对 v5，本轮仅将初筛中置信档保留数从四改为五，prompt、缩减策略和输入构造方式不变。初筛结果会改变后续候选、历史及调用数量。各阶段从头重跑，历史对照仍包含服务时段和重复调用波动，不能把单次变化视为稳定因果结论。", "",
              "[逐条结果](predictions.csv) · [配对比较](comparisons.json) · [指标](metrics.json) · [六份 v6 提示词](../../reports/jev_v6_prompts.md)", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(("TYPESAFE_API_KEY=", "JEV_API_KEY=")):
                key = line.split("=", 1)[1].strip().strip("\"'")
                break
    return key


def read_key(path):
    if path:
        key = path.read_text(encoding="utf-8-sig").strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError("Expected one plain API token in the key file")
        return key
    path = ROOT.parent / "apikey.txt"
    return api_key() or (read_key(path) if path.exists() else None)


def load_rows(subset, smoke):
    rows, manifest = baseline.load_data() if subset == "mini" else load_full()
    if smoke:
        rows = [min((r for r in rows if r["source"] == s), key=lambda r: len(baseline.dumps(r["history"]).encode()))
                for s in sorted({r["source"] for r in rows})]
    return rows, manifest


def verify(out, subset, smoke, config):
    """Replay successes and validate partial archives without issuing requests or changing metrics."""
    rows, manifest = load_rows(subset, smoke)
    frozen = run_config(rows, manifest, subset, smoke, config)
    if read_json(out / "config.json") != frozen:
        raise ValueError("Saved configuration differs from current prompts/data/code")
    config_hash = digest(frozen)
    predictions, checked_calls, covered = {}, 0, 0
    control = RunControl(out)
    row_map = {r["question_ID"]: r for r in rows}
    for path in (out / "calls").glob("*/*.json"):
        saved = read_json(path)
        request = saved["request"]
        stage = saved["tag"].split("_")[0]
        ids = list(map(int, request["questions"]["root_step"]["criteria"]))
        names = list(request["questions"].get("responsible_role", {}).get("criteria", {})) or None
        if path.parent.name not in row_map or saved["tag"] != path.stem or request.get("model") != inference.MODEL \
                or saved.get("protocol") != inference.PROTOCOL or saved.get("config_sha256") != config_hash \
                or saved.get("request_sha256") != digest(request) \
                or saved["response"].get("model") != inference.MODEL \
                or baseline.dumps(request["questions"]) != baseline.dumps(inference.questions(ids, stage, names)):
            raise ValueError("Invalid call archive: " + str(path))
        checked_calls += 1
    for index, row in enumerate(rows, 1):
        qid = row["question_ID"]
        path = out / "predictions" / (qid + ".json")
        if not path.exists():
            error = out / "errors" / (qid + ".json")
            if error.exists():
                p = read_json(error)
                if p.get("config_sha256") != config_hash:
                    raise ValueError("Error provenance mismatch")
                predictions[qid] = p
            continue
        predictions[qid] = process_row(row, None, out, control, config, config_hash)
        parts = {}
        for call_path in sorted((out / "calls" / qid).glob("*.json")):
            call = read_json(call_path)
            stage = call["tag"].split("_")[0]
            if stage in ("direct", "recall"):
                for record in call["request"]["state"]["history" if stage == "direct" else "segment"]:
                    parts.setdefault(record["step"], []).append(record["content"])
        if set(parts) != set(range(len(row["history"]))) or any("".join(parts[h["step"]]) != h["content"] for h in row["history"]):
            raise ValueError("Initial inference did not cover the full original history")
        covered += len(row["history"])
        if index % 50 == 0:
            print("Verified %d/%d dataset rows offline" % (index, len(rows)), flush=True)
    summary = read_json(out / "summary.json")
    recomputed = baseline.summarize(rows, predictions, manifest)
    if summary["overall"] != recomputed["overall"] or summary["stage_recall"] != stage_metrics(rows, predictions):
        raise ValueError("Metrics differ from offline scoring")
    calls = [read_json(p) for p in (out / "calls").glob("*/*.json")]
    used = usage(calls)
    if checked_calls != summary["successful_api_calls"] or any(used[k] != summary[k] for k in ("input_tokens", "output_tokens")):
        raise ValueError("Call/token accounting mismatch")
    successful = sum(p.get("status") == "ok" for p in predictions.values())
    audit = {"status": "passed" if successful == len(rows) else "partial_passed", "offline": True,
             "successful_trajectories": successful, "expected_trajectories": len(rows),
             "checked_calls": checked_calls, "history_records_covered": covered,
             "six_runtime_prompts_match_approved_markdown": True, "selection_decisions_replayed": True,
             "config_sha256": config_hash, "utc": datetime.now(timezone.utc).isoformat()}
    atomic_json(out / "result_audit.json", audit)
    print(json.dumps(audit, indent=2), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", choices=("mini", "full"), default="mini")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--api-key-file", type=Path)
    args = parser.parse_args(argv)
    if args.workers < 1 or args.audit_only and args.verify_only:
        parser.error("Positive workers required; audit-only and verify-only are mutually exclusive")
    if inference.approved_prompts() != inference.PROMPTS:
        parser.error("Runtime prompts differ from the v6 approved Markdown")
    out = ROOT / (args.output or ("results/jev_v6_" + args.subset + ("_smoke" if args.smoke else "")))
    config = inference.settings()
    if args.verify_only:
        return verify(out, args.subset, args.smoke, config)
    try:
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
    prepare(out, frozen)
    key = read_key(args.api_key_file)
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
