"""Retrieve EOD equity evidence for each frozen baseline-cost candidate."""

import argparse
import json
from pathlib import Path
import time

from research.batch import write_json
from research.mcp_client import OmegaClient


def check_curve(curve, result):
    points = [dict(zip(curve["pointColumns"], row)) for row in curve["points"]]
    dates = [point["date"] for point in points]
    if not points or dates != sorted(set(dates)):
        raise ValueError("Equity curve must contain unique dates in ascending order")
    peak = result["startingFunds"]
    worst = 0.0
    for point in points:
        peak = max(peak, point["netLiquidity"])
        worst = min(worst, 100 * (point["netLiquidity"] / peak - 1))
    return {
        "points": len(points),
        "ending_equity_matches": abs(points[-1]["netLiquidity"] - result["endingFunds"]) < 0.01,
        "computed_max_drawdown": round(worst, 2),
        "reported_max_drawdown": result["maxDrawdownPercentage"],
        "drawdown_matches": abs(worst - result["maxDrawdownPercentage"]) <= 0.011,
        "scope": "Recalculation from provider EOD series; not independent quote validation",
    }


def fetch(output):
    frozen = json.loads((output / "frozen-holdout.json").read_text())
    names = {"holdout-" + c["name"] + "-10000-0.03" for c in frozen["candidates"]}
    client = OmegaClient(output)
    deadline = time.monotonic() + 1800
    audits = {}
    try:
        while names:
            if time.monotonic() > deadline:
                raise TimeoutError("Holdout candidates did not all complete within 30 minutes")
            jobs = json.loads((output / "holdout-jobs.json").read_text())
            available = [j for j in jobs if j["spec"]["name"] in names and j["state"] == "done"]
            for job in available:
                name = job["spec"]["name"]
                path = output / (name + "-curve.json")
                if path.exists():
                    curve = json.loads(path.read_text())
                else:
                    curve = client.call("get_equity_curve", {"parameters": {
                        "runId": job["runId"], "seriesStart": frozen["test_start"],
                        "seriesEnd": frozen["test_end"],
                    }})
                    write_json(path, curve)
                result = json.loads((output / (name + "-results.json")).read_text())
                audits[name] = check_curve(curve, result)
                write_json(output / "curve-audit.json", audits)
                names.remove(name)
                print("CURVE", name, audits[name], flush=True)
            if names:
                time.sleep(10)
        portfolio_curve = json.loads((output / "portfolio-curve.json").read_text())
        portfolio_result = json.loads((output / "portfolio-results.json").read_text())
        audits["portfolio"] = check_curve(portfolio_curve, portfolio_result)
        write_json(output / "curve-audit.json", audits)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    fetch(parser.parse_args().output)
