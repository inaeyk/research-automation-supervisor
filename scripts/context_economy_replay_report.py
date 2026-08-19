#!/usr/bin/env python3
"""Compare completed ContextEconomyReceiptV1 trees; never launches Codex."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def summarize(root: Path) -> dict[str, object]:
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("context-economy-receipt.json"))
    ]
    additive = (
        "prompt_bytes",
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "combined_tokens",
        "inference_token_sample_count",
        "tool_call_count",
        "model_visible_tool_output_chars",
        "compaction_count",
    )
    summary: dict[str, object] = {"receipt_count": len(receipts)}
    for field in additive:
        values = [receipt.get(field) for receipt in receipts]
        summary[field] = sum(values) if all(isinstance(value, int) for value in values) else None
    inference_maxima = [
        value
        for receipt in receipts
        if isinstance((value := receipt.get("max_inference_input_tokens")), int)
    ]
    inference_medians = [
        value
        for receipt in receipts
        if isinstance((value := receipt.get("median_inference_input_tokens")), int)
    ]
    summary["max_inference_input_tokens"] = max(inference_maxima, default=None)
    summary["median_task_inference_input_tokens"] = (
        statistics.median_low(inference_medians) if inference_medians else None
    )
    summary["override_count"] = sum(bool(receipt.get("overrides")) for receipt in receipts)
    return summary


def comparison(baseline: Path, candidate: Path) -> dict[str, Any]:
    before = summarize(baseline)
    after = summarize(candidate)
    deltas: dict[str, int | None] = {}
    for key in sorted(before.keys() | after.keys()):
        old = before.get(key)
        new = after.get(key)
        deltas[key] = new - old if isinstance(old, int) and isinstance(new, int) else None
    return {"baseline": before, "candidate": after, "delta": deltas}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(comparison(args.baseline, args.candidate), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
