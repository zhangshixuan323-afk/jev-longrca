"""v6: exact v5/v1 Choice prompts, with recall and reduction both using 3/5/6."""
import copy
import importlib.util
import json
from pathlib import Path

import evaluate as baseline
import jev_v3 as prompt_parser


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "jev-v1-prompts-adaptive-v6"
PROMPT_FILE = ROOT / "prompts/jev_v6.json"
APPROVED_FILE = ROOT / "reports/jev_v6_prompts.md"
EXPECTED = prompt_parser.EXPECTED


def approved_prompts(path=APPROVED_FILE):
    return prompt_parser.approved_prompts(path)


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


def questions(step_ids, stage, names=None):
    if stage not in PROMPTS:
        raise ValueError("Unknown inference stage: " + stage)
    if names and stage not in ("direct", "final"):
        raise ValueError("Only direct/final calls judge responsible roles")
    result = baseline.questions(step_ids, names)
    if any(question["instructions"] != PROMPTS[stage][key] for key, question in result.items()):
        raise ValueError("Runtime Choice prompt differs from the frozen v1 text")
    return result


PROMPTS = load_prompts()
_spec = importlib.util.spec_from_file_location("_jev_v6_pipeline", ROOT / "scripts/jev_v2.py")
_pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pipeline)
_pipeline.PROTOCOL = PROTOCOL
_pipeline.CONFIG["protocol"] = PROTOCOL
_pipeline.CONFIG["recall"]["medium_keep"] = 5
_pipeline.CONFIG["reduce"]["medium_keep"] = 5
_pipeline.questions = questions
MODEL = _pipeline.MODEL
CONFIG = copy.deepcopy(_pipeline.CONFIG)
settings = _pipeline.settings
predict = _pipeline.predict
confidence = _pipeline.confidence
ranked_steps = _pipeline.ranked_steps
