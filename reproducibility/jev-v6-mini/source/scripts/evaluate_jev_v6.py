"""Run a fresh v6 Mini/Full evaluation: unchanged v5 prompts, both stages keep 3/5/6."""
import argparse
import copy
import csv
from datetime import datetime, timezone
import http.client
import importlib.util
import json
from pathlib import Path

import evaluate as baseline
import jev_v6 as inference


ROOT = baseline.ROOT
_spec = importlib.util.spec_from_file_location("_jev_v6_runner", ROOT / "scripts/evaluate_jev_v2.py")
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)
_runner.inference = inference
SOURCE_FILES = _runner.SOURCE_FILES + ("jev_v3.py", "jev_v6.py", "evaluate_jev_v6.py")
_original_prepare = _runner.prepare
_original_write_reports = _runner.write_reports
_original_api_key = _runner.api_key


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
            "dataset_revision": manifest["revision"], "manifest_sha256": _runner.digest(manifest),
            "sample_sha256": {r["question_ID"]: _runner.digest(r) for r in rows},
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
    _original_prepare(out, config)


class Client(_runner.Client):
    """Bounded disconnect retry, while retaining the frozen transport's other safeguards."""
    def call(self, state, questions, tag):
        disconnects = []
        for attempt in range(4):
            try:
                response = super().call(state, questions, tag)
                if disconnects:
                    self.calls[-1]["disconnect_retries"] = disconnects
                    _runner.atomic_json(self.directory / (tag + ".json"), self.calls[-1], allow_nan=True)
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
                    raise _runner.BillingStop("Balance stop cancelled disconnect retry")
        raise RuntimeError("Unreachable retry state")


def compare_reference(rows, predictions, directory, sample_hashes=None):
    config_path = directory / "config.json"
    if not config_path.exists():
        return {"available": False}
    old_config = _runner.read_json(config_path)
    old_hash = _runner.digest(old_config)
    sample_hashes = sample_hashes or {r["question_ID"]: _runner.digest(r) for r in rows}
    previous = {}
    for row in rows:
        qid = row["question_ID"]
        if old_config.get("sample_sha256", {}).get(qid) != sample_hashes[qid]:
            raise ValueError("Reference dataset differs: " + qid)
        path = directory / "predictions" / (qid + ".json")
        if path.exists():
            item = _runner.read_json(path)
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
            "stage_a": _runner.stage_metrics(matched, previous),
            "stage_b": _runner.stage_metrics(matched, predictions),
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
    archived = _runner.read_json(metric_path)
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
            "stage_a": stage_a, "stage_b": _runner.stage_metrics(matched, predictions),
            "paired_usage": usage, "details": details,
            "note": "v1 CSV paired on successful IDs. Legacy per-case candidate sets and usage are unavailable; aggregate stage/usage comparison is only valid for the entire dataset."}


def write_reports(rows, predictions, manifest, out, config, subset, elapsed, stopped):
    summary = _original_write_reports(rows, predictions, manifest, out, config, subset, elapsed, stopped)
    # Use newline='' for CSV output on Windows.
    path = out / "predictions.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields, records = reader.fieldnames, list(reader)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    sample_hashes = {r["question_ID"]: _runner.digest(r) for r in rows}
    comparisons = {"v1": compare_v1(rows, predictions, manifest, subset),
                   **{label: compare_reference(rows, predictions, directory, sample_hashes)
                      for label, directory in references(subset).items()}}
    summary["comparisons"] = {label: {k: v for k, v in result.items() if k != "details"}
                              for label, result in comparisons.items()}
    summary["successful_only"] = baseline.metrics([baseline.score_row(r, predictions[r["question_ID"]])
                                                   for r in rows if predictions.get(r["question_ID"], {}).get("status") == "ok"])
    _runner.atomic_json(out / "comparisons.json", comparisons)
    _runner.atomic_json(out / "summary.json", summary)
    _runner.atomic_json(out / "metrics.json", {k: v for k, v in summary.items() if k != "details"})
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


_runner.run_config = run_config
_runner.prepare = prepare
_runner.Client = Client
_runner.baseline_comparison = lambda *args: {"available": False, "note": "See v6 comparisons instead."}
_runner.write_reports = write_reports


def load_rows(subset, smoke):
    rows, manifest = baseline.load_data() if subset == "mini" else _runner.load_full()
    if smoke:
        rows = [min((r for r in rows if r["source"] == s), key=lambda r: len(baseline.dumps(r["history"]).encode()))
                for s in sorted({r["source"] for r in rows})]
    return rows, manifest


def verify(out, subset, smoke, config):
    """Replay successes and validate partial archives without issuing requests or changing metrics."""
    rows, manifest = load_rows(subset, smoke)
    frozen = run_config(rows, manifest, subset, smoke, config)
    if _runner.read_json(out / "config.json") != frozen:
        raise ValueError("Saved configuration differs from current prompts/data/code")
    config_hash = _runner.digest(frozen)
    predictions, checked_calls, covered = {}, 0, 0
    control = _runner.RunControl(out)
    row_map = {r["question_ID"]: r for r in rows}
    for path in (out / "calls").glob("*/*.json"):
        saved = _runner.read_json(path)
        request = saved["request"]
        stage = saved["tag"].split("_")[0]
        ids = list(map(int, request["questions"]["root_step"]["criteria"]))
        names = list(request["questions"].get("responsible_role", {}).get("criteria", {})) or None
        if path.parent.name not in row_map or saved["tag"] != path.stem or request.get("model") != inference.MODEL \
                or saved.get("protocol") != inference.PROTOCOL or saved.get("config_sha256") != config_hash \
                or saved.get("request_sha256") != _runner.digest(request) \
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
                p = _runner.read_json(error)
                if p.get("config_sha256") != config_hash:
                    raise ValueError("Error provenance mismatch")
                predictions[qid] = p
            continue
        predictions[qid] = _runner.process_row(row, None, out, control, config, config_hash)
        parts = {}
        for call_path in sorted((out / "calls" / qid).glob("*.json")):
            call = _runner.read_json(call_path)
            stage = call["tag"].split("_")[0]
            if stage in ("direct", "recall"):
                for record in call["request"]["state"]["history" if stage == "direct" else "segment"]:
                    parts.setdefault(record["step"], []).append(record["content"])
        if set(parts) != set(range(len(row["history"]))) or any("".join(parts[h["step"]]) != h["content"] for h in row["history"]):
            raise ValueError("Initial inference did not cover the full original history")
        covered += len(row["history"])
        if index % 50 == 0:
            print("Verified %d/%d dataset rows offline" % (index, len(rows)), flush=True)
    summary = _runner.read_json(out / "summary.json")
    recomputed = baseline.summarize(rows, predictions, manifest)
    if summary["overall"] != recomputed["overall"] or summary["stage_recall"] != _runner.stage_metrics(rows, predictions):
        raise ValueError("Metrics differ from offline scoring")
    calls = [_runner.read_json(p) for p in (out / "calls").glob("*/*.json")]
    used = _runner.usage(calls)
    if checked_calls != summary["successful_api_calls"] or any(used[k] != summary[k] for k in ("input_tokens", "output_tokens")):
        raise ValueError("Call/token accounting mismatch")
    successful = sum(p.get("status") == "ok" for p in predictions.values())
    audit = {"status": "passed" if successful == len(rows) else "partial_passed", "offline": True,
             "successful_trajectories": successful, "expected_trajectories": len(rows),
             "checked_calls": checked_calls, "history_records_covered": covered,
             "six_runtime_prompts_match_approved_markdown": True, "selection_decisions_replayed": True,
             "config_sha256": config_hash, "utc": datetime.now(timezone.utc).isoformat()}
    _runner.atomic_json(out / "result_audit.json", audit)
    print(json.dumps(audit, indent=2), flush=True)
    return 0


def read_key(path):
    if path:
        key = path.read_text(encoding="utf-8-sig").strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError("Expected one plain API token in the key file")
        return key
    path = ROOT.parent / "apikey.txt"
    return _original_api_key() or (read_key(path) if path.exists() else None)


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
    forwarded = ["--subset", args.subset, "--output", str(out), "--workers", str(args.workers)]
    if args.smoke:
        forwarded.append("--smoke")
    if args.audit_only:
        forwarded.append("--audit-only")
    _runner.api_key = lambda: read_key(args.api_key_file)
    return _runner.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
