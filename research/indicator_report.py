"""Report additive indicator comparisons without selecting on confirmation."""

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import time

from research.asset_audit import summarize
from research.asset_report import classification, curves
from research.asset_search import ASSETS
from research.batch import write_json
from research.report import money, table


def build(output):
    audit = summarize(output)
    lookup = {r['name']: r for r in audit}
    batches = json.loads((output / 'asset-batch-coverage.json').read_text())
    if any(s not in {'done', 'failed', 'cancelled'} for b in batches for s in b['states']):
        raise RuntimeError('Incomplete indicator experiments')
    frozen = json.loads((output / 'asset-finalists.json').read_text())
    checks = curves(output)
    source = json.loads((output / 'indicator-sources.json').read_text())
    train_jobs = json.loads((output / 'training-jobs.json').read_text())
    overview, sections, candidates = [], [], []
    for asset in ASSETS:
        candidate = frozen['candidates'][asset]
        spec = candidate['spec']
        p = spec['parameters']
        training = spec['training_result']
        candidate = dict(candidate, earlier_audit_errors=(lookup[training['name']]['audit_errors']
                                                        + lookup[spec['name']]['audit_errors']))
        base, stress, control, small = [lookup[f'confirmation-{asset}-{v}'] for v in ['base', 'stress', 'control', 'small']]
        status, reasons = classification(training, candidate, base, stress, checks[asset])
        treatment = spec['profile'] + (' + NQ' if spec['nq'] else ' / asset only')
        difference = base['profit'] - control['profit']
        card = dict(asset=asset, treatment=treatment, side=p['legs'][0]['type'],
                    target_dte=base['target_dte'], delta=base['delta'], profit_target=base['profit_target'],
                    profit=base['profit'], stress_profit=stress['profit'], control_profit=control['profit'],
                    difference=difference, trades=base['trades'], control_trades=control['trades'],
                    drawdown=base['drawdown'], control_drawdown=control['drawdown'],
                    status=status, reasons=reasons, small_profit=small['profit'],
                    largest_win=base['largest_win'], profit_without_best_trade=base['profit_without_best_trade'])
        candidates.append(card)
        overview.append([asset, p['legs'][0]['type'], treatment, money(base['profit']), money(stress['profit']),
                         money(control['profit']), money(difference), base['trades'], status])
        training_rows = []
        for job in train_jobs:
            if job['spec']['asset'] != asset:
                continue
            s = job['spec']
            if job['state'] == 'done':
                r = lookup[s['name']]
                training_rows.append([s['profile'], s['nq'], r['trades'], money(r['profit']), f"{r['drawdown']:.2f}%",
                                      '; '.join(r['audit_errors']) or 'pass'])
            else:
                training_rows.append([s['profile'], s['nq'], job['state'], job.get('error', ''), 'n/a', 'unavailable'])
        scenarios = [[name, r['trades'], money(r['profit']), f"{r['drawdown']:.2f}%",
                      'pass' if not r['audit_errors'] else '; '.join(r['audit_errors'])]
                     for name, r in [('Base .10', base), ('Stress .20', stress), ('Matched control', control), ('$10k / $500 cap', small)]]
        selected_fields = {k: v for k, v in p.items() if k.startswith('entry') and k not in {'entryTime', 'entrySlippage'}}
        max_mfe = 'n/a (no trades)' if base['max_mfe_pct'] is None else f"{base['max_mfe_pct']}%"
        mean_r = 'n/a (no trades)' if base['mean_stop_R'] is None else str(base['mean_stop_R'])
        sections.append(f"""## {asset}: {status}

Selected treatment: **{treatment}**, bought {p['legs'][0]['type'].lower()}; {base['target_dte']} target DTE, {base['delta']} delta, +{base['profit_target']}% premium target, nominal 50% premium stop, exit at seven DTE. Existing native entry filters: `{json.dumps(selected_fields, sort_keys=True)}`.

Training P&L {money(training['profit'])} / {training['numberOfTrades']} trades; validation P&L {money(candidate['validation_result']['profit'])} / {candidate['validation_result']['numberOfTrades']} trades. {'All research gates passed.' if not reasons else 'Unmet gates: ' + '; '.join(reasons) + '.'}

{table(['Confirmation', 'Trades', 'Net P&L', 'EOD DD', 'Trade audit'], scenarios)}

P&L difference versus the matched control: {money(difference)}. This is a historical comparison, not a causal or significance claim. Controls can have different entries, holding periods, and capital availability.

Largest winner: {money(base['largest_win'])}; net P&L excluding that winner: {money(base['profit_without_best_trade'])}. This concentration diagnostic leaves other trades unchanged; it is not a resimulation or a reason to discard a valid trade.

Observed MFE maximum: {max_mfe}; trades reaching +100% / +500% / +1000% before actual exit: {base['mfe_ge_100_count']} / {base['mfe_ge_500_count']} / {base['mfe_ge_1000_count']}. Mean net planned-stop R: {mean_r}. MFE is censored by actual exits and is not an achievable-fill claim.

{table(['Training profile', 'NQ confirmation', 'Trades', 'Net P&L', 'EOD DD', 'Audit'], training_rows)}
""")
    passed = [c['asset'] for c in candidates if c['status'] == 'Passed research screen']
    improved = [c['asset'] for c in candidates if c['difference'] > 0]
    journal = [json.loads(x) for x in (output / 'mcp-journal.jsonl').read_text().splitlines()]
    calls = sum(x['server'] == 'option-omega' for x in journal)
    report = f"""# Asset indicators with NQ confirmation

Generated {datetime.now(timezone.utc).isoformat()}. Historical exploratory research, not a promise of future profitability.

Passed research gates: **{', '.join(passed) or 'none'}**. Higher confirmation P&L than the matched control: **{', '.join(improved) or 'none'}**. These are different claims. {len(audit)} completed scenario records; {len(set(r['runId'] for r in audit))} distinct run IDs; {calls} authenticated MCP calls.

{table(['Asset', 'Exposure', 'Added treatment', 'Net .10', 'Net .20', 'Control', 'Difference', 'Trades', 'Status'], overview)}

## Design and interpretation

Each asset uses its preceding study's training-selected option structure and direction/SMA family. No later-period result selected that structure. Four additive treatments were compared: no added filter, RSI momentum (calls 50–70; puts 30–50), price aligned with the 50-minute EMA, and IV percentile at most 50. Each was tested with and without NQ confirmation. Existing SMA filters remain in every treatment for that asset. “Asset only” means no NQ gate, not necessarily no pre-existing asset filter.

NQ calls require positive morning return, price above session VWAP, and positive 15-minute momentum; puts require the opposite. Features stop at 10:27 ET for a 10:29 ET entry. Both controls and filtered runs use only dates with complete NQ feature coverage. NQ futures roll adjustment and timestamp labels remain unverified. Feature source SHA-256: `{source['features_sha256']}`. This compares entry calendars; the NQ condition does not trigger exits.

The provider's RSI is daily 14-day RSI updated each minute; EMA length is in minutes and includes extended-hours prices on non-index assets. IV percentile measures the prior 52-week distribution. A low percentile does not establish that an option is undervalued. Native indicator values are provider-supplied and have not been independently reconciled against stock/IV histories.

Training 2018–2023, validation 2024, confirmation January 2025–September 25, 2026. IBIT uses November 19, 2024–June 2025 / July–December 2025 / January–September 25, 2026. These periods were already explored in previous research, so confirmation is not a pristine untouched holdout. Top two training treatments plus the matched control enter validation; one finalist per asset was frozen at `{frozen['frozen_at']}` before confirmation. No confirmation-based retuning. Full grids, failures, and all treatment results are retained.

Research qualification requires positive training P&L with at least 20 trades, positive validation P&L with at least 8 trades, and positive baseline/stressed confirmation P&L with at least 20 trades each, profit factor at least 1.1, EOD drawdown no worse than 25%, no trade-audit errors, and reconciled baseline equity. This is not statistical significance or trading suitability. Choosing among many comparisons creates selection risk.

Each asset has an independent $25,000 account, one contract/open position, $1,000 premium cap, $1 commission per contract per side, .10 slippage per side stressed to .20. The $10,000 test caps premium at $500. These are not pooled portfolio results. Targets are gains on premium, not account returns. Stops can gap; the full premium remains at risk. Exit is scheduled at seven DTE; expiration or unverified late/calendar deviations are audit errors. EOD drawdown can understate intraday risk. Equity reconciliation uses the provider's series, not independent quotes.

""" + '\n'.join(sections)
    report += '\n## Batch coverage\n\n' + table(['Batch', 'States', 'Failures'], [
        [b['batch'], json.dumps(b['states']), json.dumps(b['failures'])] for b in batches])
    report += '\n\nSaved records use `TDL Asset Search - indicator-confirmation-ASSET-base`, including failures and insufficient-sample candidates. Native semantics: https://docs.optionomega.com/backtesting/backtest-setup.\n'
    Path('docs/research/2026-10-20-indicator-results.md').write_text(report, encoding='utf-8')
    write_json(output / 'indicator-candidates.json', candidates)
    write_json(output / 'indicator-report-facts.json', dict(scenarios=len(audit), mcp_calls=calls,
               qualified=passed, higher_pnl_than_control=improved, audit_errors=sum(bool(r['audit_errors']) for r in audit)))
    cards = ''.join(f"<article><h3>{c['asset']} · {html.escape(c['treatment'])}</h3><p>{c['side']} · {c['target_dte']} DTE · +{c['profit_target']}% target</p><dl><dt>Net P&L</dt><dd>{money(c['profit'])}</dd><dt>Higher costs</dt><dd>{money(c['stress_profit'])}</dd><dt>Matched control</dt><dd>{money(c['control_profit'])}</dd><dt>Trades / EOD DD</dt><dd>{c['trades']} / {c['drawdown']:.2f}%</dd></dl><p>{c['status']}</p></article>" for c in candidates)
    page = """<!doctype html><html><head><meta charset='utf-8'><style>body{margin:0;font-family:var(--font-sans,system-ui);color:var(--foreground)}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}article{border-top:1px solid var(--border);padding:8px 0}h3{margin:8px 0}p{color:var(--muted-foreground);font-size:12px}dl{display:grid;grid-template-columns:1fr auto;font-size:13px;gap:5px}dd{margin:0;font-variant-numeric:tabular-nums}</style></head><body><p>Historical indicator comparison · independent $25,000 accounts · one contract · early exits. Prior research already examined these periods.</p><main>""" + cards + "</main><p>Profitable results are not guarantees. Full report includes all training treatments, costs, smaller-account results, and audit limitations.</p><script>console.log('Asset cards',document.querySelectorAll('article').length)</script></body></html>"
    (output / 'indicator-summary.html').write_text(page, encoding='utf-8')
    print('INDICATOR REPORT COMPLETE', passed, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    if args.wait:
        deadline = time.monotonic() + 14400
        while not (args.output / 'indicator-final-usage.json').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Indicator study did not finish')
            time.sleep(10)
    build(args.output)
