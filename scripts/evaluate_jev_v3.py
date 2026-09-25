"""Run the approved six standalone prompts with unchanged v2 candidate handling."""
import argparse
import copy
import csv
from datetime import datetime, timezone
import http.client
import importlib.util
import json
from pathlib import Path
import sys

import evaluate as baseline
import jev_v3 as inference


ROOT = baseline.ROOT
_spec = importlib.util.spec_from_file_location("_jev_v3_runner", ROOT / "scripts/evaluate_jev_v2.py")
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)
_runner.inference = inference
_source_files = _runner.SOURCE_FILES + ("jev_v3.py", "evaluate_jev_v3.py")
_original_prepare = _runner.prepare
_original_write_reports = _runner.write_reports
_original_api_key = _runner.api_key


def run_config(rows, manifest, subset, smoke, config):
    if inference.approved_prompts() != inference.PROMPTS:
        raise ValueError("Runtime prompts differ from the six approved Markdown text blocks")
    reference = ROOT / "results" / ("jev_v2_" + subset)
    return {"protocol": inference.PROTOCOL, "inference": config, "subset": subset, "smoke": smoke,
            "dataset_revision": manifest["revision"], "manifest_sha256": _runner.digest(manifest),
            "sample_sha256": {r["question_ID"]: _runner.digest(r) for r in rows},
            "source_sha256": {name: baseline.sha((Path(__file__).parent / name).read_bytes()) for name in _source_files},
            "prompt_file": "prompts/jev_v3.json",
            "prompt_file_sha256": baseline.sha(inference.PROMPT_FILE.read_bytes()),
            "prompts": copy.deepcopy(inference.PROMPTS),
            "prompt_sha256": {stage: {key: baseline.sha(text.encode("utf-8")) for key, text in group.items()}
                              for stage, group in inference.PROMPTS.items()},
            "reference_v2_sha256": {name: baseline.sha((reference / name).read_bytes())
                                    for name in ("metrics.json", "predictions.csv") if (reference / name).exists()},
            "disconnect_retry_delays_seconds": [1, 2, 4]}


def prepare(out, config):
    target = out.resolve()
    for protected in (ROOT / "results").glob("jev_v2*"):
        if protected.is_dir():
            source = protected.resolve()
            if target == source or source in target.parents or target in source.parents:
                raise ValueError("v3 needs a separate directory outside existing v2 experiments")
    _original_prepare(out, config)


class Client(_runner.Client):
    """Add bounded disconnect recovery without modifying the frozen v2 client."""
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


def compare_v2(rows, predictions, subset, current_usage, revision):
    reference = ROOT / "results" / ("jev_v2_" + subset)
    csv_path, metrics_path = reference / "predictions.csv", reference / "metrics.json"
    if not csv_path.exists() or not metrics_path.exists():
        return {"available": False, "reference_protocol": "jev-choice-adaptive-history-v2"}
    previous = _runner.read_json(metrics_path)
    if previous["dataset_revision"] != revision or not previous.get("complete"):
        raise ValueError("v2 reference must be complete and use the same dataset revision")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        old = {r["question_ID"]: r for r in csv.DictReader(handle)}
    matched = [r for r in rows if r["question_ID"] in old and predictions.get(r["question_ID"], {}).get("status") == "ok"]
    a, b = [], []
    for row in matched:
        p = old[row["question_ID"]]
        if int(p["reference_step"]) != row["mistake_step"] or \
                baseline.normalize_role(p["reference_role"]) != baseline.normalize_role(row["mistake_agent"]):
            raise ValueError("v2 reference labels differ from this dataset")
        a.append(baseline.score_row(row, {"predicted_role": p["predicted_role"], "predicted_step": int(p["predicted_step"])}))
        b.append(baseline.score_row(row, predictions[row["question_ID"]]))
    result = {"available": True, "reference_protocol": previous["protocol"]["protocol"],
              "paired_n": len(matched), "v2": baseline.metrics(a), "v3": baseline.metrics(b),
              "paired_changes": {key: {"improved": sum(not x[key] and y[key] for x, y in zip(a, b)),
                                       "regressed": sum(x[key] and not y[key] for x, y in zip(a, b))}
                                 for key in ("role_correct", "step_exact", "step_within_5")},
              "note": "Same dataset and v2 pipeline; all six prompts changed together. Historical single-run comparison."}
    if len(matched) == len(rows) and {r["question_ID"] for r in matched} == set(old):
        result["v2_stage_recall"] = previous["stage_recall"]
        result["whole_subset_usage"] = {
            key: {"v2": previous[key], "v3": current_usage[key], "delta": current_usage[key] - previous[key],
                  "ratio": current_usage[key] / previous[key] if previous[key] else None}
            for key in ("successful_api_calls", "input_tokens")}
    return result


def write_reports(rows, predictions, manifest, out, config, subset, elapsed, stopped):
    summary = _original_write_reports(rows, predictions, manifest, out, config, subset, elapsed, stopped)
    # Normalize the reused runner's CSV through the csv module (Windows newline handling).
    path = out / "predictions.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields, records = reader.fieldnames, list(reader)
    temp = path.with_suffix(".csv.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    temp.replace(path)
    comparison = summary["baseline_comparison"]
    lines = ["# JEV v3：已确认独立提示词评测", "",
             "状态：%d/%d 成功；%s。" % (summary["successful_n"], len(rows), "全部完成" if summary["complete"] else "部分结果"), "",
             "六份运行时 instructions 直接取自确认稿，未拼接旧 RULES 或附加旧版提示。模型、分段、证据节选、3/4/6 保留规则、历史摘要和评分口径沿用 v2。", "",
             "模型：`%s`；数据版本：`%s`。" % (inference.MODEL, manifest["revision"]), ""]
    if comparison.get("available"):
        lines += ["## 与 v2 的逐条配对结果", "", "已完成配对 %d 条。" % comparison["paired_n"], "",
                  "| 指标 | v2 | v3 | 变化 |", "|---|---:|---:|---:|"]
        for key, label in (("role_correct", "角色准确率"), ("step_exact", "根因精确命中"), ("step_within_5", "根因 ±5 命中")):
            a, b = comparison["v2"][key], comparison["v3"][key]
            if a is not None and b is not None:
                lines.append("| %s | %.2f%% | %.2f%% | %+.2f 个百分点 |" % (label, a * 100, b * 100, (b - a) * 100))
        a, b = comparison["v2"]["valid_output_root_mae"], comparison["v3"]["valid_output_root_mae"]
        if a is not None and b is not None:
            lines.append("| 根因 MAE | %.2f | %.2f | %+.2f 步 |" % (a, b, b - a))
        lines += ["", "| 指标 | 原错现对 | 原对现错 |", "|---|---:|---:|"]
        for key, values in comparison["paired_changes"].items():
            lines.append("| %s | %d | %d |" % (key, values["improved"], values["regressed"]))
    stage = summary["stage_recall"]
    lines += ["", "## 长轨迹筛选阶段", "", "| 阶段 | v2 | v3 |", "|---|---:|---:|"]
    old_stage = comparison.get("v2_stage_recall")
    for key, label in (("recall_candidates", "初筛包含真根因"), ("expanded_candidates", "交接扩展后包含真根因"),
                       ("final_candidates", "最终候选包含真根因"), ("exact", "最终精确命中")):
        prior = "%d/%d" % (old_stage[key], old_stage["n"]) if old_stage else "未完成同范围比较"
        lines.append("| %s | %s | %d/%d |" % (label, prior, stage[key], stage["n"]))
    if stage["final_candidates"]:
        lines += ["", "v3 真根因在最终候选时的选择准确率：%.2f%%。" % (100 * stage["final_accuracy_given_root_present"])]
    lines += ["", "## 各来源", "", "| 来源 | 样本数 | 角色 | 根因精确 | 根因 ±5 |", "|---|---:|---:|---:|---:|"]
    for source, metrics in summary["by_source"].items():
        lines.append("| %s | %d | %.2f%% | %.2f%% | %.2f%% |" % (source, metrics["n"],
                     metrics["role_correct"] * 100, metrics["step_exact"] * 100, metrics["step_within_5"] * 100))
    lines += ["", "## 用量与文件", "", "成功调用 %s 次，输入 %s tokens。" %
              (format(summary["successful_api_calls"], ","), format(summary["input_tokens"], ","))]
    for key, values in comparison.get("whole_subset_usage", {}).items():
        lines.append("- %s：v2 %s，v3 %s，增量 %+d。" % (key, format(values["v2"], ","), format(values["v3"], ","), values["delta"]))
    lines += ["", "这是同时替换六份提示词的单次历史对照，不能据此分离每份提示词的贡献或认定小幅变化稳定。", "",
              "[逐条 CSV](predictions.csv) · [完整汇总](summary.json) · [指标](metrics.json) · [运行时提示词](../../prompts/jev_v3.json)", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


_runner.run_config = run_config
_runner.prepare = prepare
_runner.Client = Client
_runner.baseline_comparison = compare_v2
_runner.write_reports = write_reports


def load_rows(subset, smoke):
    rows, manifest = baseline.load_data() if subset == "mini" else _runner.load_full()
    if smoke:
        rows = [min((r for r in rows if r["source"] == source), key=lambda r: len(baseline.dumps(r["history"]).encode()))
                for source in sorted({r["source"] for r in rows})]
    return rows, manifest


def verify(out, subset, smoke, config):
    rows, manifest = load_rows(subset, smoke)
    frozen = run_config(rows, manifest, subset, smoke, config)
    if _runner.read_json(out / "config.json") != frozen:
        raise ValueError("Output configuration/code/prompts/data differ")
    if {p.stem for p in (out / "predictions").glob("*.json")} != {r["question_ID"] for r in rows}:
        raise ValueError("Offline verification requires all selected predictions")
    predictions = {}
    control, config_hash = _runner.RunControl(out), _runner.digest(frozen)
    covered, calls, tokens = 0, 0, 0
    for index, row in enumerate(rows, 1):
        qid = row["question_ID"]
        predictions[qid] = _runner.process_row(row, None, out, control, config, config_hash)
        p = predictions[qid]
        if p["status"] != "ok":
            raise ValueError("Unsuccessful prediction during offline verification")
        text_parts = {}
        for path in sorted((out / "calls" / qid).glob("*.json")):
            call = _runner.read_json(path)
            stage = call["tag"].split("_")[0]
            questions = call["request"]["questions"]
            ids = list(map(int, questions["root_step"]["criteria"]))
            names = list(questions["responsible_role"]["criteria"]) if "responsible_role" in questions else None
            if questions != inference.questions(ids, stage, names):
                raise ValueError("Saved runtime prompt does not exactly match approved text")
            if stage in ("direct", "recall"):
                records = call["request"]["state"]["history" if stage == "direct" else "segment"]
                for h in records:
                    text_parts.setdefault(h["step"], []).append(h["content"])
        if set(text_parts) != set(range(len(row["history"]))):
            raise ValueError("Initial inference did not cover all original steps")
        for h in row["history"]:
            if "".join(text_parts[h["step"]]) != h["content"]:
                raise ValueError("Original text coverage mismatch")
        covered += len(row["history"])
        calls += p["api_calls"]
        tokens += p["input_tokens"]
        if index % 50 == 0:
            print("Verified %d/%d predictions offline" % (index, len(rows)), flush=True)
    summary = _runner.read_json(out / "summary.json")
    recomputed = baseline.summarize(rows, predictions, manifest)
    if summary["overall"] != recomputed["overall"] or summary["stage_recall"] != _runner.stage_metrics(rows, predictions):
        raise ValueError("Saved metrics differ from offline scoring")
    if calls != summary["successful_api_calls"] or tokens != summary["input_tokens"] \
            or calls != len(list((out / "calls").glob("*/*.json"))):
        raise ValueError("Call/token accounting mismatch")
    audit = {"status": "passed", "offline": True, "trajectories": len(rows), "api_calls": calls,
             "input_tokens": tokens, "history_records_covered": covered, "configuration_sha256": config_hash,
             "six_runtime_prompts_match_approved_markdown": True,
             "utc": datetime.now(timezone.utc).isoformat()}
    _runner.atomic_json(out / "result_audit.json", audit)
    print(json.dumps(audit, indent=2), flush=True)
    return 0


def read_key(path):
    if path is not None:
        key = path.read_text(encoding="utf-8-sig").strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError("API key file must contain one plain token")
        return key
    workspace_file = ROOT.parent / "apikey.txt"
    return _original_api_key() or (read_key(workspace_file) if workspace_file.is_file() else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", choices=("mini", "full"), default="mini")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--retention-config", type=Path)
    parser.add_argument("--api-key-file", type=Path)
    args = parser.parse_args(argv)
    if args.audit_only and args.verify_only:
        parser.error("audit-only and verify-only are mutually exclusive")
    if inference.approved_prompts() != inference.PROMPTS:
        parser.error("Runtime prompt bundle differs from the approved Markdown")
    out = ROOT / (args.output or ("results/jev_v3_" + args.subset + ("_smoke" if args.smoke else "")))
    cfg = inference.settings(_runner.read_json(args.retention_config) if args.retention_config else None)
    if args.verify_only:
        return verify(out, args.subset, args.smoke, cfg)
    forwarded = ["--subset", args.subset, "--output", str(out), "--workers", str(args.workers)]
    if args.smoke:
        forwarded.append("--smoke")
    if args.audit_only:
        forwarded.append("--audit-only")
    if args.retention_config:
        forwarded.extend(["--retention-config", str(args.retention_config)])
    _runner.api_key = lambda: read_key(args.api_key_file)
    return _runner.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
