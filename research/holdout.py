"""Freeze candidate configurations before evaluating reserved years."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path

import pandas as pd

from research.batch import run_batch, write_json
from research.experiments import BASE_NAMES, signal_dates
from research.mcp_client import OmegaClient


def apply_coordinates(parameters, coordinates):
    result = deepcopy(parameters)
    for coordinate in coordinates:
        parts = coordinate["parameterPath"].split(".")
        target = result
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        key = parts[-1]
        value = coordinate["value"]
        if "time" in coordinate:
            value = coordinate["time"] + ":00"
        elif key in {"daysToExpiration", "exitAtDit", "exitAtDte"}:
            value = int(value)
        target[key] = value
    return result


def freeze(output):
    path = output / "frozen-holdout.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    jobs = json.loads((output / "development-jobs.json").read_text(encoding="utf-8"))
    lookup = {job["spec"]["name"]: job["spec"] for job in jobs}
    candidates = []
    names = BASE_NAMES + [
        "dev-all-put_credit-0dte", "dev-all-call_long-7dte",
        "dev-tuesday-control-call_debit-7dte", "dev-all-call_credit-0dte",
    ]
    for name in names:
        candidates.append({"name": name.removeprefix("dev-"),
                           "signal": lookup[name]["signal"],
                           "parameters": deepcopy(lookup[name]["parameters"]),
                           "selection": "Prespecified baseline or matched control"})
    for name in ["dev-morning_up-put_credit-0dte", "dev-morning_up-call_long-7dte",
                 "dev-all-put_credit-0dte"]:
        result = json.loads((output / (name + "-optimization-results.json")).read_text(encoding="utf-8"))
        eligible = [cell for cell in result["cells"]
                    if cell.get("numberOfTrades", 0) >= 30 and cell.get("profit", 0) > 0
                    and cell.get("marRatio") is not None]
        if not eligible:
            continue
        # Entire grid is retained. This selection deliberately has no access to
        # 2025-2026 results; 2024 is also not used to pick the optimizer cell.
        best = max(eligible, key=lambda cell: (cell["marRatio"], cell["profit"], -cell["index"]))
        params = apply_coordinates(lookup[name]["parameters"], best["coordinate"])
        candidate = {"name": "optimized-" + name.removeprefix("dev-"),
                     "signal": lookup[name]["signal"], "parameters": params,
                     "selection": "Highest 2023 MAR among positive cells with at least 30 trades",
                     "optimizationId": result["optimizationId"], "cell": best}
        candidates.append(candidate)
        if candidate["signal"] != "all":
            candidates.append({"name": "control-" + candidate["name"], "signal": "all",
                               "parameters": deepcopy(params),
                               "selection": "Same optimized option structure without NQ direction filter"})
    frozen = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "test_start": "2025-01-01", "test_end": "2026-09-25",
        "development": "2023-2024 family exploration; optimizer cells selected on 2023 only",
        "restriction": "Do not change candidate rules after viewing reserved-period outcomes",
        "evaluation_policy": "Each candidate: $10k/$25k at 0.03 slippage; $10k at 0.10. Four original hypotheses also use one-point spreads where applicable with a 1% initial-account risk budget.",
        "source_sha256": json.loads((output / "source-manifest.local.json").read_text())["sha256"],
        "candidates": candidates,
    }
    write_json(path, frozen)
    return frozen


def make_holdout_specs(frozen, features):
    specs = []
    for candidate in frozen["candidates"]:
        dates = signal_dates(features, candidate["signal"], frozen["test_start"], frozen["test_end"])
        if not dates:
            raise ValueError("Frozen candidate has no signals: " + candidate["name"])
        for funds, slip in [(10000, 0.03), (25000, 0.03), (10000, 0.10)]:
            params = deepcopy(candidate["parameters"])
            params.update(rangeStart=frozen["test_start"], rangeEnd=frozen["test_end"],
                          specificEntryDays=",".join(dates), startingFunds=funds,
                          entrySlippage=slip, exitSlippage=slip, stopLossSlippage=slip)
            specs.append({"name": f"holdout-{candidate['name']}-{funds}-{slip}",
                          "signal": candidate["signal"], "requested_entries": len(dates),
                          "parameters": params})
        if "dev-" + candidate["name"] in BASE_NAMES:
            for funds in [10000, 25000]:
                params = deepcopy(candidate["parameters"])
                legs = params["legs"]
                if len(legs) == 2:
                    legs[1]["parentStrikeOffset"] = 1 if legs[0]["type"] == "Call" else -1
                reserve = len(legs) * (params["openingFees"] + params["closingFees"])
                params.update(rangeStart=frozen["test_start"], rangeEnd=frozen["test_end"],
                              specificEntryDays=",".join(dates), startingFunds=funds,
                              allocationPercentage=1, maxAllocationAmount=funds * 0.01 - reserve)
                specs.append({"name": f"budget-holdout-{candidate['name']}-{funds}",
                              "signal": candidate["signal"], "requested_entries": len(dates),
                              "parameters": params})
    return specs


def pinned_specs(output, frozen):
    """Resume the exact submitted treatment; never regenerate existing specs."""
    path = output / "holdout-specs.json"
    integrity_path = output / "holdout-input-integrity.json"
    if path.exists():
        if not integrity_path.exists():
            raise RuntimeError("Existing holdout specs require an integrity manifest")
        integrity = json.loads(integrity_path.read_text())
        for name in ["holdout-specs.json", "frozen-holdout.json", "nq-features.csv"]:
            source = output / name
            expected = integrity["files"][str(source)]["sha256"]
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise RuntimeError("Frozen holdout input changed: " + name)
        return json.loads(path.read_text())
    features = pd.read_csv(output / "nq-features.csv")
    specs = make_holdout_specs(frozen, features)
    write_json(path, specs)
    files = [path, output / "frozen-holdout.json", output / "nq-features.csv"]
    write_json(integrity_path, {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "files": {str(p): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                          "bytes": p.stat().st_size} for p in files},
    })
    return specs


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    frozen = freeze(args.output)
    print("FROZEN CANDIDATES", len(frozen["candidates"]), "AT", frozen["frozen_at"], flush=True)
    if not args.freeze_only:
        specs = pinned_specs(args.output, frozen)
        client = OmegaClient(args.output)
        try:
            run_batch(client, specs, args.output, "holdout")
        finally:
            client.close()
