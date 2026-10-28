"""Exercise saved backtests, optimizers, and portfolios on research-owned items."""

import argparse
import json
from pathlib import Path
import time

from research.batch import write_json
from research.mcp_client import OmegaClient


def save_once(client, output, job):
    name = job["spec"]["name"]
    path = output / (name + "-saved.json")
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if "savedBacktestId" not in previous:
            raise RuntimeError("Reconcile uncertain save before retrying: " + name)
        return previous["savedBacktestId"]
    write_json(path, {"state": "submitting", "runId": job["runId"]})
    result = client.call("save_backtest", {
        "runId": job["runId"], "name": "TDL Research - " + name.removeprefix("dev-"),
        "description": "Exploratory 2023-2024 research. Not validated for live trading.",
        "tags": ["TDL-research", "development"], "isShared": False,
    })
    write_json(path, result)
    return result["savedBacktestId"]


def optimizers(client, output):
    jobs = json.loads((output / "development-jobs.json").read_text(encoding="utf-8"))
    by_name = {job["spec"]["name"]: job for job in jobs}
    definitions = [
        ("dev-morning_up-put_credit-0dte", [
            ("legs.0.delta", 15, 10, 35),
            ("legs.1.parentStrikeOffset", -5, 2, -1),
            ("entryTime", 629, 60, 749),
            ("exitConditions.0.exitTime", 869, 40, 949),
        ]),
        ("dev-morning_up-call_long-7dte", [
            ("legs.0.delta", 35, 15, 65),
            ("legs.0.daysToExpiration", 7, 7, 21),
            ("exitConditions.0.exitAtDit", 1, 1, 3),
            ("entryTime", 629, 60, 749),
        ]),
        ("dev-all-put_credit-0dte", [
            ("legs.0.delta", 15, 10, 35),
            ("legs.1.parentStrikeOffset", -5, 2, -1),
            ("entryTime", 629, 60, 749),
            ("exitConditions.0.exitTime", 869, 40, 949),
        ]),
    ]
    task_path = output / "optimization-tasks.json"
    tasks = json.loads(task_path.read_text(encoding="utf-8")) if task_path.exists() else []
    for name, dimensions in definitions:
        existing = next((task for task in tasks if task["name"] == name), None)
        if existing:
            wait_optimizer(client, output, existing, tasks, task_path)
            continue
        job = by_name[name]
        if job["state"] != "done":
            raise RuntimeError("Wait for baseline to complete: " + name)
        saved_id = save_once(client, output, job)
        inventory = client.call("list_optimizer_parameters", {"savedBacktestId": saved_id})
        write_json(output / (name + "-optimizer-parameters.json"), inventory)
        paths = {item["parameterPath"] for item in inventory["parameters"]}
        if not all(dimension[0] in paths for dimension in dimensions):
            raise RuntimeError("Optimizer dimension unavailable")
        params = {
            "savedBacktestId": saved_id,
            "dimensions": [{"parameterPath": path, "start": start, "step": step, "end": end}
                           for path, start, step, end in dimensions],
            "rangeStart": "2023-01-01", "rangeEnd": "2023-12-31", "startingFunds": 10000,
        }
        task = {"name": name, "savedBacktestId": saved_id,
                "request": params, "state": "submitting"}
        tasks.append(task)
        write_json(task_path, tasks)
        started = client.call("run_optimizer", params)
        task.update(start=started, state="started",
                    next_poll=time.time() + (started.get("pollAfterMs") or 60000) / 1000)
        write_json(task_path, tasks)
        print("OPTIMIZER START", name, started, flush=True)
        wait_optimizer(client, output, task, tasks, task_path)


def wait_optimizer(client, output, task, tasks, task_path):
    if task["state"] == "submitting":
        raise RuntimeError("Reconcile uncertain optimization before retrying: " + task["name"])
    deadline = time.monotonic() + 1800
    task.setdefault("next_poll", time.time() + 60)
    while task["state"] == "started":
        if time.monotonic() > deadline:
            raise TimeoutError("Optimizer stalled; retain ID and investigate before resuming")
        if time.time() < task["next_poll"]:
            time.sleep(min(10, task["next_poll"] - time.time()))
            continue
        optimization_id = task["start"]["optimizationId"]
        status = client.call("get_optimization_status", {"optimizationId": optimization_id})
        task["last_status"] = status
        task["next_poll"] = time.time() + max(15, (status.get("pollAfterMs") or 0) / 1000)
        if not status.get("pollAfterMs") and status["status"] == "complete":
            result = client.call("get_optimization_results", {
                "optimizationId": optimization_id, "limit": 500,
            })
            write_json(output / (task["name"] + "-optimization-results.json"), result)
            task["state"] = "done"
            print("OPTIMIZER DONE", task["name"], flush=True)
        elif status["status"] in {"failed", "cancelled"}:
            task["state"] = status["status"]
            print("OPTIMIZER FAILURE", status, flush=True)
        write_json(task_path, tasks)


def portfolio(client, output):
    jobs = json.loads((output / "development-jobs.json").read_text(encoding="utf-8"))
    by_name = {job["spec"]["name"]: job for job in jobs}
    names = ["dev-morning_up-put_credit-0dte", "dev-morning_up-call_long-7dte"]
    saved_ids = [save_once(client, output, by_name[name]) for name in names]
    task_path = output / "portfolio-task.json"
    if task_path.exists():
        task = json.loads(task_path.read_text(encoding="utf-8"))
    else:
        params = {
            "strategies": [{"savedBacktestId": saved_id, "maxContractsPerTrade": 1,
                            "ignoreMarginRequirements": False, "allocationPercentage": 10,
                            "maxAllocationAmount": 1000} for saved_id in saved_ids],
            "rangeStart": "2023-01-01", "rangeEnd": "2024-12-31",
            "startingFunds": 10000, "closeOpenTradesAtTestCompletion": True,
            "monthlyWithdrawalMode": "none",
        }
        task = {"request": params, "state": "submitting"}
        write_json(task_path, task)
        task.update(start=client.call("run_portfolio", {"parameters": params}), state="started")
        write_json(task_path, task)
    if "start" not in task:
        raise RuntimeError("Reconcile uncertain portfolio submission before retrying")
    run_id = task["start"]["runId"]
    deadline = time.monotonic() + 1800
    time.sleep((task["start"].get("pollAfterMs") or 5000) / 1000)
    while True:
        status = client.call("get_portfolio_status", {"runId": run_id})
        if status["status"] == "complete":
            break
        if status["status"] in {"failed", "cancelled"} or time.monotonic() > deadline:
            raise RuntimeError(json.dumps(status))
        time.sleep(max(5, (status.get("pollAfterMs") or 5000) / 1000))
    for tool, suffix, args in [
        ("get_portfolio_results", "results", {"runId": run_id}),
        ("get_trade_log", "trades", {"runId": run_id, "limit": 5000}),
        ("get_equity_curve", "curve", {"parameters": {"runId": run_id,
                                                     "seriesStart": "2023-01-01",
                                                     "seriesEnd": "2024-12-31"}}),
    ]:
        write_json(output / f"portfolio-{suffix}.json", client.call(tool, args))
    if "savedPortfolioId" not in task:
        if task.get("save_state") == "submitting":
            raise RuntimeError("Reconcile uncertain portfolio save before retrying")
        task["save_state"] = "submitting"
        write_json(task_path, task)
        saved = client.call("save_portfolio", {
            "runId": run_id, "name": "TDL Research - NQ morning portfolio",
            "description": "Exploratory shared-capital test; correlated bullish strategies.",
            "tags": ["TDL-research"], "isShared": False,
        })
        task.update(saved)
        task["save_state"] = "done"
        write_json(task_path, task)
    print("PORTFOLIO", task, flush=True)


def coverage(client, output):
    """Metadata changes and no-op cancellation tests affect only our records."""
    smoke = json.loads((output / "saved-smoke.json").read_text(encoding="utf-8"))["savedBacktestId"]
    smoke_jobs = json.loads((output / "smoke-v2-jobs.json").read_text(encoding="utf-8"))
    smoke_run = next(job["runId"] for job in smoke_jobs if job["state"] == "done")
    ptask = json.loads((output / "portfolio-task.json").read_text(encoding="utf-8"))
    task = json.loads((output / "optimization-tasks.json").read_text(encoding="utf-8"))[0]
    calls = [
        ("list_backtests", {"limit": 100}),
        ("get_saved_backtest", {"savedBacktestId": smoke}),
        ("edit_saved_backtest", {"savedBacktestId": smoke,
                                "description": "Verified three explicit entry dates, fills, and fees. Workflow test only."}),
        ("replace_saved_backtest", {"runId": smoke_run, "savedBacktestId": smoke}),
        ("cancel_backtest", {"runId": smoke_run}),
        ("get_equity_curve", {"parameters": {"savedBacktestId": smoke,
                                             "seriesStart": "2024-01-01", "seriesEnd": "2024-01-31"}}),
        ("list_optimizations", {"savedBacktestId": task["savedBacktestId"]}),
        ("cancel_optimization", {"optimizationId": task["start"]["optimizationId"]}),
        ("list_portfolios", {"limit": 100}),
        ("get_saved_portfolio", {"savedPortfolioId": ptask["savedPortfolioId"]}),
        ("edit_saved_portfolio", {"savedPortfolioId": ptask["savedPortfolioId"],
                                 "description": "Verified shared-capital portfolio workflow. Development-only; both strategies are bullish."}),
        ("replace_saved_portfolio", {"savedPortfolioId": ptask["savedPortfolioId"],
                                    "runId": ptask["start"]["runId"]}),
        ("cancel_portfolio", {"runId": ptask["start"]["runId"]}),
        ("get_usage_limits", {}),
    ]
    evidence = []
    for tool, args in calls:
        try:
            evidence.append({"tool": tool, "response": client.call(tool, args)})
        except Exception as error:
            evidence.append({"tool": tool, "error": str(error)})
        write_json(output / "workflow-coverage.json", evidence)
        print("WORKFLOW", tool, "error" if "error" in evidence[-1] else "ok", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("mode", choices=["optimizers", "portfolio", "coverage"])
    args = parser.parse_args()
    client = OmegaClient(args.output)
    try:
        {"optimizers": optimizers, "portfolio": portfolio, "coverage": coverage}[args.mode](client, args.output)
    finally:
        client.close()
