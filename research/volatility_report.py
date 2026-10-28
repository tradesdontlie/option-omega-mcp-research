"""Audited native volatility-ratio comparisons, including negative evidence."""

import argparse
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path

from research.asset_audit import summarize
from research.asset_report import classify_with_prior_audits
from research.asset_search import ASSETS
from research.batch import write_json
from research.curves import check_curve
from research.evidence import trade_rows
from research.mcp_client import OmegaClient
from research.report import money, table


def ratio_issues(parameters, row):
    errors, warnings = [], []
    for metric in ['Hv30Iv30Ratio', 'Iv9DIv30Ratio', 'Vix9DVixRatio']:
        for bound in ['Min', 'Max']:
            key = 'entry' + bound + metric
            limit = parameters.get(key)
            if limit is None:
                continue
            field = 'opening' + metric
            value = row.get(field)
            if value is None:
                warnings.append('Entry ratio not exported: ' + field)
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append('Invalid exported entry ratio: ' + field)
                continue
            if not math.isfinite(number) or (bound == 'Min' and number < limit) or (bound == 'Max' and number > limit):
                errors.append('Exported entry ratio violates ' + key)
    return errors, warnings


def audit_ratios(output, audit):
    lookup = {r['name']: r for r in audit}
    evidence = []
    for path in output.glob('*-jobs.json'):
        for job in json.loads(path.read_text()):
            if job['state'] != 'done':
                continue
            name = job['spec']['name']
            rows = trade_rows(json.loads((output / (name + '-trades.json')).read_text()))
            for row in rows:
                errors, warnings = ratio_issues(job['spec']['parameters'], row)
                if errors or warnings:
                    evidence.append(dict(name=name, opened=row['dateOpened'], errors=errors, warnings=warnings))
                    lookup[name]['audit_errors'] = sorted(set(lookup[name]['audit_errors'] + errors))
                    lookup[name]['audit_warnings'] = sorted(set(lookup[name]['audit_warnings'] + warnings))
    write_json(output / 'volatility-entry-ratio-exceptions.json', evidence)
    write_json(output / 'volatility-trade-audit.json', audit)
    return audit


def available_curves(output):
    jobs = json.loads((output / 'confirmation-jobs.json').read_text())
    checks = {}
    client = OmegaClient(output)
    try:
        for job in jobs:
            if job['spec']['variant'] != 'base' or job['state'] != 'done':
                continue
            name, p = job['spec']['name'], job['spec']['parameters']
            path = output / (name + '-curve.json')
            if path.exists():
                curve = json.loads(path.read_text())
            else:
                curve = client.call('get_equity_curve', {'parameters': {
                    'runId': job['runId'], 'seriesStart': p['rangeStart'], 'seriesEnd': p['rangeEnd']}})
                write_json(path, curve)
            result = json.loads((output / (name + '-results.json')).read_text())
            checks[job['spec']['asset']] = check_curve(curve, result)
    finally:
        client.close()
    write_json(output / 'asset-curve-audit.json', checks)
    return checks


def build(output):
    audit = audit_ratios(output, summarize(output))
    lookup = {r['name']: r for r in audit}
    coverage = json.loads((output / 'asset-batch-coverage.json').read_text())
    if any(s not in {'done', 'failed', 'cancelled'} for b in coverage for s in b['states']):
        raise RuntimeError('Cannot publish incomplete volatility research')
    frozen = json.loads((output / 'asset-finalists.json').read_text())
    checks = available_curves(output)
    candidates, overview, sections = [], [], []
    for asset in ASSETS:
        missing = [v for v in ['base', 'stress', 'control', 'small'] if f'confirmation-{asset}-{v}' not in lookup]
        if asset not in frozen['candidates'] or missing:
            reason = 'No successful validation' if asset not in frozen['candidates'] else 'Unavailable confirmation variants: ' + ', '.join(missing)
            candidates.append(dict(asset=asset, status='Unavailable, not qualified', reasons=[reason], difference=None))
            overview.append([asset, 'n/a', 'n/a', 'n/a', 'n/a', 'n/a', 'n/a', 'n/a', reason])
            sections.append(f'## {asset}: Unavailable, not qualified\n\n{reason}. No replacement was selected from confirmation results. See batch failure evidence.\n')
            continue
        candidate = frozen['candidates'][asset]
        spec = candidate['spec']
        training = spec['training_result']
        p = spec['parameters']
        base, stress, control, small = [lookup[f'confirmation-{asset}-{v}'] for v in ['base', 'stress', 'control', 'small']]
        status, reasons = classify_with_prior_audits(training, candidate, base, stress, checks[asset], lookup)
        difference = base['profit'] - control['profit']
        card = dict(asset=asset, profile=spec['profile'], side=p['legs'][0]['type'],
                    profit=base['profit'], stress_profit=stress['profit'], control_profit=control['profit'],
                    difference=difference, trades=base['trades'], drawdown=base['drawdown'],
                    status=status, reasons=reasons, small_profit=small['profit'], small_trades=small['trades'],
                    small_drawdown=small['drawdown'], profit_without_best_trade=base['profit_without_best_trade'],
                    stress_profit_without_best_trade=stress['profit_without_best_trade'])
        candidates.append(card)
        overview.append([asset, card['side'], spec['profile'], money(base['profit']), money(stress['profit']),
                         money(control['profit']), money(difference), base['trades'], status])
        scenarios = [[label, r['trades'], money(r['profit']), f"{r['drawdown']:.2f}%",
                      '; '.join(r['audit_errors']) or 'pass'] for label, r in
                     [('Base', base), ('Higher costs', stress), ('Matched control', control), ('$10k / $500 cap', small)]]
        treatments = [r for r in audit if r['asset'] == asset and r['phase'] == 'training']
        fields = {k: v for k, v in p.items() if k.startswith('entry') and k not in {'entryTime', 'entrySlippage'}}
        mfe = 'n/a' if base['max_mfe_pct'] is None else f"{base['max_mfe_pct']:.2f}%"
        sections.append(f"""## {asset}: {status}

Selected profile: **{spec['profile']}**, bought {card['side'].lower()}; {base['target_dte']} target DTE, {base['delta']} delta, +{base['profit_target']}% target. Native filter fields: `{json.dumps(fields, sort_keys=True)}`.

Training: {money(training['profit'])} / {training['numberOfTrades']} trades. Validation: {money(candidate['validation_result']['profit'])} / {candidate['validation_result']['numberOfTrades']} trades. Unmet gates: {'; '.join(reasons) or 'none'}.

{table(['Confirmation', 'Trades', 'Net P&L', 'EOD DD', 'Trade audit'], scenarios)}

Difference from matched control: {money(difference)}. This is not a causal or statistical-significance claim. Removing a filter can change entries, holding periods, and later capital availability.

Largest winner: {money(base['largest_win'])}; net P&L excluding that winner: {money(base['profit_without_best_trade'])}. This diagnostic leaves the other trades unchanged; it is not a resimulation. Maximum observed MFE: {mfe}; trades reaching +100% / +500% / +1000% before actual exit: {base['mfe_ge_100_count']} / {base['mfe_ge_500_count']} / {base['mfe_ge_1000_count']}. MFE is censored by exits and may not be executable.

Under higher costs, the largest winner is {money(stress['largest_win'])}; excluding it leaves {money(stress['profit_without_best_trade'])}. The same concentration caveat applies; no qualification threshold was added after seeing this diagnostic.

{table(['Training treatment', 'Trades', 'Net P&L', 'EOD DD', 'Audit'], [[r['name'], r['trades'], money(r['profit']), f"{r['drawdown']:.2f}%", '; '.join(r['audit_errors']) or 'pass'] for r in treatments])}
""")
    calls = sum(json.loads(line)['server'] == 'option-omega' for line in (output / 'mcp-journal.jsonl').read_text().splitlines())
    passed = [c['asset'] for c in candidates if c['status'] == 'Passed research screen']
    improved = [c['asset'] for c in candidates if c['difference'] is not None and c['difference'] > 0]
    errors = [r for r in audit if r['audit_errors']]
    report = f"""# Native volatility-ratio study

Generated {datetime.now(timezone.utc).isoformat()}. Exploratory historical research, not a live-trading recommendation.

Passed research gates: **{', '.join(passed) or 'none'}**. Higher confirmation P&L than matched control: **{', '.join(improved) or 'none'}**. These are different claims. {len(audit)} completed scenario records; {len(set(r['runId'] for r in audit))} distinct run IDs; {calls} authenticated MCP calls.

{table(['Asset', 'Exposure', 'Profile', 'Net .10', 'Net .20', 'Control', 'Difference', 'Trades', 'Status'], overview)}

## Fixed design and interpretation

The original training-selected direction/SMA family supplies each asset's reference structure: 42 DTE, 35 delta (SPX 15), +100% premium target, nominal 50% stop, and scheduled seven-DTE exit. These are the original screening parameters, not the later optimized finalists. Existing SMA conditions remain. A +100% target means doubling the paid premium before costs, not doubling the account.

Five profiles: `baseline` adds no volatility filter; `hv-high` requires HV30/IV30 >= 1; `hv-low` requires <= 1; `term-high` requires IV9/IV30 >= 1; `term-low` requires <= 1. SPX/SPY term profiles use VIX9D/VIX. Equality can appear in both groups. These are fixed exploratory thresholds, not optimized cutoffs. Historical volatility is backward-looking; exceeding IV does not prove underpricing or forecast future volatility. Underlying-level IV does not establish the cheapness of a particular strike.

No NQ directional filter is added. Every treatment uses the same dates with complete NQ feature coverage solely to retain a matched calendar. NQ prices are not used to value these options. No external Black-Scholes valuation or theoretical-price discount is tested here.

Training 2018–2023; validation 2024; confirmation January 2025–September 25, 2026. IBIT uses November 19, 2024–June 2025 / July–December 2025 / January–September 25, 2026. These periods were already examined: none is claimed as a pristine holdout. Select top two training profiles plus baseline for validation, then freeze one finalist per asset before confirmation. Frozen at `{frozen['frozen_at']}`. No confirmation-based retuning.

Qualification requires positive training profit with at least 20 trades, positive validation profit with at least 8 trades, positive baseline and higher-cost confirmation profit with at least 20 trades each, profit factor >= 1.1, EOD drawdown no worse than 25%, clean selected training/validation and base/stress trade audits, and reconciled baseline equity. These checks are not statistical significance or trading suitability.

Each asset has an independent $25,000 account, one contract/open trade, $1,000 premium cap, $1/contract/side commissions, and .10 slippage per side, stressed to .20. The $10,000 test reduces the premium cap to $500 and can change the sequence. Do not add asset P&Ls as though these were a shared portfolio. Stops can gap beyond 50%; full premium remains at risk. Scheduled seven-DTE exits include individually verified calendar adjustments. Ordinary late exits remain audit failures. EOD drawdown understates intraday risk; curves and historical quotes come from the provider.

""" + '\n'.join(sections)
    report += '\n## Execution evidence\n\n' + table(['Batch', 'States', 'Failures'], [[b['batch'], json.dumps(b['states']), json.dumps(b['failures'])] for b in coverage])
    report += '\n\n' + (table(['Audit-failed record', 'Errors'], [[r['name'], '; '.join(r['audit_errors'])] for r in errors]) if errors else 'No trade-audit errors.')
    report += '\n\nExported entry ratios are checked against requested boundaries when present. This checks the provider\'s rounded reported values, not independent IV data. Some VIX-filtered logs expose VIX but omit VIX9D/VIX; missing ratios remain unverified warnings, never fabricated values. `volatility-trade-audit.json` combines execution and ratio checks; `volatility-entry-ratio-exceptions.json` records missing values or violations.\n'
    report += '\n\nSaved finalists use `TDL Asset Search - volatility-confirmation-ASSET-base`, including failures. Full requests, source hashes, trades and curves are retained separately from earlier studies. Native field semantics: [Option Omega setup](https://docs.optionomega.com/backtesting/backtest-setup#use-underlying-iv).\n'
    Path('docs/research/2026-10-20-volatility-results.md').write_text(report, encoding='utf-8')
    write_json(output / 'volatility-candidates.json', candidates)
    write_json(output / 'volatility-report-facts.json', dict(scenarios=len(audit), mcp_calls=calls, qualified=passed,
               higher_pnl_than_control=improved, audit_errors=len(errors)))
    cards = ''.join((f"<article><h3>{c['asset']}</h3><p>{c['status']}: {html.escape('; '.join(c['reasons']))}</p></article>" if c['difference'] is None else f"<article><h3>{c['asset']} · {html.escape(c['profile'])}</h3><p>{c['side']} · {c['status']}</p><dl><dt>Net P&L</dt><dd>{money(c['profit'])}</dd><dt>Higher costs</dt><dd>{money(c['stress_profit'])}</dd><dt>Matched control</dt><dd>{money(c['control_profit'])}</dd><dt>Trades / EOD DD</dt><dd>{c['trades']} / {c['drawdown']:.2f}%</dd></dl></article>") for c in candidates)
    page = """<!doctype html><html><head><meta charset='utf-8'><style>body{margin:0;font-family:var(--font-sans,system-ui);color:var(--foreground)}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}article{border-top:1px solid var(--border);padding:8px 0}p{font-size:12px;color:var(--muted-foreground)}dl{display:grid;grid-template-columns:1fr auto;font-size:13px;gap:5px}dd{margin:0}</style></head><body><p>Volatility-ratio research · independent $25,000 accounts · previously examined historical periods. Fixed 42-DTE structures; +100% premium target.</p><main>""" + cards + "</main><p>Historical gains are not guarantees. See full report for exact filters, samples, costs and audit exceptions.</p><script>console.log('Assets',document.querySelectorAll('article').length)</script></body></html>"
    legend = '<p>HV = 30-day historical volatility; IV = implied volatility. hv-high / hv-low require HV/IV ≥ 1 / ≤ 1. term-high / term-low require 9-day/30-day IV ≥ 1 / ≤ 1; SPX/SPY use VIX9D/VIX. Baseline adds no volatility filter.</p><p>Confirmation: January 2025–September 25, 2026; IBIT uses January–September 25, 2026. Targets are option-premium gains, not account returns.</p>'
    page = page.replace('<main>', legend + '<main>', 1)
    (output / 'volatility-summary.html').write_text(page, encoding='utf-8')
    print('VOLATILITY REPORT COMPLETE', passed, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    build(parser.parse_args().output)
