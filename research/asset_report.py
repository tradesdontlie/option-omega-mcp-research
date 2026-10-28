"""Publish complete per-asset evidence without selecting on confirmation data."""

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import time

from research.asset_audit import summarize
from research.asset_search import ASSETS
from research.batch import write_json
from research.curves import check_curve
from research.mcp_client import OmegaClient
from research.report import table, money


def training_result(output, asset, candidate, tasks):
    task = next(t for t in tasks if t['asset'] == asset)
    name = candidate['spec']['name']
    if name.endswith('-baseline'):
        return task['selected']
    index = int(name.rsplit('-cell-', 1)[1])
    grid = json.loads((output / (task['name'] + '-optimization-results.json')).read_text())
    return next(c for c in grid['cells'] if c['index'] == index)


def classification(training, candidate, base, stress, curve):
    reasons = []
    if training['profit'] <= 0 or training['numberOfTrades'] < 20:
        reasons.append('training profit/sample gate')
    if not candidate['validation_qualified']:
        reasons.append('validation profit/sample gate')
    if base['profit'] <= 0:
        reasons.append('confirmation loss' if base['profit'] < 0 else 'no positive confirmation profit')
    if stress['profit'] <= 0:
        reasons.append('cost-stress loss' if stress['profit'] < 0 else 'no positive cost-stress profit')
    if min(base['trades'], stress['trades']) < 20:
        reasons.append('fewer than 20 confirmation trades')
    if base['drawdown'] < -25 or stress['drawdown'] < -25:
        reasons.append('EOD drawdown exceeds 25%')
    def passes_profit_factor(row):
        value = row['profit_factor']
        if value is not None:
            return value >= 1.1
        return row.get('gross_losses') == 0 and row.get('gross_gains', 0) > 0

    if not passes_profit_factor(base) or not passes_profit_factor(stress):
        reasons.append('profit factor below 1.1')
    if base['audit_errors'] or stress['audit_errors']:
        reasons.append('unresolved trade-audit errors')
    if candidate.get('earlier_audit_errors'):
        reasons.append('selected training/validation audit errors')
    if not curve['ending_equity_matches'] or not curve['drawdown_matches']:
        reasons.append('equity curve mismatch')
    label = 'Passed research screen' if not reasons else ('Profitable, not qualified' if base['profit'] > 0 else 'Not profitable')
    if base['trades'] == 0:
        label = 'No confirmation trades'
    return label, reasons


def classify_with_prior_audits(training, candidate, base, stress, curve, lookup):
    errors = list(lookup[candidate['spec']['name']]['audit_errors'])
    # Optimizer cells have summary statistics but no standalone training trade log.
    if training.get('name'):
        errors.extend(lookup[training['name']]['audit_errors'])
    audited = dict(candidate, earlier_audit_errors=errors)
    return classification(training, audited, base, stress, curve)


def curves(output):
    jobs = json.loads((output / 'confirmation-jobs.json').read_text())
    client = OmegaClient(output)
    audit = {}
    try:
        for job in jobs:
            if job['spec']['variant'] != 'base':
                continue
            if job['state'] != 'done':
                raise RuntimeError('Incomplete confirmation run')
            name = job['spec']['name']
            p = job['spec']['parameters']
            path = output / (name + '-curve.json')
            if path.exists():
                curve = json.loads(path.read_text())
            else:
                curve = client.call('get_equity_curve', {'parameters': {
                    'runId': job['runId'], 'seriesStart': p['rangeStart'], 'seriesEnd': p['rangeEnd'],
                }})
                write_json(path, curve)
            result = json.loads((output / (name + '-results.json')).read_text())
            audit[job['spec']['asset']] = check_curve(curve, result)
        write_json(output / 'asset-curve-audit.json', audit)
    finally:
        client.close()
    return audit


def build(output):
    audit = summarize(output)
    lookup = {r['name']: r for r in audit}
    batches = json.loads((output / 'asset-batch-coverage.json').read_text())
    if any(state not in {'done', 'failed', 'cancelled'} for batch in batches for state in batch['states']):
        raise RuntimeError('Cannot publish unfinished research')
    frozen = json.loads((output / 'asset-finalists.json').read_text())
    tasks = json.loads((output / 'asset-optimization-tasks.json').read_text())
    curve_audit = curves(output)
    candidates, overview, sections = [], [], []
    for asset in ASSETS:
        candidate = frozen['candidates'][asset]
        training = training_result(output, asset, candidate, tasks)
        validation = candidate['validation_result']
        base, stress, control, small = [lookup[f'confirmation-{asset}-{v}'] for v in ['base', 'stress', 'control', 'small']]
        label, reasons = classify_with_prior_audits(training, candidate, base, stress, curve_audit[asset], lookup)
        p = candidate['spec']['parameters']
        side = 'Buy call' if p['legs'][0]['type'] == 'Call' else 'Buy put'
        condition = 'No trend filter'
        if p.get('entrySmaCondition'):
            condition = ('Above' if p['entrySmaCondition'] == 'AboveSma' else 'Below') + ' own 50-day SMA'
        card = dict(asset=asset, status=label, reasons=reasons, side=side, condition=condition,
                    target_dte=base['target_dte'], delta=base['delta'], profit_target=base['profit_target'],
                    training_profit=training['profit'], validation_profit=validation['profit'],
                    confirmation_profit=base['profit'], stress_profit=stress['profit'],
                    trades=base['trades'], stress_trades=stress['trades'], drawdown=base['drawdown'],
                    profit_factor=base['profit_factor'], max_mfe_pct=base['max_mfe_pct'],
                    small_profit=small['profit'], small_trades=small['trades'],
                    control_profit=control['profit'], audit_warnings=base['audit_warnings'],
                    largest_win=base['largest_win'], profit_without_best_trade=base['profit_without_best_trade'])
        candidates.append(card)
        overview.append([asset, side, condition, f"{base['target_dte']} / {base['delta']}",
                         str(base['profit_target']) + '%', money(base['profit']), money(stress['profit']),
                         base['trades'], f"{base['drawdown']:.2f}%", label])
        scenario_rows = []
        for variant, r in [('Base .10', base), ('Stress .20', stress), ('Unfiltered .10', control), ('$10k / $500 cap', small)]:
            scenario_rows.append([variant, r['trades'], money(r['profit']), f"{r['drawdown']:.2f}%",
                                  f"{r['profit_factor']:.2f}" if r['profit_factor'] is not None else 'n/a',
                                  'review' if r['audit_errors'] else 'pass'])
        mfe = 'No trades' if base['max_mfe_pct'] is None else (
            f"Median observed MFE {base['median_mfe_pct']:.1f}%; maximum {base['max_mfe_pct']:.1f}%. "
            f"Trades reaching at least +100% / +500% / +1000% before their actual exit: "
            f"{base['mfe_ge_100_count']} / {base['mfe_ge_500_count']} / {base['mfe_ge_1000_count']} of {base['trades']}.")
        r_text = 'No trades' if base['mean_stop_R'] is None else (
            f"Average net premium return {100 * base['mean_premium_R']:.2f}%; "
            f"average net planned-stop R {base['mean_stop_R']:.3f}R. "
            f"Worst realized loss {money(base['worst_loss'])}; largest opening premium {money(base['largest_premium'])}.")
        screens = [r for r in audit if r['phase'] == 'screen' and r['asset'] == asset]
        family_table = table(['Training family', 'Trades', 'Net P&L', 'EOD DD'], [
            [r['side'] + ' / ' + r['regime'], r['trades'], money(r['profit']), f"{r['drawdown']:.2f}%"] for r in screens])
        sections.append(f"""## {asset}: {label}

**Rule:** {side}; {condition}; enter at 10:29 ET; target {base['target_dte']} DTE and {base['delta']} delta; take profit at +{base['profit_target']}% of premium; nominal stop at −50%; close at 7 DTE if neither price exit occurs. One contract, one open trade. Actual entry DTE ranged from {base['actual_entry_dte_min']} to {base['actual_entry_dte_max']}; minimum exit DTE was {base['minimum_exit_dte']}.

Selected configuration training: {money(training['profit'])} over {training['numberOfTrades']} trades. Validation: {money(validation['profit'])} over {validation['numberOfTrades']} trades. {'All research gates passed.' if not reasons else 'Unmet gates: ' + '; '.join(reasons) + '.'}

{table(['Confirmation scenario', 'Trades', 'Net P&L', 'EOD max DD', 'Profit factor', 'Audit'], scenario_rows)}

{r_text}

Largest winning trade: {money(base['largest_win'])}. Net P&L excluding that one winner: {money(base['profit_without_best_trade'])}. This is a concentration diagnostic, not a new backtest or an assumption that the winner was invalid; it leaves the remaining trade sequence unchanged.

{mfe}

Exit reasons: `{json.dumps(base['exits'])}`. Trade audit errors: `{json.dumps(base['audit_errors'])}`. Warnings: `{json.dumps(base['audit_warnings'])}`.

{family_table}
""")
    journal = [json.loads(line) for line in (output / 'mcp-journal.jsonl').read_text().splitlines()]
    calls = [r for r in journal if r['server'] == 'option-omega']
    grids = [json.loads((output / (t['name'] + '-optimization-results.json')).read_text()) for t in tasks]
    cells = sum(len(g['cells']) for g in grids)
    unique_runs = len({r['runId'] for r in audit})
    passed = [c['asset'] for c in candidates if c['status'] == 'Passed research screen']
    profitable = [c['asset'] for c in candidates if c['confirmation_profit'] > 0]
    report = f"""# Per-asset options strategy search

Generated {datetime.now(timezone.utc).isoformat()}. Historical research, not a promise of future profitability.

## Findings

Profitable at baseline confirmation costs: **{', '.join(profitable) or 'none'}**. Passed the predefined research gates: **{', '.join(passed) or 'none'}**. A profitable confirmation result and a qualified candidate are different claims. Every asset is shown, including failures.

{len(audit)} completed scenario records ({unique_runs} distinct backtest run IDs), {len(grids)} optimizer grids / {cells} cells, and {len(calls)} authenticated MCP calls. Identical unfiltered controls may reuse the same server run; they are not independent confirmations. All calls and full grids are retained.

{table(['Asset', 'Exposure', 'Entry filter', 'DTE / delta', 'PT', 'Net .10', 'Net .20', 'Trades .10', 'EOD DD .10', 'Status'], overview)}

## What the strategies mean

These strategies buy calls for bullish exposure or buy puts for bearish exposure. They do not sell naked options. Entry conditions use each asset's own 50-day SMA where selected; no NQ signal is used in this search. Option Omega calculates that daily SMA using the current price and previous daily closes, not the future end-of-day close. The filter decides whether to open a new position; it does not itself close an existing position when the trend flips.

Profit targets are percentage gains on option premium. +100% means an option position doubles before costs; +500% means six times the initial premium; +1000% means eleven times. A nominal 50% stop means these targets correspond to approximately 2R, 10R, and 20R before fees and slippage, if R is that planned stop distance. A stop is not a guaranteed loss ceiling. The full paid premium remains at risk for a bought option.

The report's premium R is net P&L divided by opening premium. Planned-stop R divides net P&L by 50% of opening premium plus modeled round-trip commissions and stop slippage. Neither denominator is account equity. MFE is the provider's largest observed profit percentage during the actual holding period: it is hindsight, may reflect non-executable quotes, and is censored by whichever exit occurred. A strategy exiting at +100% cannot tell us whether that same trade would later have reached +1000%.

## Search protocol and dates

The search screens both directions with and without a 50-day trend filter, then chooses one family per asset using training only. It sweeps 21/42/63 target DTE, three delta settings, and targets from 100% to 1000% in 100-point increments: 90 cells per asset. Two training-selected cells plus the original baseline are evaluated on the validation period; duplicate settings are removed. The chosen validation finalist is frozen before confirmation. This searches only one selected direction/filter family deeply per asset, not every possible strategy.

Most assets: training 2018–2023, validation 2024, confirmation January 2025–September 25, 2026. IBIT: training November 19, 2024–June 2025, validation July–December 2025, confirmation January–September 25, 2026. Any server-required later start is retained in the manifests. Prior research already observed some of these confirmation years, especially QQQ; they are not a pristine untouched holdout. New tuning based on these results requires a new forward evaluation.

Finalists frozen at `{frozen['frozen_at']}`. Training eligibility prefers positive net P&L and at least 20 trades; validation requires positive net P&L and at least 8 trades. The final research gate additionally requires positive baseline and stressed confirmation P&L, at least 20 trades in both, profit factor at least 1.1 in both, EOD drawdown no worse than 25% in both, no trade-audit errors, and reconciled baseline equity. These are research thresholds, not a suitability test or statistical significance claim. Hundreds of comparisons create selection risk despite chronological separation.

## Execution and account assumptions

Base account $25,000, maximum opening premium $1,000, one contract and one open position per independent test. Entry 10:29 ET; exit at seven calendar DTE if no target or stop occurs. $1 per contract per side; $0.10 position slippage per side, stressed to $0.20. Profit targets require two consecutive price hits and cap non-opening target fills. Losses are not capped. Stop fills can gap past the nominal trigger. The audit flags any close after the configured DTE deadline and any scheduled DTE deviation; calendar exceptions require verification. Any actual expiration is an error, not an accepted exercise plan.

DTE is nonexact: the next available expiration at or beyond the requested DTE is used. Actual DTE ranges are reported because the extension can exceed one week. The provider's EOD drawdown omits worse intraday paths; raw MFE/MAE and trade logs remain available. Twelve baseline equity curves are reconciled from the provider's own series, not independently sourced options quotes.

The $10,000 test reduces the premium cap to $500. It can skip trades and produce a different sequence; it is not a scaled version of the $25,000 result. Cost stress can also change affordability and subsequent entries. These asset tests each have their own capital pool; their P&Ls must not be added as though they came from a single funded portfolio. Broker approvals and live fills were not tested.

""" + '\n'.join(sections)
    errors = [r for r in audit if r['audit_errors']]
    report += f"""
## Audit exceptions and automation

{len(errors)} completed scenario records have trade-audit errors. Corporate-action normalization and extended expirations are separately visible warnings. No failed run is silently omitted from the batch coverage.

{table(['Experiment', 'Errors'], [[r['name'], '; '.join(r['audit_errors'])] for r in errors]) if errors else 'No trade-audit errors.'}

{table(['Batch', 'States', 'Failures'], [[b['batch'], json.dumps(b['states']), json.dumps(b['failures'])] for b in batches])}

`python -m research.asset_search OUTPUT` orchestrates the complete search through authenticated MCP: submit, poll, export, select on earlier data, freeze, confirm, and save privately. `python -m research.asset_report OUTPUT` audits and generates this report. The scripts do not require an agent to issue each MCP tool call manually, and do not start model turns. They still use the real MCP endpoint and respect its call/start limits. Optimizer grids reduce server calls as well: one submitted grid returns 90 comparisons rather than starting 90 standalone jobs. Daily usage remains governed by the provider, and the scripts do not bypass quotas.

Saved finalists use `TDL Asset Search - confirmation-ASSET-base`, including losing and insufficient-sample finalists. Their presence in Saved Backtests is not an endorsement. The full local evidence directory includes all request manifests, rejected calls, result/trade JSON, optimizer cells, `asset-finalists.json`, confirmation hashes, `asset-results.csv`, and `asset-curve-audit.json`.

Sources: [Option Omega setup and indicator/target conventions](https://docs.optionomega.com/backtesting/backtest-setup), [FAQ on nonexact expiration, fills, and drawdown](https://docs.optionomega.com/backtesting/backtesting-faq), and the authenticated MCP tool schemas and exports saved with this study.
"""
    path = Path('docs/research/2026-10-20-asset-strategies.md')
    path.write_text(report, encoding='utf-8')
    write_json(output / 'asset-candidates.json', candidates)
    write_json(output / 'asset-report-facts.json', {'scenarios': len(audit), 'unique_runs': unique_runs,
        'optimizer_cells': cells, 'mcp_calls': len(calls), 'profitable_assets': profitable, 'qualified_assets': passed,
        'audit_errors': len(errors)})
    cards = []
    for c in candidates:
        tone = 'var(--success,#48b77b)' if c['status'] == 'Passed research screen' else 'var(--muted-foreground,#aaa)'
        cards.append(f"<tr><td><b>{c['asset']}</b><small>{html.escape(c['side'] + ' · ' + c['condition'])}</small></td>"
                     f"<td>{c['target_dte']}d / +{c['profit_target']}%</td><td>{money(c['confirmation_profit'])}</td>"
                     f"<td>{money(c['stress_profit'])}</td><td>{c['trades']}</td><td style='color:{tone}'>{c['status']}</td></tr>")
    page = """<!doctype html><html><head><meta charset='utf-8'><style>
body{margin:0;font-family:var(--font-sans,system-ui);color:var(--foreground,#ddd)}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:10px 5px;text-align:right;border-bottom:1px solid var(--border,#444)}
td:first-child,th:first-child{text-align:left}td:nth-child(3),td:nth-child(4){white-space:nowrap}
small{display:block;color:var(--muted-foreground,#999);font-size:11px}p{color:var(--muted-foreground,#aaa)}
@media(max-width:550px){thead{display:none}table,tbody{display:block}tr{display:grid;grid-template-columns:1fr 1fr;padding:10px 0;border-bottom:1px solid var(--border,#444)}td{border:0;text-align:left;padding:4px 0}td:first-child,td:last-child{grid-column:1/-1}td:nth-child(2)::before{content:"DTE / target: ";color:var(--muted-foreground,#999)}td:nth-child(3)::before{content:"Net .10: ";color:var(--muted-foreground,#999)}td:nth-child(4)::before{content:"Net .20: ";color:var(--muted-foreground,#999)}td:nth-child(5)::before{content:"Trades: ";color:var(--muted-foreground,#999)}}
</style></head><body><h3>Options candidates by asset</h3>
<p>Historical confirmation · $25,000 · one contract · $1,000 premium cap. Bought calls/puts; early exit before expiration.</p>
<table><thead><tr><th>Asset / entry</th><th>DTE / target</th><th>Net .10</th><th>Net .20</th><th>Trades</th><th>Research status</th></tr></thead><tbody>""" + ''.join(cards) + """</tbody></table>
<p>Targets are returns on option premium, not account returns. Profitable histories are not guarantees. Full report includes drawdowns, controls, account constraints, and MFE.</p>
<script>console.log('Asset rows',document.querySelectorAll('tbody tr').length)</script></body></html>"""
    (output / 'asset-summary.html').write_text(page, encoding='utf-8')
    print(json.dumps({'report': str(path.resolve()), 'profitable': profitable, 'qualified': passed}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    if args.wait:
        deadline = time.monotonic() + 7200
        while not (args.output / 'search-final-usage.json').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Search did not finish within two hours')
            time.sleep(10)
    build(args.output)
