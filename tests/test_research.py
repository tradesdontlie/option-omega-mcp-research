import unittest
from datetime import time
import hashlib
import json
from pathlib import Path
import tempfile

import pandas as pd

from research.mcp_client import unwrap
from research.signals import session_features
from research.batch import strategy, next_pending, validate_spec
from research.evidence import parse_legs, execution_issues
from research.holdout import pinned_specs
from research.curves import check_curve


class ResearchTests(unittest.TestCase):
    def test_equity_audit_includes_open_position_drawdown(self):
        result = {"startingFunds": 100, "endingFunds": 120, "maxDrawdownPercentage": -50}
        curve = {"pointColumns": ["date", "netLiquidity"],
                 "points": [["2024-01-02", 200], ["2024-01-03", 100], ["2024-01-04", 120]]}
        check = check_curve(curve, result)
        self.assertTrue(check["drawdown_matches"])
        self.assertTrue(check["ending_equity_matches"])

    def test_holdout_resume_rejects_changed_signal_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            files = {}
            for name, text in [("holdout-specs.json", "[]"),
                               ("frozen-holdout.json", "{}"), ("nq-features.csv", "date\n")]:
                path = output / name
                path.write_text(text)
                files[str(path)] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            (output / "holdout-input-integrity.json").write_text(json.dumps({"files": files}))
            self.assertEqual(pinned_specs(output, {}), [])
            (output / "nq-features.csv").write_text("date\n2025-01-02\n")
            with self.assertRaisesRegex(RuntimeError, "Frozen holdout input changed"):
                pinned_specs(output, {})

    def test_audit_rejects_wrong_expiration_and_impossible_exit(self):
        params = strategy("call_debit")
        row = {"dateOpened": "2023-01-03", "dateClosed": "2099-01-01",
               "timeOpened": "10:29:00", "timeClosed": "09:31:00",
               "daysInTrade": 2, "reasonClosed": "Exited at Specified Time",
               "legs": "1 Jan 10 265 C BTO 3.94 | 1 Feb 10 270 C STO 1.87"}
        issues = execution_issues(params, row)
        self.assertIn("Same-DTE legs have different expirations", issues)
        self.assertIn("Exit falls outside backtest interval", issues)
        row.update(dateClosed="2023-01-05", timeClosed="15:49:00",
                   legs="1 Jan 10 265 C BTO 3.94 | 1 Jan 10 270 C STO 1.87")
        self.assertEqual(execution_issues(params, row), [])

    def test_uncertain_submission_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            next_pending([{"state": "submitting", "spec": {"name": "uncertain"}}])

    def test_empty_dates_cannot_disable_signal(self):
        with self.assertRaises(ValueError):
            validate_spec({"name": "empty", "signal": "morning_up",
                           "parameters": {"specificEntryDays": ""}})

    def test_trade_leg_parser_preserves_strikes_and_sides(self):
        legs = parse_legs("1 Jan 9 403 C BTO 3.40 | 1 Jan 9 408 C STO 1.39")
        self.assertEqual([leg["strike"] for leg in legs], [403, 408])
        self.assertEqual([leg["side"] for leg in legs], ["BTO", "STO"])
        with self.assertRaises(ValueError):
            parse_legs("unrecognized provider format")

    def test_credit_wings_limit_the_short_option_risk(self):
        self.assertEqual(strategy("put_credit")["legs"][1]["parentStrikeOffset"], -5)
        self.assertEqual(strategy("call_credit")["legs"][1]["parentStrikeOffset"], 5)

    def test_tool_errors_are_not_results(self):
        with self.assertRaises(RuntimeError):
            unwrap({"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]})
        self.assertEqual(unwrap({"structuredContent": {"remaining": 20}}), {"remaining": 20})
        self.assertEqual(unwrap({"content": [{"type": "text", "text": '{"used": 1}'}]}), {"used": 1})

    def bars(self):
        ts = pd.date_range("2024-01-02 09:30", periods=390, freq="min", tz="America/New_York")
        close = pd.Series(range(390), dtype=float) / 10 + 100
        return pd.DataFrame({"timestamp": ts, "open": close, "high": close + 1,
                             "low": close - 1, "close": close, "volume": 100.0})

    def test_entry_features_ignore_later_bars(self):
        bars = self.bars()
        first = session_features(bars)
        bars.loc[bars.timestamp.dt.time >= time(10, 28),
                 ["open", "high", "low", "close"]] *= 10
        second = session_features(bars)
        for column in ["signal_close", "vwap", "sigma", "ema50", "momentum15", "morning_return"]:
            self.assertEqual(first.iloc[0][column], second.iloc[0][column])

    def test_incomplete_signal_window_is_excluded(self):
        bars = self.bars().drop(index=20)
        self.assertTrue(session_features(bars).empty)


if __name__ == "__main__":
    unittest.main()
