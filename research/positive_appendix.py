"""Build a local appendix of every profitable historical backtest in the saved research artifacts.

The appendix is a presentation aid for the article revision of 2026-10-25. It reads the
existing result files only; it never starts a backtest and never changes the original
screening criteria. A profitable row here is a profitable historical backtest, not a
validated or recommended strategy.

Usage:
    python -m research.positive_appendix DATA_ROOT OUTPUT_DIR
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

STUDIES = [
    ("2026-10-20", "NQ signal study (QQQ options and cross-asset checks)"),
    ("2026-10-20-asset-search", "Per-asset search (90-cell grid per asset)"),
    ("2026-10-20-indicators", "Native indicators with and without NQ confirmation"),
    ("2026-10-20-volatility", "Native volatility-ratio filters"),
    ("2026-10-22-mass-sweep", "Mass optimizer sweep (9,072 cells)"),
]

# Rows with these traits are kept in the appendix but flagged, and must not be featured.
OUTLIER_NOTE = ("Unexplained AAPL development outlier pending trade-level audit "
                "(possible split-adjustment artifact). Not featured.")
OUTLIER_PROFIT = 50_000

PHASE_ORDER = ["development grid", "training grid", "screen", "training", "development", "pilot",
               "validation", "holdout", "robustness", "confirmation"]
FIELDS = ["study", "asset", "phase", "variant", "name", "rule", "window", "account_and_costs", "profit",
          "trades", "max_drawdown_pct", "profit_factor", "profit_without_best_trade", "context", "flag", "source"]


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _money(value: float | None) -> str:
    if value is None:
        return ""
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    return f"{sign}${abs(value):,.0f}"


def describe(params: dict[str, Any] | None) -> str:
    """Plain-language rule from Option Omega backtest parameters."""
    if not params:
        return "rule not recorded"
    legs = params.get("legs") or []
    parts = []
    for leg in legs:
        side = "Buy" if leg.get("buySell") == "Buy" else "Sell"
        how = f"{leg.get('delta')} delta" if leg.get("metricType", "Delta") == "Delta" and "delta" in leg else ""
        if "strikeOffset" in leg or "parentStrikeOffset" in leg:
            how = how or f"{leg.get('parentStrikeOffset', leg.get('strikeOffset'))} pt offset"
        parts.append(f"{side} {leg.get('type', '').lower()} {leg.get('daysToExpiration')} DTE {how}".strip())
    rule = "; ".join(parts) if parts else "legs not recorded"
    exits = (params.get("exitConditions") or [{}])[0]
    ex = []
    if exits.get("maxProfit") is not None:
        ex.append(f"target +{exits['maxProfit']:g}%")
    if exits.get("maxLoss") is not None:
        ex.append(f"stop -{exits['maxLoss']:g}%")
    if exits.get("exitAtDte") is not None:
        ex.append(f"exit at {exits['exitAtDte']} DTE")
    if exits.get("exitAtDit") is not None:
        ex.append(f"exit after {exits['exitAtDit']} days in trade")
    entry = str(params.get("entryTime", ""))[:5]
    filters = []
    if params.get("entrySmaCondition"):
        filters.append(f"{'above' if params['entrySmaCondition'] == 'AboveSma' else 'below'} "
                       f"SMA{params.get('entrySmaLength', '')}")
    for key, label in (("entryMaxHv30Iv30Ratio", "HV30/IV30 <="), ("entryMinHv30Iv30Ratio", "HV30/IV30 >="),
                       ("entryMaxIv9DIv30Ratio", "IV9/IV30 <="), ("entryMinIv9DIv30Ratio", "IV9/IV30 >="),
                       ("entryMaxVix9DVixRatio", "VIX9D/VIX <="), ("entryMinVix9DVixRatio", "VIX9D/VIX >="),
                       ("entryMaxIvPercentile", "IV percentile <=")):
        if params.get(key) is not None:
            filters.append(f"{label} {params[key]:g}")
    for key in params:
        if key.lower().startswith("entry") and "rsi" in key.lower():
            filters.append(f"{key}={params[key]}")
        if key.lower().startswith("entry") and "ema" in key.lower():
            filters.append(f"{key}={params[key]}")
    if params.get("specificEntryDays"):
        filters.append("explicit entry-date list")
    text = f"{rule}; {', '.join(ex)}"
    if entry:
        text += f"; enter {entry} ET"
    if filters:
        text += f"; filters: {', '.join(filters)}"
    return text


def costs(params: dict[str, Any] | None, starting: Any = None) -> str:
    if not params:
        return f"${_num(starting) or 0:,.0f} start" if starting else ""
    funds = _num(params.get("startingFunds")) or _num(starting) or 0
    cap = params.get("maxAllocationAmount")
    slip = params.get("entrySlippage")
    fees = params.get("openingFees")
    out = f"${funds:,.0f}"
    if cap is not None:
        out += f", cap ${cap:,.0f}"
    if slip is not None:
        out += f", slippage ${slip:.2f}/side"
    if fees is not None:
        out += f", fees ${fees:g}/contract/side"
    return out


def window(params: dict[str, Any] | None) -> str:
    if not params or not params.get("rangeStart"):
        return "window not recorded"
    return f"{params['rangeStart']} to {params.get('rangeEnd', '?')}"


def apply_coordinate(params: dict[str, Any], coordinate: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return a copy of grid base parameters with an optimizer cell's coordinate applied."""
    out = json.loads(json.dumps(params))
    for c in coordinate:
        path = c["parameterPath"].split(".")
        target: Any = out
        for key in path[:-1]:
            target = target[int(key)] if isinstance(target, list) else target.setdefault(key, {})
        last = path[-1]
        value = c.get("time") if c["parameterPath"] == "entryTime" and c.get("time") else c["value"]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value
    return out


def spec_index(study_dir: Path) -> dict[str, dict[str, Any]]:
    """Map experiment name to parameters from every spec-like JSON in a study folder."""
    index: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("name"), str) and isinstance(node.get("parameters"), dict):
                index.setdefault(node["name"], node["parameters"])
            for v in node.values():
                if isinstance(v, (dict, list)):
                    visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    for path in sorted(study_dir.glob("*.json")):
        if path.name.endswith(("-trades.json", "-curve.json")) or path.stat().st_size > 20_000_000:
            continue
        try:
            visit(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return index


def flag_for(asset: str, phase: str, profit: float) -> str:
    if asset == "AAPL" and phase in ("development grid", "training grid", "training", "screen", "development") \
            and profit >= OUTLIER_PROFIT:
        return OUTLIER_NOTE
    return ""


def row(study: str, asset: str, phase: str, variant: str, name: str, params: dict[str, Any] | None,
        result: dict[str, Any], source: str, context: str = "", starting: Any = None) -> dict[str, Any]:
    profit = _num(result.get("profit")) or 0.0
    return {
        "study": study, "asset": asset, "phase": phase, "variant": variant, "name": name,
        "rule": describe(params), "window": window(params), "account_and_costs": costs(params, starting),
        "profit": profit, "trades": int(_num(result.get("trades", result.get("numberOfTrades"))) or 0),
        "max_drawdown_pct": _num(result.get("drawdown", result.get("maxDrawdownPercentage"))),
        "profit_factor": _num(result.get("profit_factor")),
        "profit_without_best_trade": _num(result.get("profit_without_best_trade")),
        "context": context, "flag": flag_for(asset, phase, profit), "source": source,
    }


def _confirmation_context(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Base, stress and small-account siblings for each confirmation finalist."""
    sib: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for r in rows:
        if r.get("phase") == "confirmation":
            prof = _num(r.get("profit")) or 0
            trades = int(_num(r.get("trades")) or 0)
            sib[(r["asset"], r.get("group", ""))][r.get("variant", "")] = f"{_money(prof) or '$0'} / {trades} trades"
    out = {}
    for key, v in sib.items():
        out[key] = "; ".join(f"{k}: {v[k]}" for k in ("base", "stress", "control", "small") if k in v)
    return out


def csv_study(study: str, study_dir: Path, csv_name: str) -> list[dict[str, Any]]:
    path = study_dir / csv_name
    with path.open(encoding="utf-8") as fh:
        raw = list(csv.DictReader(fh))
    specs = spec_index(study_dir)
    for r in raw:
        r["group"] = ""
    ctx = _confirmation_context(raw)
    out = []
    for r in raw:
        if (_num(r.get("profit")) or 0) <= 0:
            continue
        phase = r.get("phase") or "unknown"
        context = ctx.get((r["asset"], ""), "") if phase == "confirmation" else ""
        out.append(row(study, r["asset"], phase, r.get("variant", ""), r["name"], specs.get(r["name"]), r,
                       f"{study}/{csv_name}", context))
    return out


def asset_grids(study: str, study_dir: Path) -> list[dict[str, Any]]:
    tasks = json.loads((study_dir / "asset-optimization-tasks.json").read_text(encoding="utf-8"))
    base = {t["asset"]: t["selected"]["spec"]["parameters"] for t in tasks}
    out = []
    for path in sorted(study_dir.glob("grid-*-optimization-results.json")):
        asset = path.name.split("-")[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        for cell in data["cells"]:
            if (_num(cell.get("profit")) or 0) <= 0:
                continue
            params = apply_coordinate(base[asset], cell["coordinate"])
            out.append(row(study, asset, "training grid", "", f"grid-{asset} cell {cell['index']}", params, cell,
                           f"{study}/{path.name}"))
    return out


def nq_study(study: str, study_dir: Path) -> list[dict[str, Any]]:
    with (study_dir / "summary.csv").open(encoding="utf-8") as fh:
        raw = list(csv.DictReader(fh))
    specs = spec_index(study_dir)
    out = []
    for r in raw:
        if (_num(r.get("profit")) or 0) <= 0:
            continue
        name = r["name"]
        asset = (specs.get(name) or {}).get("underlying") or (name.split("-")[1] if "-" in name else "?")
        phase = r.get("batch", "").replace("-jobs", "") or "unknown"
        res = {"profit": r["profit"], "trades": r["numberOfTrades"], "drawdown": r["maxDrawdownPercentage"]}
        out.append(row(study, asset, phase, "", name, specs.get(name), res, f"{study}/summary.csv",
                       starting=r.get("startingFunds")))
    return out


def mass_sweep(study: str, study_dir: Path) -> list[dict[str, Any]]:
    protocol = json.loads((study_dir / "protocol.json").read_text(encoding="utf-8"))
    base = {s["name"]: s["parameters"] for s in protocol["specs"]}
    out = []
    dev = json.loads((study_dir / "ranked-all-development.json").read_text(encoding="utf-8"))
    for cell in dev:
        if cell.get("error") or (_num(cell.get("profit")) or 0) <= 0:
            continue
        params = apply_coordinate(base[cell["grid"]], cell["coordinate"])
        out.append(row(study, cell["asset"], "development grid", f"{cell['structure']} {cell['horizon']}",
                       f"{cell['grid']} cell {cell['index']}", params, cell, f"{study}/ranked-all-development.json"))
    for fname, phase in (("ranked-validation.json", "validation"), ("ranked-confirmation.json", "confirmation")):
        runs = json.loads((study_dir / fname).read_text(encoding="utf-8"))
        sib: dict[str, dict[str, str]] = defaultdict(dict)
        for r in runs:
            stem, _, variant = r["name"].rpartition("-")
            sib[stem][variant] = f"{_money(_num(r['profit'])) or '$0'} / {r['numberOfTrades']} trades"
        for r in runs:
            if (_num(r.get("profit")) or 0) <= 0:
                continue
            spec = r.get("spec") or {}
            stem, _, variant = r["name"].rpartition("-")
            context = ""
            if phase == "confirmation":
                context = "; ".join(f"{k}: {sib[stem][k]}" for k in ("base", "stress", "small") if k in sib[stem])
            out.append(row(study, spec.get("asset", "?"), phase, variant if phase == "confirmation" else
                           f"{spec.get('structure', '')} {spec.get('horizon', '')}".strip(), r["name"],
                           spec.get("parameters"), r, f"{study}/{fname}", context))
    return out


def collect(data_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows += nq_study("2026-10-20", data_root / "2026-10-20")
    rows += asset_grids("2026-10-20-asset-search", data_root / "2026-10-20-asset-search")
    rows += csv_study("2026-10-20-asset-search", data_root / "2026-10-20-asset-search", "asset-results.csv")
    rows += csv_study("2026-10-20-indicators", data_root / "2026-10-20-indicators", "asset-results.csv")
    rows += csv_study("2026-10-20-volatility", data_root / "2026-10-20-volatility", "asset-results.csv")
    rows += mass_sweep("2026-10-22-mass-sweep", data_root / "2026-10-22-mass-sweep")
    order = {s: i for i, (s, _) in enumerate(STUDIES)}
    rows.sort(key=lambda r: (order.get(r["study"], 99), r["asset"],
                             PHASE_ORDER.index(r["phase"]) if r["phase"] in PHASE_ORDER else 50,
                             -r["profit"]))
    return rows


def _md_cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value if value is not None else "").replace("|", "/")


def write(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "positive-results-appendix.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    titles = dict(STUDIES)
    md = ["# Appendix: every profitable historical backtest in the saved research", "",
          "Generated from the saved result files by `research/positive_appendix.py`. No backtest was run to make it.",
          "",
          "Read this first:",
          "",
          "- Every row is a **profitable historical, hypothetical backtest**. Many other tested configurations lost "
          "money and are not listed here; the full result files keep them.",
          "- A profitable row is not a validated, robust or recommended strategy. Development and training rows were "
          "chosen by an optimizer on data it could see. The original fixed screening criteria in each study report "
          "are unchanged, and this appendix does not re-label any row as passing them.",
          "- Rows from different studies use different rules, filters, windows and selection steps. Do not compare "
          "best profits across studies as if they were one test, and do not add P&Ls together: each test had its "
          "own account.",
          "- Confirmation rows list their base, stress and small-account siblings in `context`, including losing "
          "siblings.",
          f"- `flag` marks rows that must not be featured. {OUTLIER_NOTE}",
          "- Profit is net P&L in dollars after the modeled commissions and slippage, on the account shown. It is not "
          "a percentage return.", ""]
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for r in rows:
        counts[(r["study"], r["asset"], r["phase"])] += 1
    md += ["## Counts by study, asset and phase", "", "| Study | Asset | Phase | Profitable rows |",
           "| --- | --- | --- | --- |"]
    for (s, a, p), n in counts.items():
        md.append(f"| {s} | {a} | {p} | {n} |")
    md.append("")
    cols = ["phase", "variant", "name", "rule", "window", "account_and_costs", "profit", "trades",
            "max_drawdown_pct", "profit_without_best_trade", "context", "flag"]
    by_study: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_study[r["study"]].append(r)
    for study, srows in by_study.items():
        md += [f"## {study}: {titles.get(study, '')}", ""]
        by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in srows:
            by_asset[r["asset"]].append(r)
        for asset, arows in by_asset.items():
            md += [f"### {asset}", "", "| " + " | ".join(cols) + " |", "|" + " --- |" * len(cols)]
            for r in arows:
                vals = dict(r)
                vals["profit"] = _money(r["profit"])
                vals["profit_without_best_trade"] = _money(r["profit_without_best_trade"])
                md.append("| " + " | ".join(_md_cell(vals[c]) for c in cols) + " |")
            md.append("")
    (out_dir / "positive-results-appendix.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main(argv: list[str]) -> None:
    data_root, out_dir = Path(argv[0]), Path(argv[1])
    rows = collect(data_root)
    write(rows, out_dir)
    print(json.dumps({"rows": len(rows), "flagged": sum(1 for r in rows if r["flag"])}))


if __name__ == "__main__":
    main(sys.argv[1:])
