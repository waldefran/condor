#!/usr/bin/env python3
"""Validate the production context skill against the host's MarketContextV2."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

# Standalone invocation uses the same contract as the production host.
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from pydantic import ValidationError
from condor.brooks.contracts import MarketContextV2

PROBABILITY = re.compile(
    r"(?:\b\d{1,3}(?:\.\d+)?\s?%\s*(?:chance|probability|odds|win\s+rate)|\b(?:probability|chance|odds|win\s+rate)\s*[:=]?\s*\d|\b\d+\s+out\s+of\s+\d)",
    re.I,
)


def validate_output(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {"ok": False, "errors": ["empty output"]}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "errors": ["output must be strict JSON with no prose or code fence"]}
    try:
        context = MarketContextV2.model_validate(parsed)
    except ValidationError as exc:
        return {"ok": False, "errors": [str(exc)], "parsed": parsed}
    # Preserve the skill's ban on numeric odds in narrative fields as well.
    narratives = [
        *context.observations, *context.evidence_for, *context.evidence_against,
        *context.transition_conditions, *context.missing_information,
        *(structure.description for structure in context.structures),
    ]
    errors = ["numeric probability or odds detected"] if PROBABILITY.search(" ".join(narratives)) else []
    return {"ok": not errors, "errors": errors, "parsed": parsed}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_output.py <raw-output-file>", file=sys.stderr)
        return 2
    result = validate_output(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
