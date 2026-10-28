"""Pre-entry NQ features; no future-return calculations or option pricing."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


def session_features(bars):
    frame = bars.copy()
    minute = frame.timestamp.dt.hour * 60 + frame.timestamp.dt.minute
    frame = frame.loc[(minute >= 570) & (minute < 960)].copy()
    frame["date"] = frame.timestamp.dt.strftime("%Y-%m-%d")
    minute = frame.timestamp.dt.hour * 60 + frame.timestamp.dt.minute
    morning = frame.loc[minute <= 627]
    rows = []
    for day, group in morning.groupby("date", sort=True):
        # Exactly 09:30 through 10:27. A 10:27 start-labeled bar is complete by
        # 10:28, leaving one full minute before the fixed 10:29 option entry.
        expected = pd.date_range(day + " 09:30", periods=58, freq="min", tz="America/New_York")
        if len(group) != 58 or list(group.timestamp) != list(expected):
            continue
        typical = (group.high + group.low + group.close) / 3
        volume = group.volume
        vwap = float((typical * volume).sum() / volume.sum())
        variance = float(((typical - vwap) ** 2 * volume).sum() / volume.sum())
        rows.append({
            "date": day, "signal_close": float(group.close.iloc[-1]),
            "vwap": vwap, "sigma": variance ** 0.5,
            "ema50": float(group.close.ewm(span=50, adjust=False).mean().iloc[-1]),
            "momentum15": float(group.close.iloc[-1] - group.close.iloc[-16]),
            "morning_return": float(group.close.iloc[-1] / group.open.iloc[0] - 1),
        })
    return pd.DataFrame(rows)


def generate(source, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    bars = pq.read_table(source).to_pandas()
    features = session_features(bars)
    minute = bars.timestamp.dt.hour * 60 + bars.timestamp.dt.minute
    regular = bars.loc[(minute >= 570) & (minute < 960)].copy()
    regular["date"] = regular.timestamp.dt.strftime("%Y-%m-%d")
    days = regular.groupby("date").agg(day_high=("high", "max"), day_low=("low", "min"),
                                       bar_count=("close", "size"))
    # Monday's range is used only on the next calendar day, Tuesday.
    dates = pd.to_datetime(features.date)
    previous = (dates - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    features["previous_high"] = previous.map(days.day_high)
    features["previous_low"] = previous.map(days.day_low)
    features["previous_bars"] = previous.map(days.bar_count)
    features["weekday"] = dates.dt.dayofweek
    features["all"] = True
    features["morning_up"] = features.morning_return > 0
    features["morning_down"] = features.morning_return < 0
    features["tuesday_breakout_up"] = ((features.weekday == 1) & (features.previous_bars == 390)
                                      & (features.signal_close > features.previous_high))
    features["tuesday_breakout_down"] = ((features.weekday == 1) & (features.previous_bars == 390)
                                        & (features.signal_close < features.previous_low))
    features["vwap_pullback_up"] = ((features.signal_close < features.vwap - features.sigma)
                                   & (features.momentum15 > 0))
    features["vwap_above_down"] = ((features.signal_close > features.vwap + features.sigma)
                                  & (features.momentum15 < 0))
    features["user_combo_up"] = (features.vwap_pullback_up
                                & (features.signal_close > features.ema50))
    features.to_csv(output / "nq-features.csv", index=False)
    with Path(source).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"source": str(Path(source).resolve()), "sha256": digest,
                "bytes": Path(source).stat().st_size, "rows": len(bars),
                "valid_signal_sessions": len(features), "entry_time": "10:29:00",
                "information_cutoff": "10:27:00", "timezone": "America/New_York",
                "roll_adjustment": "unverified", "bar_timestamp_convention": "unverified",
                "vwap": "volume-weighted typical minute price; session reset",
                "ema50": "session-reset 50-bar EMA; adjust=False",
                "purpose": "NQ signal dates for Option Omega research"}
    (output / "source-manifest.local.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args()
    generate(args.source, args.output)
