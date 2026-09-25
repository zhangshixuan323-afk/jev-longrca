"""Approved standalone prompts on an isolated copy of the frozen v2 pipeline."""
import copy
import importlib.util
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PROMPT_FILE = ROOT / "prompts/jev_v3.json"
APPROVED_FILE = ROOT / "reports/jev_prompt_revision.md"
PROTOCOL = "jev-standalone-prompts-v3"
EXPECTED = {("direct", "root_step"), ("direct", "responsible_role"),
            ("recall", "root_step"), ("reduce", "root_step"),
            ("final", "root_step"), ("final", "responsible_role")}


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


def load_prompts(path=PROMPT_FILE):
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle.get("protocol") != PROTOCOL:
        raise ValueError("Unexpected prompt bundle version")
    prompts = bundle["prompts"]
    if {(stage, key) for stage, group in prompts.items() for key in group} != EXPECTED:
        raise ValueError("Incomplete standalone prompt bundle")
    if any(not isinstance(text, str) or not text.strip() for group in prompts.values() for text in group.values()):
        raise ValueError("Prompt texts must be nonempty strings")
    return prompts


def questions(step_ids, stage, names=None):
    if stage not in PROMPTS:
        raise ValueError("Unknown inference stage: " + stage)
    if names and stage not in ("direct", "final"):
        raise ValueError("Only direct/final calls judge responsible roles")
    result = {"root_step": {"type": "choice", "instructions": PROMPTS[stage]["root_step"],
                            "criteria": {str(s): "Original trajectory step %d" % s
                                         for s in sorted(set(step_ids))}}}
    if names:
        result["responsible_role"] = {
            "type": "choice", "instructions": PROMPTS[stage]["responsible_role"],
            "criteria": {name: "Recorded workflow role: " + name for name in names}}
    return result


PROMPTS = load_prompts()
# Separate module globals avoid mutating jev_v2 used by old experiments/tests.
_spec = importlib.util.spec_from_file_location("_jev_v3_pipeline", ROOT / "scripts/jev_v2.py")
_pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pipeline)
_pipeline.PROTOCOL = PROTOCOL
_pipeline.CONFIG["protocol"] = PROTOCOL
_pipeline.questions = questions
MODEL = _pipeline.MODEL
CONFIG = copy.deepcopy(_pipeline.CONFIG)
settings = _pipeline.settings
predict = _pipeline.predict
confidence = _pipeline.confidence
ranked_steps = _pipeline.ranked_steps
