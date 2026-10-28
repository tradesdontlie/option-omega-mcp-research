import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from research.positive_appendix import OUTLIER_NOTE, apply_coordinate, collect, costs, describe, flag_for, write

PARAMS = {
    "underlying": "QQQ",
    "legs": [{"id": "primary", "daysToExpiration": 42, "type": "Call", "buySell": "Buy", "metricType": "Delta",
              "delta": 35}],
    "rangeStart": "2025-01-01", "rangeEnd": "2026-09-25", "startingFunds": 25000, "maxAllocationAmount": 1000,
    "entryTime": "10:29:00", "openingFees": 1.0, "entrySlippage": 0.1,
    "exitConditions": [{"maxProfit": 100, "maxLoss": 50, "exitAtDte": 7}],
    "entrySmaCondition": "AboveSma", "entrySmaLength": 50, "entryMaxHv30Iv30Ratio": 1.0,
}


class DescribeTests(unittest.TestCase):
    def test_rule_text_carries_structure_exits_and_filters(self):
        text = describe(PARAMS)
        self.assertIn("Buy call 42 DTE 35 delta", text)
        self.assertIn("target +100%", text)
        self.assertIn("stop -50%", text)
        self.assertIn("exit at 7 DTE", text)
        self.assertIn("enter 10:29 ET", text)
        self.assertIn("above SMA50", text)
        self.assertIn("HV30/IV30 <= 1", text)

    def test_costs_text(self):
        self.assertEqual(costs(PARAMS), "$25,000, cap $1,000, slippage $0.10/side, fees $1/contract/side")

    def test_coordinate_overrides_copy_not_original(self):
        coord = [{"parameterPath": "legs.0.delta", "value": 55},
                 {"parameterPath": "exitConditions.0.maxProfit", "value": 300},
                 {"parameterPath": "entryTime", "value": 869, "time": "14:29"}]
        out = apply_coordinate(PARAMS, coord)
        self.assertEqual(out["legs"][0]["delta"], 55)
        self.assertEqual(out["exitConditions"][0]["maxProfit"], 300)
        self.assertEqual(out["entryTime"], "14:29")
        self.assertEqual(PARAMS["legs"][0]["delta"], 35)

    def test_only_large_aapl_development_rows_are_flagged(self):
        self.assertEqual(flag_for("AAPL", "development grid", 145847), OUTLIER_NOTE)
        self.assertEqual(flag_for("AAPL", "confirmation", 145847), "")
        self.assertEqual(flag_for("AAPL", "development grid", 839), "")
        self.assertEqual(flag_for("GLD", "development grid", 145847), "")


class CollectTests(unittest.TestCase):
    def _fixture(self, root: Path) -> None:
        nq = root / "2026-10-20"
        nq.mkdir()
        with (nq / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "batch", "profit", "numberOfTrades",
                                               "maxDrawdownPercentage", "startingFunds"])
            w.writeheader()
            w.writerow({"name": "holdout-QQQ-a", "batch": "holdout-jobs", "profit": "10", "numberOfTrades": "3",
                        "maxDrawdownPercentage": "-1", "startingFunds": "10000"})
            w.writerow({"name": "holdout-QQQ-b", "batch": "holdout-jobs", "profit": "-10", "numberOfTrades": "3",
                        "maxDrawdownPercentage": "-1", "startingFunds": "10000"})
        (nq / "specs.json").write_text(json.dumps([{"name": "holdout-QQQ-a", "parameters": PARAMS}]))
        asset = root / "2026-10-20-asset-search"
        asset.mkdir()
        (asset / "asset-optimization-tasks.json").write_text(json.dumps(
            [{"asset": "QQQ", "selected": {"spec": {"parameters": PARAMS}}}]))
        (asset / "grid-QQQ-optimization-results.json").write_text(json.dumps({"cells": [
            {"index": 0, "coordinate": [{"parameterPath": "legs.0.delta", "value": 15}], "profit": 5,
             "numberOfTrades": 30, "maxDrawdownPercentage": -2},
            {"index": 1, "coordinate": [{"parameterPath": "legs.0.delta", "value": 55}], "profit": -5,
             "numberOfTrades": 30, "maxDrawdownPercentage": -2}]}))
        fields = ["name", "asset", "phase", "variant", "profit", "trades", "drawdown", "profit_factor",
                  "profit_without_best_trade"]
        for study in ("2026-10-20-asset-search", "2026-10-20-indicators", "2026-10-20-volatility"):
            d = root / study
            d.mkdir(exist_ok=True)
            with (d / "asset-results.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                w.writerow({"name": "confirmation-QQQ-base", "asset": "QQQ", "phase": "confirmation",
                            "variant": "base", "profit": "3188", "trades": "22", "drawdown": "-11.38",
                            "profit_factor": "1.5", "profit_without_best_trade": "2176"})
                w.writerow({"name": "confirmation-QQQ-stress", "asset": "QQQ", "phase": "confirmation",
                            "variant": "stress", "profit": "-5", "trades": "20", "drawdown": "-13",
                            "profit_factor": "0.9", "profit_without_best_trade": "-900"})
        mass = root / "2026-10-22-mass-sweep"
        mass.mkdir()
        (mass / "protocol.json").write_text(json.dumps({"specs": [{"name": "mass-QQQ-call_long-long",
                                                                    "parameters": PARAMS}]}))
        (mass / "ranked-all-development.json").write_text(json.dumps([
            {"index": 3, "grid": "mass-QQQ-call_long-long", "asset": "QQQ", "structure": "call_long",
             "horizon": "long", "coordinate": [], "profit": 7, "numberOfTrades": 40, "maxDrawdownPercentage": -4},
            {"index": 4, "grid": "mass-QQQ-call_long-long", "asset": "QQQ", "structure": "call_long",
             "horizon": "long", "coordinate": [], "profit": 9, "error": "bad"}]))
        (mass / "ranked-validation.json").write_text("[]")
        (mass / "ranked-confirmation.json").write_text(json.dumps([
            {"name": "mass-QQQ-x-confirmation-base", "profit": 50, "numberOfTrades": 12,
             "maxDrawdownPercentage": -3, "spec": {"asset": "QQQ", "parameters": PARAMS}},
            {"name": "mass-QQQ-x-confirmation-stress", "profit": -20, "numberOfTrades": 12,
             "maxDrawdownPercentage": -3, "spec": {"asset": "QQQ", "parameters": PARAMS}}]))

    def test_collect_keeps_only_profitable_rows_and_losing_sibling_context(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._fixture(root)
            rows = collect(root)
            self.assertTrue(all(r["profit"] > 0 for r in rows))
            names = [r["name"] for r in rows]
            self.assertIn("holdout-QQQ-a", names)
            self.assertNotIn("holdout-QQQ-b", names)
            self.assertIn("grid-QQQ cell 0", names)
            self.assertNotIn("grid-QQQ cell 1", names)
            self.assertNotIn("mass-QQQ-call_long-long cell 4", names)
            conf = [r for r in rows if r["name"] == "mass-QQQ-x-confirmation-base"][0]
            self.assertIn("stress: -$20 / 12 trades", conf["context"])
            vol = [r for r in rows if r["study"] == "2026-10-20-volatility"][0]
            self.assertIn("stress: -$5 / 20 trades", vol["context"])
            write(rows, root / "out")
            md = (root / "out" / "positive-results-appendix.md").read_text(encoding="utf-8")
            self.assertIn("not a validated, robust or recommended strategy", md)
            self.assertNotIn("—", md)


if __name__ == "__main__":
    unittest.main()
