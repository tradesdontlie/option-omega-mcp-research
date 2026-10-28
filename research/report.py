"""Build a research evidence report from completed, audited local artifacts."""

import argparse
import contextlib
from datetime import datetime, timezone
import html
import json
from pathlib import Path

from research.evidence import summarize, trade_rows
from research.batch import write_json


def table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |",
                      "| " + " | ".join(["---"] * len(headers)) + " |"]
                     + ["| " + " | ".join(str(v) for v in row) + " |" for row in rows])


def money(value):
    return f"${value:,.0f}" if value >= 0 else f"−${-value:,.0f}"


def build(output):
    with (output / "audit-console.txt").open("w", encoding="utf-8") as stream:
        with contextlib.redirect_stdout(stream):
            frame = summarize(output)
    rows = {row["name"]: row for row in frame.to_dict("records")}
    batches = json.loads((output / "batch-coverage.json").read_text())
    pending = sum(count for batch in batches for state, count in batch["states"].items()
                  if state not in {"done", "failed", "cancelled"})
    if pending:
        raise RuntimeError(f"Research still has {pending} unfinished jobs")
    journal = [json.loads(line) for line in (output / "mcp-journal.jsonl").read_text().splitlines()]
    journal = [r for r in journal if r["server"] == "option-omega"]
    tools = json.loads((output / "tool-schemas.json").read_text())["data"][0]["tools"]
    coverage = []
    for name in sorted(tools):
        calls = [r for r in journal if r["tool"] == name]
        failures = sum("error" in r or r.get("response", {}).get("isError", False) for r in calls)
        coverage.append({"tool": name, "calls": len(calls), "successful": len(calls) - failures,
                         "errors": failures})
    write_json(output / "mcp-tool-coverage.json", coverage)
    audits = json.loads((output / "trade-audit.json").read_text())
    issues = [a for a in audits if a["issues"]]
    grids = [json.loads(p.read_text()) for p in output.glob("*-optimization-results.json")]
    cells = sum(len(g["cells"]) for g in grids)
    frozen = json.loads((output / "frozen-holdout.json").read_text())
    curve_audit = json.loads((output / "curve-audit.json").read_text())
    expected_curves = {"portfolio"} | {"holdout-" + c["name"] + "-10000-0.03"
                                      for c in frozen["candidates"]}
    if set(curve_audit) != expected_curves:
        raise RuntimeError("Equity curve evidence is incomplete")
    curve_failures = {name: check for name, check in curve_audit.items()
                      if not check["ending_equity_matches"] or not check["drawdown_matches"]}
    candidate_rows = []
    for c in frozen["candidates"]:
        base = rows["holdout-" + c["name"] + "-10000-0.03"]
        stress = rows["holdout-" + c["name"] + "-10000-0.1"]
        larger = rows["holdout-" + c["name"] + "-25000-0.03"]
        params = c["parameters"]
        candidate_rows.append([
            c["name"], params["legs"][0]["daysToExpiration"], params["entryTime"][:5],
            int(base["numberOfTrades"]), money(base["profit"]), money(stress["profit"]),
            f'{base["maxDrawdownPercentage"]:.2f}%',
            "pass" if base["audit_pass"] and stress["audit_pass"] and larger["audit_pass"] else "review",
            money(larger["profit"]), f'{larger["maxDrawdownPercentage"]:.2f}%',
        ])

    def results_group(prefix):
        return table(["Experiment", "Trades", "Net P&L", "EOD max DD", "Audit"],
                     [[name, int(r["numberOfTrades"]), money(r["profit"]),
                       f'{r["maxDrawdownPercentage"]:.2f}%', "pass" if r["audit_pass"] else "review"]
                      for name, r in rows.items() if name.startswith(prefix)])

    budget_rows = []
    for a in audits:
        if a["name"].startswith("budget-"):
            r = rows[a["name"]]
            budget_rows.append([a["name"], int(r["numberOfTrades"]), money(r["profit"]),
                                money(a["max_planned_loss"]) if a["max_planned_loss"] is not None else "no trades",
                                money(a["worst_realized_loss"]), "review" if a["issues"] else "pass"])
    assets = []
    for name, r in rows.items():
        if name.startswith("asset-"):
            trades = trade_rows(json.loads((output / (name + "-trades.json")).read_text()))
            assets.append([name.split("-")[1], len(trades),
                           trades[0]["dateOpened"] if trades else "none",
                           trades[-1]["dateOpened"] if trades else "none",
                           money(r["profit"]), "pass" if r["audit_pass"] else "review"])
    portfolio = json.loads((output / "portfolio-results.json").read_text())
    portfolio_log = json.loads((output / "portfolio-trades.json").read_text())
    p_trades = trade_rows(portfolio_log)
    portfolio_reconciles = (len(p_trades) == portfolio["numberOfTrades"]
                            and abs(sum(t["profit"] for t in p_trades) - portfolio["profit"]) < 0.01)
    write_json(output / "portfolio-audit.json", {
        "trade_count": len(p_trades), "reported_trade_count": portfolio["numberOfTrades"],
        "sum_trade_profit": sum(t["profit"] for t in p_trades),
        "reported_profit": portfolio["profit"], "reconciles": portfolio_reconciles,
        "scope": "Count and P&L reconciliation only; not an independent margin simulator",
    })
    report = f"""# Option Omega MCP: NQ-conditioned options research

Research date: 2026-10-20. Generated: {datetime.now(timezone.utc).isoformat()}.

This is an evidence report for a future sponsored demonstration, not a trading recommendation or a finished article. The sponsorship fee is $1,500. No live trades were placed.

## Main finding

The integration is useful for testing ideas, including disproving them. It has not established a trade-ready NQ-conditioned options edge. In the reserved period, the original NQ-up seven-DTE long-call rule returned {money(rows["holdout-morning_up-call_long-7dte-10000-0.03"]["profit"])}, versus {money(rows["holdout-all-call_long-7dte-10000-0.03"]["profit"])} for the unfiltered control. Development profitability did not reliably transfer. Execution costs, tiny samples, and account affordability substantially change the conclusions.

## What was tested

- {len(rows)} completed standalone backtests, {len(grids)} optimizer grids containing {cells} cells, and one shared-capital portfolio.
- {len(journal)} authenticated Option Omega MCP calls; {sum(c["successful"] > 0 for c in coverage)} of {len(tools)} tools returned successful responses.
- 52 development comparisons, 68 robustness/asset/structure experiments, 47 reserved-period evaluations, one three-date verification run, and separate targeted diagnostics where listed in the batch table.
- A final 12-asset exploratory extension applies NQ-up Monday signals to the same 2024 asset baselines. It was added after main holdout execution began, is not a new holdout, and does not alter any frozen candidate.
- Every request, response, result, and exported trade log is retained. Rejected requests are counted separately, not treated as completed simulations.
- These are correlated research comparisons, not hundreds of independent confirmations of an edge.

The key integration works: NQ signals are computed locally and sent through `specificEntryDays`, with a fixed option entry time. QQQ contracts and option P&L come from Option Omega. NQ futures prices are not used as option strikes or as synthetic option prices. Arbitrary timestamp CSV upload remains a separate web-app workflow and was not tested in this study.

These executions used Codex's authenticated MCP transport. A future Claude-focused video should repeat its selected demonstration in Claude; this study does not claim that Claude itself performed the recorded runs.

## Methods and fixed assumptions

The original Parquet contains 6,629,079 rows from January 2006 through September 25, 2026. Its SHA-256 is `e44c31e69badc3c5233a0011b10453ea8020bb406e6563cea9a4a349e1e986f5`. Structural checks found no duplicate timestamps, invalid OHLC relationships, null/nonfinite values, or nonpositive prices/volume. Contract-roll adjustment and timestamp start/end labeling remain **unverified**; no metadata establishes either.

Signals require all 58 minute bars from 09:30 through 10:27 ET. Entries begin at 10:29, leaving a buffer under either timestamp convention. Morning direction compares the latest close with the 09:30 open. Tuesday breakout requires the latest close beyond the prior Monday's full 390-bar regular-session range. VWAP uses volume-weighted minute typical prices; its bands use weighted price dispersion. EMA50 resets each session, so it is not a daily EMA or a fully warmed continuous EMA.

Development uses 2023–2024. Four older-history tests use 2013–2022 separately. Candidates were frozen at `{frozen["frozen_at"]}` before reserved-period tests covering 2025-01-01 through 2026-09-25. Optimizer cells were selected on 2023 only by highest MAR among profitable cells with at least 30 trades. The exact submitted holdout specs were written before submission. A feature/spec hash manifest was added during execution before inspecting holdout outcomes; that later timestamp is explicitly recorded. Resume now refuses changed frozen inputs.

Baseline trades use one contract, at most one open position per individual strategy, a $1,000 buying-power cap, $1 per contract per side in commissions, and $0.03 position slippage per side. Stress tests use $0.10. Five-point verticals are the ordinary baseline; budget tests deliberately narrow them to one point. These are different structures, not merely resized versions of the original trade. DTE is nonexact: Option Omega chooses an available expiration at or beyond the requested DTE. Two-day holding means calendar days, delayed by market closures; it is not two trading sessions. Positions close at the test boundary.

## Reserved-period results

The main scenarios start with $10,000 and one contract; separate columns show the $25,000 comparison. Net P&L includes the specified fees and slippage. Stable experiment IDs can contain the original `7dte` family name; the separate DTE column is the actual configured target, including the optimized 21-DTE call.

{table(["Frozen candidate", "DTE", "Entry ET", "$10k trades", "$10k P&L .03", "$10k P&L .10", "$10k EOD DD .03", "Audit", "$25k P&L .03", "$25k EOD DD .03"], candidate_rows)}

These results must be compared with their matched controls. A positive options result does not establish that the NQ filter adds value, and a winning optimizer cell is selected from many alternatives. Reserved-period results are now observed; further tuning on these years would make them development data.

The $25,000 comparison keeps one contract and the same $1,000 allocation cap. It does not scale exposure by 2.5 times; equal dollar P&L with lower percentage drawdown is expected when both accounts can afford identical trades.

Slippage can also change whether a trade fits the allocation cap and therefore change later entries under the one-open-position rule. The two cost scenarios need not contain identical trades. Full scenario-specific counts and logs are retained in the summary CSV and raw evidence.

## Costs, timing, and volatility

In development, the NQ-up 0DTE put credit spread changed from +$1,261 to −$2,351 under the $0.10 slippage stress. The extra $0.07 per side represents $14 per round trip for one position. This is a direct demonstration of a thin apparent edge being consumed by modest execution costs.

{results_group("stress-")}

{results_group("delay-")}

The following filters use Option Omega's historical option metrics. They are not a locally calculated NQ implied-volatility signal, and these small development subsets do not establish a universal time to buy options.

{results_group("vol-")}

## Account feasibility

The budget scenarios use 0.5% or 1% of initial account equity as an expiration-payoff risk comparison, with commission reserves and buying-power constraints. The reported planned loss is the theoretical expiration payoff plus modeled round-trip commissions. It excludes additional closing slippage and real-world assignment/legging risk; it is **not a guarantee that realized loss cannot exceed the budget**. Zero or one trade indicates affordability constraints, not validation of a strategy.

{table(["Budget experiment", "Trades", "Net P&L", "Largest planned expiration loss + fees", "Worst realized loss", "Audit"], budget_rows)}

## Supported-asset smoke tests

Each test requested a weekly Monday entry in 2024, a 30-DTE five-point put credit spread, and a seven-calendar-day hold. IBIT rejected the January start with an explicit earliest date of November 19, 2024; a separately named correction uses that date and retains the rejected request. These test API/data availability, not an NQ-conditioned stock strategy. First/last fills below are observed fills under this rule, not the vendor's complete historical coverage dates.

{table(["Asset", "Trades", "First observed fill", "Last observed fill", "Net P&L", "Audit"], assets)}

The vendor's changelog states IBIT options backtesting begins in November 2024. An asset can be supported while having far less history than QQQ. Single-stock research also needs its own prices, earnings/dividend dates, and corporate-action handling.

The following extension adds the NQ-up condition to those Monday entries, keeping each underlying's option structure unchanged. This demonstrates the cross-asset signal path directly. Compare each with its asset baseline above, recognizing different trade counts and exposure. It does not identify undervalued stocks or use stock-specific technical indicators.

{results_group("crossasset-")}

## Other structures and older history

Condors, same-strike straddles, strangles, and same-strike call calendars were exercised. Calendars buy a longer-dated call and sell a shorter-dated call. These are workflow/structure experiments; gross buying-power assumptions and one-lot caps are not an assessment of suitability for a novice account.

{results_group("multileg-")}

{results_group("history-")}

## Portfolio and saved records

The development portfolio combines the NQ-up 0DTE put spread and NQ-up seven-DTE long call with shared $10,000 capital and per-strategy caps. It returned {money(portfolio["profit"])} across {portfolio["numberOfTrades"]} trades, with provider-reported EOD maximum drawdown of {portfolio["maxDrawdownPercentage"]}%. Trade-count and P&L reconciliation: **{portfolio_reconciles}**. Both components are bullish; combining them does not create independent diversification. This portfolio has not itself been evaluated on the reserved period.

Private saved records use the `TDL Research -` prefix. The portfolio is named `TDL Research - NQ morning portfolio`; the small workflow verification is `TDL Research - MCP verification`. Save/edit/replace operations touched only research-owned records. Cancellation calls targeted already completed runs and returned no-op results, so active cancellation semantics remain untested.

## Verification and limitations

Trade audit: {len(audits) - len(issues)} of {len(audits)} completed runs pass implemented checks. Any exceptions are shown below and remain review items.

{table(["Experiment", "Audit issues"], [[a["name"], "; ".join(a["issues"])] for a in issues]) if issues else "No outstanding trade-audit exceptions."}

All {len(curve_audit)} expected EOD equity series were retrieved (13 frozen baseline-cost candidates and the portfolio). Ending equity and peak-to-trough EOD drawdown were independently recalculated from those provider series; {len(curve_failures)} mismatches. Details: `{json.dumps(curve_failures)}`. This verifies arithmetic, not the underlying quotes or completeness of the vendor's market calendar.

Checks cover complete trade counts, summed P&L, allowed entry dates/times, contracts, instrument/leg sides, linked strikes, fees, allocation caps, same-expiry structures, calendar ordering, DTE bounds, and basic exit consistency. The nonexact DTE audit flags extensions over seven days for review, although the provider permits longer extensions. It cannot independently verify that the chosen expiry was the next available one. Holding/half-day checks are bounded consistency checks, not an independent complete exchange calendar.

An older-history exception was isolated for a QQQ June 30, 2015 110.5 call entered June 23. The original two-day exit request instead held through expiration, with no closing commission. Dedicated two- and three-day exit reproductions are retained below. The absence of a closing fee on expiration is distinguishable from an executed closing order; the unexplained failure to exit on schedule is the substantive issue. Its cause is unresolved and should be raised with the vendor using the request and trade evidence before that historical segment supports a performance claim.

{results_group("diagnostic-")}

NVDA also triggers a corporate-action review flag: the June 3–10, 2024 position is explicitly marked `wasAdjusted`, has ten post-adjustment contracts, and a half-point strike width instead of the original one-contract/five-point spread. This is consistent with NVIDIA's June 10 ten-for-one split, not evidence that the engine arbitrarily ignored the contract cap. The logged $174 total opening premium, $90 close, $2 opening fees, and $20 closing fees reconcile to $62 profit. General split-normalized structural validation is not implemented, so the review flag remains visible.

Historical quotes, deltas, bid/ask liquidity, fill executability, assignment/exercise behavior, and the vendor's full intraday equity path were not independently reconstructed. The provider documents midpoint fills and EOD maximum drawdown. NQ/QQQ basis differences, roll methodology, incomplete historical sessions, and regime changes remain relevant. No statistical significance claim or live-trading recommendation is made. The most complex user example produced zero development entries under the stated definitions and was not silently relaxed.

Development tooling: Python diagnostics were checked through a fresh Serena connection. Codebase-memory refresh twice reported `Indexing worker crashed on a file`; SocratiCode status twice returned `UND_ERR_SOCKET`, despite healthy Docker/Qdrant/Ollama infrastructure checks. Small-repository direct inspection, Serena symbol/reference checks, executable tests, and raw-result audits provide the fallback. Neither failed index refresh is represented as successful.

## MCP coverage and rejected requests

{table(["Tool", "Calls", "Successful", "Errors"], [[c["tool"], c["calls"], c["successful"], c["errors"]] for c in coverage])}

{table(["Batch", "States"], [[b["batch"], json.dumps(b["states"])] for b in batches])}

Known integration rejections included `capProfits=true` without a profit target, attempting a second active optimizer, and requesting IBIT before its available history. Each was corrected with retained evidence. Tool-call success means a valid response, not proof that every possible parameter or failure mode was tested.

## Next data layer and content readiness

Keep a reusable equities store outside this repository, partitioned by provider/feed, symbol, interval, adjustment policy, and version. Start with QQQ for a direct NQ-versus-QQQ signal comparison, then supported equities actually used by a hypothesis. Preserve timestamp convention, timezone, source entitlement, split/dividend treatment, missing-bar diagnostics, and SHA-256 in manifests. Credentials belong in a private credential store. Stock minute bars cannot replace historical option chains.

No Alpaca account/key was provisioned and no stock dataset was downloaded. Current tests use NQ features and Option Omega's options data. “Undervalued stocks,” earnings strategies, options prop-firm rules, and NQ futures options are outside this evidence.

The strongest demonstration is a reproducible research workflow: formulate a rule, derive causal signal dates, run an option structure and matched control, inspect trades, stress costs and account size, then open a frozen later-period test. The eventual article must disclose the sponsorship and hypothetical nature of results, show failures alongside attractive outcomes, and distinguish local code from MCP actions. The brand package requires draft review before publication; no draft has been sent and nothing has been published.

## Sources and evidence

- [Option Omega MCP](https://optionomega.com/mcp/)
- [Backtest setup and cost settings](https://docs.optionomega.com/backtesting/backtest-setup)
- [Backtesting FAQ: expiration selection, fills, and drawdown](https://docs.optionomega.com/backtesting/backtesting-faq)
- [Custom signal CSV documentation](https://docs.optionomega.com/backtesting/advanced-options/custom-signals-tester)
- [Official product changelog](https://optionomega.canny.io/changelog)
- [OIC bull call spread mechanics and expiration risks](https://www.optionseducation.org/strategies/all-strategies/bull-call-spread-debit-call-spread)
- [NYSE 2024 calendar](https://www.nyse.com/publicdocs/ICE_NYSE_2024_Yearly_Trading_Calendar.pdf)
- [NVIDIA's 2024 split FAQ](https://investor.nvidia.com/files/doc_downloads/2024/05/NVIDIA-2024-Stock-Split-FAQ.pdf)
- User-supplied creator handoff and brand package.

Raw artifacts are in the stable project evidence directory, not Git: `mcp-journal.jsonl`, `tool-schemas.json`, all `*-jobs.json`, individual results/trades, `summary.csv`, `trade-audit.json`, `mcp-tool-coverage.json`, `frozen-holdout.json`, `holdout-specs.json`, and `holdout-input-integrity.json`.
"""
    report_path = Path("docs/research/2026-10-20-results.md")
    report_path.write_text(report, encoding="utf-8")
    write_json(output / "report-facts.json", {
        "backtests": len(rows), "optimizer_cells": cells, "mcp_calls": len(journal),
        "tools_successful": sum(c["successful"] > 0 for c in coverage),
        "audit_exceptions": len(issues), "holdout_rows": candidate_rows,
    })
    labels = ["NQ up · 0DTE put credit spread", "NQ up · 7-DTE long call",
              "Tuesday breakout · 7-DTE call spread", "NQ down · 0DTE call credit spread",
              "Unfiltered · 0DTE put credit spread", "Unfiltered · 7-DTE long call",
              "Tuesday control · 7-DTE call spread", "Unfiltered · 0DTE call credit spread"]
    cards = "".join(f"<tr><td>{html.escape(label)}</td><td>{r[3]}</td><td>{r[4]}</td><td>{r[5]}</td><td>{r[6]}</td></tr>"
                    for label, r in zip(labels, candidate_rows[:8]))
    page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{font-family:var(--font-sans,system-ui);color:var(--foreground,#ddd);margin:0}}
.metrics{{display:flex;flex-wrap:wrap;gap:24px;margin:16px 0}}strong{{font-size:27px;display:block}}
small,p{{color:var(--muted-foreground,#aaa)}}table{{border-collapse:collapse;width:100%;font-size:12px}}
th,td{{padding:9px 5px;text-align:right;border-bottom:1px solid var(--border,#444)}}
td:not(:first-child){{white-space:nowrap}}
th:first-child,td:first-child{{text-align:left;overflow-wrap:anywhere;max-width:220px}}
.note{{padding:16px;background:var(--muted,#222);border-radius:var(--radius,8px);margin-top:16px}}
</style></head><body>
<div class="metrics"><div><strong>{len(rows)}</strong><small>completed backtests</small></div>
<div><strong>{cells}</strong><small>optimizer cells</small></div><div><strong>{sum(c["successful"] > 0 for c in coverage)} / {len(tools)}</strong><small>MCP tools exercised</small></div></div>
<h3>Frozen later-period tests · $10,000 · one contract</h3>
<p>January 2025–September 25, 2026. Net simulated P&amp;L after modeled fees. Slippage shown per position, per side.</p>
<table><thead><tr><th>Original hypothesis / control</th><th>Trades</th><th>$0.03</th><th>$0.10</th><th>EOD DD</th></tr></thead><tbody>{cards}</tbody></table>
<div class="note">Historical simulation, not a live-trading recommendation. NQ adjustment and bar-label provenance remain unverified. Full report includes all optimizer selections, failures, asset checks, and account constraints.</div>
<script>console.log("Research summary rows",document.querySelectorAll("tbody tr").length);</script>
</body></html>"""
    (output / "research-summary.html").write_text(page, encoding="utf-8")
    print(json.dumps({"report": str(report_path.resolve()), "backtests": len(rows),
                      "tools": len(coverage), "audit_exceptions": len(issues)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
