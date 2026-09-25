"""JEV Choice v6: v1 prompts, 3/5/6 recall and reduction, bounded history."""
import copy
import json
import math
from pathlib import Path
import re

import evaluate as baseline

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "jev-v1-prompts-adaptive-v6"
MODEL = baseline.MODEL
PROMPT_FILE = ROOT / "prompts/jev_v6.json"
APPROVED_FILE = ROOT / "reports/jev_v6_prompts.md"
EXPECTED = {("direct", "root_step"), ("direct", "responsible_role"),
            ("recall", "root_step"), ("reduce", "root_step"),
            ("final", "root_step"), ("final", "responsible_role")}
DEFAULT_POLICY = {"low_threshold": 0.30, "high_threshold": 0.70,
                  "high_keep": 3, "medium_keep": 4, "low_keep": 6}
CONFIG = {k: v for k, v in baseline.CONFIG.items()
          if k not in ("protocol", "recall_k", "final_group_size")}
CONFIG.update(protocol=PROTOCOL, recall=dict(DEFAULT_POLICY, medium_keep=5),
              reduce=dict(DEFAULT_POLICY, medium_keep=5), reduce_group_size=8,
              final_candidate_limit=8, bypass_group_size=3)



def approved_prompts(path=APPROVED_FILE):
    """Extract only confirmed text blocks; preserve their wording and whitespace."""
    result = {}
    fence = "`" * 3
    for section in path.read_text(encoding="utf-8").split("\n## ")[1:]:
        title, _, body = section.partition("\n")
        match = re.search(r"(direct|recall|reduce|final)\s*/\s*(root_step|responsible_role)", title)
        if not match:
            continue
        if not title.startswith("已确认"):
            raise ValueError("Prompt is not confirmed: " + title)
        stage, key = match.groups()
        if key in result.get(stage, {}):
            raise ValueError("Duplicate approved prompt: " + stage + "/" + key)
        if body.count(fence + "text\n") != 1:
            raise ValueError("Expected one complete text block: " + title)
        text, closing, _ = body.split(fence + "text\n", 1)[1].partition("\n" + fence)
        if not closing:
            raise ValueError("Unclosed prompt block: " + title)
        if not text.strip():
            raise ValueError("Empty approved prompt")
        result.setdefault(stage, {})[key] = text
    if {(stage, key) for stage, group in result.items() for key in group} != EXPECTED:
        raise ValueError("Expected all six independent approved prompts")
    return result


def expected_prompts():
    result = {}
    for stage in ("direct", "recall", "reduce", "final"):
        roles = ["role"] if stage in ("direct", "final") else None
        result[stage] = {key: question["instructions"]
                         for key, question in baseline.questions([0], roles).items()}
    return result


def load_prompts(path=PROMPT_FILE):
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle.get("protocol") != PROTOCOL or bundle.get("prompts") != expected_prompts():
        raise ValueError("All v6 instructions must exactly match the original v1 Choice prompts")
    return bundle["prompts"]


PROMPTS = load_prompts()


def questions(step_ids, stage, names=None):
    if stage not in PROMPTS:
        raise ValueError("Unknown inference stage: " + stage)
    if names and stage not in ("direct", "final"):
        raise ValueError("Only direct/final calls judge responsible roles")
    result = baseline.questions(step_ids, names)
    if any(question["instructions"] != PROMPTS[stage][key] for key, question in result.items()):
        raise ValueError("Runtime Choice prompt differs from the frozen v1 text")
    return result


def settings(overrides=None):
    """Only retention settings may change; baseline evidence budgets remain frozen."""
    config = copy.deepcopy(CONFIG)
    allowed = {"recall", "reduce", "reduce_group_size", "final_candidate_limit"}
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - allowed:
            raise ValueError("Retention config only accepts: " + ", ".join(sorted(allowed)))
        for key, value in overrides.items():
            if key in ("recall", "reduce"):
                if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
                    raise ValueError("Unknown retention policy fields: " + key)
                config[key].update(value)
            else:
                config[key] = value
    for stage in ("recall", "reduce"):
        policy = config[stage]
        low, high = policy["low_threshold"], policy["high_threshold"]
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (low, high)) \
                or not 0 <= low < high <= 1:
            raise ValueError("Thresholds must satisfy 0 <= low < high <= 1")
        counts = [policy[k] for k in ("high_keep", "medium_keep", "low_keep")]
        if any(type(x) is not int or not 3 <= x <= 8 for x in counts) or counts != sorted(counts):
            raise ValueError("Retention counts must be nondecreasing integers between 3 and 8")
    if type(config["reduce_group_size"]) is not int or not 4 <= config["reduce_group_size"] <= 8:
        raise ValueError("Reduction group size must be between 4 and 8")
    if type(config["final_candidate_limit"]) is not int or not 3 <= config["final_candidate_limit"] <= 8:
        raise ValueError("Final candidate limit must be between 3 and 8")
    return config


def confidence(answer):
    value = answer.get("confidence")
    valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    return (value if valid else None), valid


def ranked_steps(answer, valid):
    """Preserve choice-first ranking, but refuse malformed probability distributions."""
    ids = sorted(set(valid))
    if not isinstance(answer, dict) or str(answer.get("choice")) not in {str(s) for s in ids}:
        raise ValueError("Invalid root choice returned by API")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        raise ValueError("Missing root probabilities")
    for step in ids:
        p = probabilities.get(str(step))
        if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("Invalid or missing probability for step %d" % step)
    return baseline.top_steps(answer, ids, len(ids))


def retain(answer, ids, stage, config):
    ranked = ranked_steps(answer, ids)
    value, valid = confidence(answer)
    policy = config[stage]
    tier = ("low" if not valid or value < policy["low_threshold"] else
            "medium" if value < policy["high_threshold"] else "high")
    target = policy[tier + "_keep"]
    limit = len(ranked) - 1 if stage == "reduce" else len(ranked)
    count = min(target, limit)
    return {"stage": stage, "candidate_ids": sorted(ids), "ranked_steps": ranked,
            "selected_step": int(answer["choice"]), "question_confidence": value,
            "confidence_valid": valid, "confidence_tier": tier,
            "retention_reason": "confidence_policy" if valid else "invalid_confidence_low_fallback",
            "target_keep": target, "actual_keep": count, "kept_steps": ranked[:count],
            "discarded_steps": sorted(set(ids) - set(ranked[:count]))}


class SelectionHistory:
    """Bounded per-candidate references; request summaries deduplicate source calls."""
    def __init__(self):
        self.candidates = {}
        self.calls = {}

    def observe(self, event):
        tag, stage = event["tag"], event["stage"]
        self.calls[tag] = {"stage": stage, "call_tag": tag,
                           "option_count": len(event["candidate_ids"]),
                           "selected_step": event["selected_step"],
                           "question_confidence": event["question_confidence"],
                           "confidence_valid": event["confidence_valid"]}
        ranks = {step: index + 1 for index, step in enumerate(event["ranked_steps"])}
        for step in event["kept_steps"]:
            entry = self.candidates.setdefault(step, {"origin": "recall"})
            ref = {"call_tag": tag, "rank": ranks[step]}
            if stage == "recall":
                entry.setdefault("first_recall", ref)
            else:
                entry["latest_reduction"] = ref

    def handoff(self, step, source):
        self.candidates.setdefault(step, {"origin": "handoff_expansion", "source_step": source})

    def summary(self, ids):
        calls, candidates = {}, []
        for step in sorted(ids):
            origin = self.candidates[step]
            item = {"step": step, "origin": origin["origin"]}
            if "source_step" in origin:
                item["source_step"] = origin["source_step"]
            for name in ("first_recall", "latest_reduction"):
                ref = origin.get(name)
                if ref is not None:
                    tag = ref["call_tag"]
                    item[name] = tag
                    record = calls.setdefault(tag, dict(self.calls[tag], candidate_ranks={}))
                    record["candidate_ranks"][str(step)] = ref["rank"]
            candidates.append(item)
        return {"candidates": candidates, "calls": calls}


def predict(history, client, config=None):
    config = settings() if config is None else settings(
        {key: config[key] for key in ("recall", "reduce", "reduce_group_size", "final_candidate_limit")}
    )
    if not history or [h["step"] for h in history] != list(range(len(history))):
        raise ValueError("Expected a nonempty trajectory with contiguous original step IDs")
    names = baseline.roles(history)
    if not names or len(names) > 255:
        raise ValueError("Expected between 1 and 255 workflow roles")
    clean = [baseline.record(h) for h in history]
    ids = [h["step"] for h in clean]
    trace = []

    def record_decision(event):
        trace.append(event)
        if hasattr(client, "record_decision"):
            client.record_decision(event)

    if len(baseline.dumps(clean).encode()) <= config["chunk_bytes"] and len(ids) <= 255:
        response = client.call({"history": clean, "known_outcome": "Failed execution"},
                               questions(ids, "direct", names), "direct")
        recall, expanded, count, method, final_tag = ids, ids, 1, "full_context", "direct"
    else:
        shared, ledger = baseline.context(history), SelectionHistory()
        recall, count = set(), 0
        for index, batch in enumerate(baseline.chunks(history)):
            group = sorted({h["step"] for h in batch})
            tag = "recall_%03d" % index
            response = client.call(dict(shared, segment=batch), questions(group, "recall"), tag)
            event = dict(retain(response["answers"].get("root_step"), group, "recall", config), tag=tag)
            record_decision(event)
            ledger.observe(event)
            recall.update(event["kept_steps"])
            count += 1
        recall = sorted(recall)
        candidates = set(recall)
        for step in recall:
            handoff = baseline.nearest_handoff(history, step)
            if handoff is not None:
                candidates.add(handoff)
                ledger.handoff(handoff, step)
        candidates = sorted(candidates)
        expanded = list(candidates)
        record_decision({"tag": "handoff_expansion", "stage": "handoff_expansion",
                         "added_steps": sorted(set(expanded) - set(recall)),
                         "kept_steps": expanded, "actual_keep": len(expanded)})
        level = 0
        while len(candidates) > config["final_candidate_limit"]:
            reduced = set()
            for offset in range(0, len(candidates), config["reduce_group_size"]):
                group = candidates[offset:offset + config["reduce_group_size"]]
                tag = "reduce_%02d_%03d" % (level, offset)
                if len(group) <= config["bypass_group_size"]:
                    event = {"tag": tag, "stage": "reduce_bypass", "candidate_ids": group,
                             "kept_steps": group, "discarded_steps": [], "actual_keep": len(group),
                             "retention_reason": "small_group_no_api_call"}
                else:
                    state = dict(shared, candidate_evidence=[baseline.evidence(history, s) for s in group],
                                 selection_history=ledger.summary(group))
                    response = client.call(state, questions(group, "reduce"), tag)
                    event = dict(retain(response["answers"].get("root_step"), group, "reduce", config), tag=tag)
                    ledger.observe(event)
                record_decision(event)
                reduced.update(event["kept_steps"])
            if len(reduced) >= len(candidates):
                raise RuntimeError("Reduction must strictly decrease the candidate count")
            candidates = sorted(reduced)
            level += 1
        state = dict(shared, candidate_evidence=[baseline.evidence(history, s) for s in candidates],
                     selection_history=ledger.summary(candidates))
        response = client.call(state, questions(candidates, "final", names), "final")
        ids, method, final_tag = candidates, "segmented_choice", "final"

    answers = response["answers"]
    ranked = ranked_steps(answers.get("root_step"), ids)
    step_confidence, valid_confidence = confidence(answers["root_step"])
    record_decision({"tag": final_tag, "stage": final_tag, "candidate_ids": ids,
                     "selected_step": ranked[0], "ranked_steps": ranked,
                     "kept_steps": ranked[:1], "discarded_steps": sorted(set(ids) - {ranked[0]}),
                     "actual_keep": 1, "question_confidence": step_confidence,
                     "confidence_valid": valid_confidence, "retention_reason": "final_choice"})
    role = answers.get("responsible_role", {})
    selected_role = role.get("choice")
    if baseline.normalize_role(selected_role) not in {baseline.normalize_role(n) for n in names}:
        selected_role = None
    return {"protocol": PROTOCOL, "predicted_role": selected_role, "predicted_step": ranked[0],
            "role_confidence": confidence(role)[0], "step_confidence": step_confidence,
            "method": method, "segments": count, "recall_candidates": recall,
            "expanded_candidates": expanded, "final_candidates": ids, "selection_trace": trace}
