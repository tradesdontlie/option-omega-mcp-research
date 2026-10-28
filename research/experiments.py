"""Prespecified robustness, sizing, asset-coverage, and multi-leg experiments."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from research.batch import run_batch, strategy, write_json
from research.mcp_client import OmegaClient


BASE_NAMES = [
    "dev-morning_up-put_credit-0dte",
    "dev-morning_up-call_long-7dte",
    "dev-tuesday_breakout_up-call_debit-7dte",
    "dev-morning_down-call_credit-0dte",
]


def signal_dates(features, signal, start, end):
    frame = features.loc[(features.date >= start) & (features.date <= end)]
    if signal == "tuesday":
        frame = frame.loc[frame.weekday == 1]
    elif signal != "all":
        frame = frame.loc[frame[signal]]
    return frame.date.tolist()


def robustness_specs(jobs, features):
    lookup = {job["spec"]["name"]: job["spec"] for job in jobs}
    specs = []

    def add(name, parameters, signal=None):
        dates = (parameters.get("specificEntryDays") or "").split(",")
        specs.append({"name": name, "parameters": parameters, "signal": signal,
                      "requested_entries": len(dates) if dates != [""] else None})

    for name in BASE_NAMES:
        original = lookup[name]
        short = name.removeprefix("dev-")
        for funds, slippage in [(10000, 0.10), (25000, 0.03), (25000, 0.10)]:
            params = deepcopy(original["parameters"])
            params.update(startingFunds=funds, entrySlippage=slippage,
                          exitSlippage=slippage, stopLossSlippage=slippage)
            add(f"stress-{short}-{funds}-{slippage}", params, original["signal"])
        for funds, percent in [(10000, 0.5), (10000, 1), (25000, 0.5), (25000, 1)]:
            params = deepcopy(original["parameters"])
            legs = params["legs"]
            if len(legs) == 2:
                legs[1]["parentStrikeOffset"] = 1 if legs[0]["type"] == "Call" else -1
            # Reserve the modeled round-trip commissions within the budget.
            reserve = len(legs) * (params["openingFees"] + params["closingFees"])
            params.update(startingFunds=funds, allocationPercentage=percent,
                          maxAllocationAmount=funds * percent / 100 - reserve)
            add(f"budget-{short}-{funds}-{percent}pct", params, original["signal"])
        for entry in ["10:30:00", "10:31:00"]:
            params = deepcopy(original["parameters"])
            params["entryTime"] = entry
            add(f"delay-{short}-{entry[:5].replace(':', '')}", params, original["signal"])

    for name in BASE_NAMES[:2]:
        original = lookup[name]
        for label, filters in [
            ("ivrank-low", {"entryMaxIvRank": 30}),
            ("ivrank-high", {"entryMinIvRank": 50}),
            ("hv-iv-low", {"entryMaxHv30Iv30Ratio": 0.8}),
            ("hv-iv-high", {"entryMinHv30Iv30Ratio": 1.2}),
        ]:
            params = deepcopy(original["parameters"])
            params.update(filters)
            add(f"vol-{name.removeprefix('dev-')}-{label}", params, original["signal"])

    for name in [BASE_NAMES[1], BASE_NAMES[2], "dev-all-call_long-7dte",
                 "dev-tuesday-control-call_debit-7dte"]:
        original = lookup[name]
        params = deepcopy(original["parameters"])
        dates = signal_dates(features, original["signal"], "2013-01-01", "2022-12-30")
        params.update(rangeStart="2013-01-01", rangeEnd="2022-12-30",
                      specificEntryDays=",".join(dates))
        add(f"history-{name.removeprefix('dev-')}", params, original["signal"])

    for underlying in ["SPX", "SPY", "QQQ", "IWM", "AAPL", "TSLA", "MSFT",
                       "NVDA", "AMZN", "GLD", "TLT", "IBIT"]:
        params = strategy("put_credit", dte=30, hold=7)
        params.update(underlying=underlying, rangeStart="2024-01-01",
                      rangeEnd="2024-12-31", weeklyEntries=["Monday"])
        add(f"asset-{underlying}-30dte-put-credit", params, "weekly")

    for signal in ["all", "morning_up"]:
        dates = signal_dates(features, signal, "2023-01-01", "2024-12-31")
        for structure in ["condor", "straddle", "strangle", "calendar"]:
            params = strategy("put_credit" if structure == "condor" else "call_long",
                              dte=0 if structure == "condor" else 7)
            if structure == "condor":
                params["legs"][0]["delta"] = 15
                call_legs = strategy("call_credit", dte=0)["legs"]
                call_legs[0].update(id="call-primary", delta=15)
                call_legs[1].update(id="call-wing", parentId="call-primary")
                params["legs"].extend(call_legs)
            elif structure in {"straddle", "strangle"}:
                delta = 50 if structure == "straddle" else 25
                params["legs"][0]["delta"] = delta
                put = deepcopy(params["legs"][0])
                put.update(id="put", type="Put")
                if structure == "straddle":
                    put.pop("delta")
                    put.update(metricType="ChildLeg", parentId="primary",
                               childMetricType="ParentOffset", parentStrikeOffset=0,
                               isExactOffset=True)
                params["legs"].append(put)
            else:
                params["legs"][0]["daysToExpiration"] = 30
                params["legs"].append({
                    "id": "front", "daysToExpiration": 7, "quantity": 1,
                    "type": "Call", "buySell": "Sell", "metricType": "ChildLeg",
                    "parentId": "primary", "childMetricType": "ParentOffset",
                    "parentStrikeOffset": 0, "isExactOffset": True,
                })
            params["specificEntryDays"] = ",".join(dates)
            add(f"multileg-{signal}-{structure}", params, signal)
    return specs


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    jobs = json.loads((args.output / "development-jobs.json").read_text(encoding="utf-8"))
    features = pd.read_csv(args.output / "nq-features.csv")
    specs = robustness_specs(jobs, features)
    write_json(args.output / "robustness-specs.json", specs)
    client = OmegaClient(args.output)
    try:
        print("ROBUSTNESS TESTS", len(specs), flush=True)
        run_batch(client, specs, args.output, "robustness")
    finally:
        client.close()
