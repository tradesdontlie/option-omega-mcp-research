"""Validate exported option trades and summarize provider-reported metrics."""

import argparse
import json
from pathlib import Path
import re
from collections import Counter
from datetime import datetime

import pandas as pd

from research.batch import write_json


def trade_rows(log):
    return [dict(zip(log["tradeColumns"], row)) for row in log.get("trades", [])]


def parse_legs(value):
    pattern = r"(\d+) ([A-Z][a-z]{2}) (\d{1,2}) (\d+(?:\.\d+)?) ([CP]) (BTO|STO) (-?\d+(?:\.\d+)?)"
    result = []
    for part in value.split("|"):
        match = re.fullmatch(pattern, part.strip())
        if match is None:
            raise ValueError("Unrecognized option leg: " + part)
        quantity, month, day, strike, kind, side, price = match.groups()
        result.append({"quantity": int(quantity), "expiry": f"{month} {int(day)}",
                       "strike": float(strike), "type": kind, "side": side, "price": float(price)})
    return result


def structure_issues(parameters, row):
    if row.get("wasAdjusted"):
        return ["Corporate-action adjusted position requires separate normalization review"]
    issues = []
    if row["underlying"] != parameters["underlying"]:
        issues.append("Underlying differs from request")
    try:
        actual = parse_legs(row["legs"])
    except ValueError as error:
        return [str(error)]
    expected = parameters["legs"]
    if len(actual) != len(expected):
        return ["Number of legs differs from request"]
    remaining = list(actual)
    matched = {}
    for leg in expected:
        side = "BTO" if leg["buySell"] == "Buy" else "STO"
        candidate = next((item for item in remaining if item["type"] == leg["type"][0]
                          and item["side"] == side and item["quantity"] == leg["quantity"]), None)
        if candidate is None:
            return ["Option type, side, or quantity differs from request"]
        matched[leg["id"]] = candidate
        remaining.remove(candidate)
    for leg in expected:
        actual_leg = matched[leg["id"]]
        if leg.get("metricType") == "ChildLeg" and leg.get("childMetricType") == "ParentOffset":
            parent = matched[leg["parentId"]]
            if abs(actual_leg["strike"] - parent["strike"] - leg["parentStrikeOffset"]) > 0.001:
                issues.append("Linked strike offset differs from request")
        if leg["daysToExpiration"] == 0:
            date = pd.Timestamp(row["dateOpened"])
            if actual_leg["expiry"] != f"{date.strftime('%b')} {date.day}":
                issues.append("Requested 0DTE but actual expiration differs")
    quantities = sum(leg["quantity"] for leg in expected) * row["numberOfContracts"]
    for field in ["openingFees", "closingFees"]:
        if abs(row[field] - quantities * parameters[field]) > 0.01:
            issues.append(field + " differs from modeled contract fees")
    cap = parameters.get("maxAllocationAmount")
    if cap and row["buyingPowerRequired"] > cap + 0.01:
        issues.append("Buying power exceeds allocation cap")
    return issues


def planned_loss(row):
    """Expiration payoff loss for a single long option or same-expiry vertical."""
    legs = parse_legs(row["legs"])
    premium = row["premiumPerContract"]
    if len(legs) == 1 and legs[0]["side"] == "BTO":
        risk = -premium
    elif (len(legs) == 2 and legs[0]["type"] == legs[1]["type"]
          and legs[0]["expiry"] == legs[1]["expiry"]):
        risk = -premium if premium < 0 else abs(legs[0]["strike"] - legs[1]["strike"]) * 100 - premium
    else:
        return None
    return risk * row["numberOfContracts"] + row["openingFees"] + row["closingFees"]


def execution_issues(parameters, row):
    """Check structural/temporal consistency, not historical chain availability.

    Nonexact requests use the next expiration at or beyond the requested DTE.
    Delays over seven days are flagged for review, although the provider allows
    unbounded extension. Independent chains are needed to verify availability.
    """
    issues = []
    opened = datetime.fromisoformat(row["dateOpened"])
    closed = datetime.fromisoformat(row["dateClosed"])
    end = datetime.fromisoformat(parameters["rangeEnd"])
    if closed < opened or closed > end:
        issues.append("Exit falls outside backtest interval")
    if (closed - opened).days != row["daysInTrade"]:
        issues.append("Days in trade differs from calendar dates")
    actual = parse_legs(row["legs"])
    remaining = list(actual)
    expirations = []
    for expected in parameters["legs"]:
        side = "BTO" if expected["buySell"] == "Buy" else "STO"
        leg = next((item for item in remaining if item["type"] == expected["type"][0]
                    and item["side"] == side), None)
        if leg is None:
            continue
        remaining.remove(leg)
        expiry = datetime.strptime(f"{opened.year} {leg['expiry']}", "%Y %b %d")
        if expiry < opened:
            expiry = expiry.replace(year=opened.year + 1)
        dte = (expiry - opened).days
        target = expected["daysToExpiration"]
        tolerance = 0 if parameters.get("useExactDte") or target == 0 else 7
        if not target <= dte <= target + tolerance:
            issues.append("Actual DTE outside requested expiration tolerance")
        expirations.append((target, expiry))
    for target, expiry in expirations:
        for other_target, other_expiry in expirations:
            if target == other_target and expiry != other_expiry:
                issues.append("Same-DTE legs have different expirations")
            if target < other_target and expiry >= other_expiry:
                issues.append("Calendar expiration ordering differs from request")
    reason = row["reasonClosed"]
    rule = parameters["exitConditions"][0]
    if reason == "Backtest Completed":
        if not parameters["closeOpenTradesAtTestCompletion"] or not 0 <= (end - closed).days <= 4:
            issues.append("Unexpected backtest-boundary exit")
    else:
        expiry = min((expiry for _, expiry in expirations), default=end)
        target_hold = rule.get("exitAtDit")
        if target_hold is not None:
            earliest = min(target_hold, (expiry - opened).days)
            # Weekends/holidays may delay calendar-day holding exits. This is
            # a bounded consistency check, not a full exchange-calendar audit.
            if not earliest <= (closed - opened).days <= target_hold + 4:
                issues.append("Holding period outside requested window")
        if "exitAtDte" in rule and (expiry - closed).days != rule["exitAtDte"]:
            issues.append("Exit DTE differs from requested rule")
        if row["timeClosed"] != rule["exitTime"]:
            # Provider closes early sessions at 12:59, or expiration at 15:59.
            half_day = ((closed.month == 11 and closed.weekday() == 4 and 23 <= closed.day <= 29)
                        or (closed.month == 12 and closed.day == 24)
                        or (closed.month == 7 and closed.day in {2, 3}))
            allowed = (half_day and row["timeClosed"] == "12:59:00"
                       and reason in {"Exited at Specified DTE", "Exited at DIT"})
            allowed |= reason == "Exited at Specified DTE" and row["timeClosed"] == "15:59:00"
            if not allowed:
                issues.append("Exit time differs from rule without recognized session exception")
    return sorted(set(issues))


def validate_job(job, result, log):
    issues = []
    rows = trade_rows(log)
    parameters = job["spec"]["parameters"]
    if len(rows) != log.get("totalCount", len(rows)):
        issues.append("Trade log is paginated or incomplete")
    if len(rows) != result.get("numberOfTrades"):
        issues.append("Trade count differs from reported result")
    if abs(sum(row["profit"] for row in rows) - result["profit"]) > 0.51:
        issues.append("Trade profits do not reconcile to aggregate profit")
    dates = set((parameters.get("specificEntryDays") or "").split(","))
    for row in rows:
        issues.extend(structure_issues(parameters, row))
        issues.extend(execution_issues(parameters, row))
        if dates != {""} and row["dateOpened"] not in dates:
            issues.append("Unexpected entry date: " + row["dateOpened"])
        if row["timeOpened"] != parameters["entryTime"]:
            issues.append("Unexpected entry time: " + row["timeOpened"])
        if not row.get("wasAdjusted") and row["numberOfContracts"] > parameters["maxContractsPerTrade"]:
            issues.append("Position exceeded contract cap")
        if parameters["legs"][0]["daysToExpiration"] == 0:
            day = pd.Timestamp(row["dateOpened"])
            expiry = f"{day.strftime('%b')} {day.day} "
            if any(expiry not in leg for leg in row["legs"].split("|")):
                issues.append("Requested 0DTE but log contains another expiration")
        if job["spec"]["name"].startswith("budget-"):
            risk = planned_loss(row)
            budget = parameters["startingFunds"] * parameters["allocationPercentage"] / 100
            if risk is None or risk > budget + 0.01:
                issues.append("Planned expiration loss plus fees exceeds initial account risk budget")
    return sorted(set(issues))


def summarize(output):
    output = Path(output)
    records, audit, batches = [], [], []
    for manifest in output.glob("*-jobs.json"):
        jobs = json.loads(manifest.read_text(encoding="utf-8"))
        batches.append({"batch": manifest.stem, "expected": len(jobs),
                        "states": dict(Counter(job["state"] for job in jobs)),
                        "errors": [{"name": job["spec"]["name"], "error": job.get("error")}
                                   for job in jobs if job["state"] in {"failed", "uncertain", "cancelled"}]})
        for job in jobs:
            if job["state"] != "done":
                continue
            name = job["spec"]["name"]
            result = json.loads((output / (name + "-results.json")).read_text(encoding="utf-8"))
            log = json.loads((output / (name + "-trades.json")).read_text(encoding="utf-8"))
            issues = validate_job(job, result, log)
            losses = [planned_loss(row) for row in trade_rows(log)] if name.startswith("budget-") else []
            audit.append({"name": name, "issues": issues, "runId": job["runId"],
                          "max_planned_loss": max((v for v in losses if v is not None), default=None),
                          "worst_realized_loss": max(0, max((-row["profit"] for row in trade_rows(log)), default=0))})
            record = {"name": name, "batch": manifest.stem, "runId": job["runId"],
                      "signal": job["spec"].get("signal"), "audit_pass": not issues}
            record.update({key: value for key, value in result.items()
                           if isinstance(value, (str, int, float, bool))})
            for year in result.get("annualMetrics", []):
                record["profit_" + str(year["year"])] = year["profitLoss"]
            record["requested_entries"] = job["spec"].get("requested_entries")
            records.append(record)
    frame = pd.DataFrame(records).drop_duplicates("runId")
    frame.to_csv(output / "summary.csv", index=False)
    write_json(output / "trade-audit.json", audit)
    write_json(output / "batch-coverage.json", batches)
    print("Batch coverage:", json.dumps(batches))
    print("Results:", len(frame), "Audit failures:", sum(bool(row["issues"]) for row in audit))
    if not frame.empty:
        display = pd.DataFrame(frame[["name", "profit", "numberOfTrades", "maxDrawdownPercentage", "audit_pass"]])
        print(display.sort_values(by="profit", ascending=False).to_string(index=False))
    return frame


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    summarize(parser.parse_args().output)
