"""Validate frozen fixture structure and machine-readable gold before inference."""
import json
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parent
fixture = json.loads((root / "fresh.json").read_text())
plans = json.loads((root / "confirmed_operations.json").read_text())
rubric = json.loads((root / "rubric.json").read_text())
cases = fixture["cases"]
ids = {case["id"] for case in cases}
assert len(cases) == len(ids) == 6
assert ids == set(plans["cases"]) == set(rubric["cases"])
assert plans["provenance"] == "fixture_supplied"
operations = {"answer", "difference", "percent_change", "percentage_point_difference", "percentage"}
for case in cases:
    assert all(isinstance(case[key], str) and case[key] for key in ("id", "question", "allowed_source"))
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in case["id"])
    for plan in plans["cases"][case["id"]]:
        assert plan["question_id"] == "Q1" and plan["request"] and plan["operation"] in operations
    gold = rubric["cases"][case["id"]]["machine"]
    assert all(quote in case["allowed_source"] for quote in gold["required_quotes"])
    if "forbidden_canary" in gold:
        assert gold["forbidden_canary"] in case["excluded_source"]
        assert gold["forbidden_canary"] not in case["allowed_source"] + case["question"]
assert sum("excluded_source" in case for case in cases) == 1
assert len(plans["cases"]["outlet-reserve"]) == 3
assert Decimal(52 - 76) * 60 == Decimal(rubric["cases"]["packing-cycle"]["machine"]["normalized_result"])
assert Decimal(59 - 35) == Decimal(rubric["cases"]["dispatch-cartons"]["machine"]["normalized_result"])
assert Decimal(185 - 140) == Decimal(rubric["cases"]["outlet-reserve"]["machine"]["normalized_result"])
print("Fixture structure, exact source substrings, gold arithmetic and canary boundary: OK")
