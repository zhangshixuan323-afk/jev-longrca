"""v4: two reduction-warning deletions and reduction retention 3/5/6."""
import copy
import importlib.util
import json
from pathlib import Path

import jev_v3 as previous


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "jev-standalone-prompts-v4"
PROMPT_FILE = ROOT / "prompts/jev_v4.json"
APPROVED_FILE = ROOT / "reports/jev_v4_prompts.md"
EXPECTED = previous.EXPECTED
WARNING = ("A confident winner may still be wrong if the true cause was absent\n"
           "from its options.\n")
OLD_COMPARISON = ("Do not treat ranks or confidence from different candidate sets as\n"
                  "comparable scores, or repeated selections as independent confirmation.")
NEW_COMPARISON = ("Do not treat ranks or confidence from different candidate sets as\n"
                  "comparable scores.")


def approved_prompts(path=APPROVED_FILE):
    return previous.approved_prompts(path)


def expected_prompts():
    prompts = copy.deepcopy(previous.PROMPTS)
    text = prompts["reduce"]["root_step"]
    if text.count(WARNING) != 1 or text.count(OLD_COMPARISON) != 1:
        raise ValueError("The v3 source no longer contains the two expected warnings")
    prompts["reduce"]["root_step"] = text.replace(WARNING, "").replace(OLD_COMPARISON, NEW_COMPARISON)
    return prompts


def load_prompts(path=PROMPT_FILE):
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle.get("protocol") != PROTOCOL or bundle.get("prompts") != expected_prompts():
        raise ValueError("v4 must differ from current v3 by exactly two reduction-warning deletions")
    return bundle["prompts"]


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
_spec = importlib.util.spec_from_file_location("_jev_v4_pipeline", ROOT / "scripts/jev_v2.py")
_pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pipeline)
_pipeline.PROTOCOL = PROTOCOL
_pipeline.CONFIG["protocol"] = PROTOCOL
_pipeline.CONFIG["reduce"]["medium_keep"] = 5
_pipeline.questions = questions
MODEL = _pipeline.MODEL
CONFIG = copy.deepcopy(_pipeline.CONFIG)
settings = _pipeline.settings
predict = _pipeline.predict
confidence = _pipeline.confidence
ranked_steps = _pipeline.ranked_steps
