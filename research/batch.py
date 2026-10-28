"""Rate-limited, resumable option backtests with immutable request evidence."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time

import pandas as pd

from research.mcp_client import OmegaClient, ToolError


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def strategy(structure="call_debit", dte=7, hold=2, funds=10000, slippage=0.03):
    is_put = structure.startswith("put")
    kind = "Put" if is_put else "Call"
    credit = structure.endswith("credit")
    legs = [{"id": "primary", "daysToExpiration": dte, "quantity": 1, "type": kind,
             "buySell": "Sell" if credit else "Buy", "metricType": "Delta",
             "delta": 30 if credit else 50}]
    if not structure.endswith("long"):
        # A same-expiry vertical with a five-point strike width.
        offset = -5 if is_put else 5
        legs.append({"id": "wing", "daysToExpiration": dte, "quantity": 1, "type": kind,
                     "buySell": "Buy" if credit else "Sell", "metricType": "ChildLeg",
                     "parentId": "primary", "childMetricType": "ParentOffset",
                     "parentStrikeOffset": offset, "isExactOffset": True})
    exit_rule = {"id": "whole", "name": "Whole position", "legIds": [],
                 "maxProfit": None, "maxLoss": None, "exitTime": "15:49:00",
                 "useIntraMinuteStop": False}
    if dte == 0:
        exit_rule["exitAtDte"] = 0
    else:
        exit_rule["exitAtDit"] = hold
    return {
        "underlying": "QQQ", "legs": legs, "useExactDte": False,
        "rangeStart": "2023-01-01", "rangeEnd": "2024-12-31",
        "startingFunds": funds, "allocationPercentage": 100,
        "maxAllocationAmount": 1000, "maxOpenTrades": 1, "maxContractsPerTrade": 1,
        "ignoreMarginRequirements": False, "capitalPerContract": None,
        "entryTime": "10:29:00", "weeklyEntries": [], "monthlyEntryDay": "None",
        "specificEntryDays": None, "useFloatingEntryTime": False,
        "exitConditions": [exit_rule],
        "openingFees": 1.0, "closingFees": 1.0,
        "entrySlippage": slippage, "exitSlippage": slippage, "stopLossSlippage": slippage,
        "closeOpenTradesAtTestCompletion": True, "capProfits": False,
        "capLosses": False, "requireTwoPricesForProfitTarget": False,
        "requireTwoPricesForStopLoss": False,
    }


def make_specs(features, smoke=False):
    if smoke:
        params = strategy()
        params.update(rangeStart="2024-01-01", rangeEnd="2024-01-31",
                      specificEntryDays="2024-01-02,2024-01-16,2024-01-30")
        return [{"name": "smoke-date-filter", "parameters": params}]
    frame = features.loc[(features.date >= "2023-01-01") & (features.date <= "2024-12-31")]
    specs = []
    signals = {
        "all": ["call_debit", "put_debit", "put_credit", "call_credit", "call_long", "put_long"],
        "morning_up": ["call_debit", "call_long", "put_credit"],
        "morning_down": ["put_debit", "put_long", "call_credit"],
        "tuesday_breakout_up": ["call_debit", "call_long", "put_credit"],
        "tuesday_breakout_down": ["put_debit", "put_long", "call_credit"],
        "vwap_pullback_up": ["call_debit", "call_long", "put_credit"],
        "vwap_above_down": ["put_debit", "put_long", "call_credit"],
        "user_combo_up": ["call_debit", "put_credit"],
    }
    for signal, structures in signals.items():
        selected = frame.loc[frame[signal]]
        if selected.empty:
            continue
        for structure in structures:
            for dte, hold in [(0, 0), (7, 2)]:
                params = strategy(structure, dte, hold)
                params["specificEntryDays"] = ",".join(selected.date)
                specs.append({"name": f"dev-{signal}-{structure}-{dte}dte",
                              "signal": signal, "requested_entries": len(selected),
                              "parameters": params})
    # Matched Tuesday controls are necessary for the Monday-range hypothesis.
    selected = frame.loc[frame.weekday == 1]
    for structure in ["call_debit", "put_debit", "put_credit", "call_credit"]:
        params = strategy(structure)
        params["specificEntryDays"] = ",".join(selected.date)
        specs.append({"name": f"dev-tuesday-control-{structure}-7dte",
                      "signal": "tuesday", "requested_entries": len(selected), "parameters": params})
    return specs


def next_pending(jobs):
    uncertain = [job for job in jobs if job["state"] in {"submitting", "uncertain"}]
    if uncertain:
        raise RuntimeError("Reconcile uncertain submission before resuming: "
                           + uncertain[0]["spec"]["name"])
    return next((job for job in jobs if job["state"] == "pending"), None)


def validate_spec(spec):
    parameters = spec["parameters"]
    dates = parameters.get("specificEntryDays")
    if dates == "" or (spec.get("signal") not in {None, "weekly"} and not dates):
        raise ValueError("Empty signal dates must not become unrestricted entries: " + spec["name"])


def run_batch(client, specs, output, label, concurrency=8):
    for spec in specs:
        validate_spec(spec)
    output = Path(output)
    manifest_path = output / f"{label}-jobs.json"
    if manifest_path.exists():
        jobs = json.loads(manifest_path.read_text(encoding="utf-8"))
        if [j["spec"] for j in jobs] != specs:
            raise RuntimeError("Existing batch differs from requested specs; use a new label")
    else:
        jobs = [{"spec": deepcopy(spec), "state": "pending"} for spec in specs]
        write_json(manifest_path, jobs)
    deadline = time.monotonic() + 7200
    while any(j["state"] not in {"done", "failed", "cancelled"} for j in jobs):
        if time.monotonic() > deadline:
            raise TimeoutError("Batch time limit; resume with the same label")
        active = [j for j in jobs if j["state"] in {"queued", "running", "complete"}]
        pending = next_pending(jobs)
        if pending and len(active) < concurrency:
            pending["state"] = "submitting"
            write_json(manifest_path, jobs)
            try:
                started = client.call("run_backtest", {"parameters": pending["spec"]["parameters"]})
                if "runId" not in started:
                    raise RuntimeError(json.dumps(started))
                pending.update(runId=started["runId"], state=started["status"],
                               next_poll=time.time() + (started.get("pollAfterMs") or 5000) / 1000)
                print("START", pending["spec"]["name"], started["runId"], flush=True)
            except ToolError as error:
                pending.update(state="failed", error=str(error))
                print("ERROR", pending["spec"]["name"], str(error)[:500], flush=True)
            except Exception as error:
                pending.update(state="uncertain", error=str(error))
                write_json(manifest_path, jobs)
                raise
            write_json(manifest_path, jobs)
        for job in list(active):
            if time.time() < job.get("next_poll", 0):
                continue
            status = client.call("get_backtest_status", {"runId": job["runId"]})
            job["state"] = status["status"]
            job["next_poll"] = time.time() + (status.get("pollAfterMs") or 1000) / 1000
            write_json(manifest_path, jobs)
            if job["state"] == "complete":
                result = client.call("get_backtest_results", {"runId": job["runId"]})
                trades = client.call("get_trade_log", {"runId": job["runId"], "limit": 5000,
                                                       "sortBy": "opened", "direction": "asc"})
                write_json(output / (job["spec"]["name"] + "-results.json"), result)
                write_json(output / (job["spec"]["name"] + "-trades.json"), trades)
                job["state"] = "done"
                print("DONE", job["spec"]["name"], json.dumps(result)[:220], flush=True)
                write_json(manifest_path, jobs)
        time.sleep(1)
    return jobs


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--label")
    args = parser.parse_args()
    output = Path(args.output)
    features = pd.read_csv(output / "nq-features.csv")
    specs = make_specs(features, args.smoke)
    write_json(output / ("smoke-specs.json" if args.smoke else "development-specs.json"), specs)
    client = OmegaClient(output)
    try:
        print("USAGE", client.call("get_usage_limits"), flush=True)
        client.inventory()
        run_batch(client, specs, output, args.label or ("smoke" if args.smoke else "development"))
    finally:
        client.close()
