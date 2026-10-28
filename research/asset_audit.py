"""Long-option target/stop audits and explicitly defined return denominators."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
from statistics import mean, median

from research.batch import write_json
from research.evidence import parse_legs, structure_issues, trade_rows

# Independently verified closure/early-close date; not a general date tolerance.
# Cboe reference C2025061200, MIAX July 2, 2025 notice, and Cboe 2026 calendar.
VERIFIED_PRIOR_SESSION_EXITS = {
    '2019-04-19': ('2019-04-18', '15:49:00'),
    '2020-01-01': ('2019-12-31', '15:49:00'),
    '2021-01-01': ('2020-12-31', '15:49:00'),
    '2021-04-02': ('2021-04-01', '15:49:00'),
    '2024-03-29': ('2024-03-28', '15:49:00'),
    '2025-07-04': ('2025-07-03', '12:59:00'),
    '2026-04-03': ('2026-04-02', '15:49:00'),
    '2026-06-19': ('2026-06-18', '15:49:00'),
    '2026-07-03': ('2026-07-02', '15:49:00'),
}
# Observed next-session convention for verified Monday market closures.
# NYSE Group's published 2018/2019/2020 holiday calendar verifies these dates.
VERIFIED_NEXT_SESSION_EXITS = {
    '2020-01-20': ('2020-01-21', '15:49:00'),
    '2020-02-17': ('2020-02-18', '15:49:00'),
}


def trade_risk(parameters, row):
    premium = -row['premiumPerContract'] * row['numberOfContracts']
    original_contracts = row['openingFees'] / parameters['openingFees']
    reserve = original_contracts * (parameters['openingFees'] + parameters['closingFees']
                                    + 100 * parameters['stopLossSlippage'])
    nominal = premium * parameters['exitConditions'][0]['maxLoss'] / 100 + reserve
    return {'premium': premium, 'nominal_stop_risk': nominal,
            'premium_R': row['profit'] / premium if premium else 0,
            'stop_R': row['profit'] / nominal if nominal else 0}


def audit_trade(parameters, row):
    errors, warnings = [], []
    for issue in structure_issues(parameters, row):
        (warnings if row.get('wasAdjusted') else errors).append(issue)
    opened = datetime.fromisoformat(row['dateOpened'] + 'T' + row['timeOpened'])
    closed = datetime.fromisoformat(row['dateClosed'] + 'T' + row['timeClosed'])
    if closed < opened or row['dateClosed'] > parameters['rangeEnd']:
        errors.append('Invalid close date')
    if not parameters['rangeStart'] <= row['dateOpened'] <= parameters['rangeEnd']:
        errors.append('Entry outside test interval')
    if parameters.get('specificEntryDays') and row['dateOpened'] not in parameters['specificEntryDays'].split(','):
        errors.append('Entry outside explicit signal calendar')
    if row['timeOpened'] != parameters['entryTime']:
        errors.append('Entry time differs from request')
    if (closed.date() - opened.date()).days != row['daysInTrade']:
        errors.append('Days in trade does not reconcile')
    legs = parse_legs(row['legs'])
    if len(legs) != 1 or legs[0]['side'] != 'BTO':
        errors.append('Expected one bought option')
        return {'errors': errors, 'warnings': warnings}
    try:
        month_day = datetime.strptime('2000 ' + legs[0]['expiry'], '%Y %b %d')
        year = opened.year + ((month_day.month, month_day.day) < (opened.month, opened.day))
        expiry = month_day.replace(year=year)
    except ValueError:
        errors.append('Malformed expiration date')
        return {'errors': errors, 'warnings': warnings}
    dte = (expiry.date() - opened.date()).days
    remaining = (expiry.date() - closed.date()).days
    target_dte = parameters['legs'][0]['daysToExpiration']
    if dte < target_dte:
        errors.append('Expiration earlier than requested DTE')
    if dte > target_dte + 7:
        warnings.append('Nonexact expiration extends more than seven days')
    if not row.get('wasAdjusted') and row['numberOfContracts'] > parameters['maxContractsPerTrade']:
        errors.append('Contract cap exceeded')
    if remaining <= 0 or row['reasonClosed'] == 'Expired':
        errors.append('Position reached expiration')
    rule = parameters['exitConditions'][0]
    deadline = (expiry - timedelta(days=rule['exitAtDte'])).date().isoformat()
    close_stamp = (row['dateClosed'], row['timeClosed'])
    next_session = (VERIFIED_NEXT_SESSION_EXITS.get(deadline) == close_stamp
                    and row['reasonClosed'] in {'Exited at Specified DTE', 'Exited at Specified Time'})
    if not next_session and (remaining < rule['exitAtDte'] or (remaining == rule['exitAtDte'] and row['timeClosed'] > rule['exitTime'])):
        errors.append('Position remained open after configured DTE deadline')
    reason = row['reasonClosed']
    premium = trade_risk(parameters, row)['premium']
    fees = row['openingFees'] + row['closingFees']
    gross = row['profit'] + fees
    if reason == 'Profit Target':
        tolerance = 100 * parameters['exitSlippage'] * row['numberOfContracts'] + 1
        if gross < premium * rule['maxProfit'] / 100 - tolerance:
            errors.append('Profit-target exit below configured target tolerance')
    elif reason == 'Stop Loss':
        if gross > -premium * rule['maxLoss'] / 100 + 1:
            errors.append('Stop-loss exit above configured trigger')
    elif reason in {'Exited at Specified DTE', 'Exited at Specified Time'}:
        if remaining != rule['exitAtDte']:
            if next_session:
                warnings.append('Verified holiday: observed next-session scheduled exit convention')
            elif VERIFIED_PRIOR_SESSION_EXITS.get(deadline) == close_stamp:
                warnings.append('Verified holiday: scheduled exit moved to prior '
                                + ('shortened session' if row['timeClosed'] == '12:59:00' else 'regular session'))
            else:
                errors.append('Scheduled exit differs from configured DTE; calendar exception unverified')
        if row['timeClosed'] != rule['exitTime'] and row['timeClosed'] != '12:59:00':
            errors.append('Unexpected scheduled exit time')
    elif reason == 'Backtest Completed':
        end = datetime.fromisoformat(parameters['rangeEnd'])
        if not 0 <= (end.date() - closed.date()).days <= 4:
            errors.append('Unexpected test-boundary close')
    elif reason != 'Expired':
        errors.append('Unrecognized exit reason: ' + reason)
    return {'errors': sorted(set(errors)), 'warnings': sorted(set(warnings)),
            'actual_dte': dte, 'remaining_dte': remaining}


def audit_job(job, result, log):
    p = job['spec']['parameters']
    trades = trade_rows(log)
    errors, warnings = [], []
    if len(trades) != log.get('totalCount', len(trades)) or len(trades) != result['numberOfTrades']:
        errors.append('Trade log incomplete or count mismatch')
    if abs(sum(t['profit'] for t in trades) - result['profit']) > .51:
        errors.append('Trade profit reconciliation failed')
    details, dtes, remaining_dtes = [], [], []
    for t in trades:
        check = audit_trade(p, t)
        errors.extend(check['errors'])
        warnings.extend(check['warnings'])
        if 'actual_dte' in check:
            dtes.append(check['actual_dte'])
            remaining_dtes.append(check['remaining_dte'])
        if check['errors'] or check['warnings']:
            details.append({'opened': t['dateOpened'], 'closed': t['dateClosed'], **check})
    risks = [trade_risk(p, t) for t in trades]
    wins = sum(max(t['profit'], 0) for t in trades)
    losses = -sum(min(t['profit'], 0) for t in trades)
    mfes = [t['highestProfitPercentage'] for t in trades if t.get('highestProfitPercentage') is not None]
    return {
        'name': job['spec']['name'], 'asset': p['underlying'],
        'phase': job['spec'].get('phase', 'pilot'), 'variant': job['spec'].get('variant'),
        'side': p['legs'][0]['type'], 'regime': job['spec'].get('regime', 'none'),
        'target_dte': p['legs'][0]['daysToExpiration'], 'delta': p['legs'][0]['delta'],
        'profit_target': p['exitConditions'][0]['maxProfit'], 'stop_loss': p['exitConditions'][0]['maxLoss'],
        'profit': result['profit'], 'trades': len(trades), 'drawdown': result['maxDrawdownPercentage'],
        'cagr': result['cagr'], 'profit_factor': wins / losses if losses else None,
        'gross_gains': wins, 'gross_losses': losses,
        'mean_premium_R': mean(r['premium_R'] for r in risks) if risks else None,
        'mean_stop_R': mean(r['stop_R'] for r in risks) if risks else None,
        'largest_premium': max((r['premium'] for r in risks), default=0),
        'actual_entry_dte_min': min(dtes, default=None),
        'actual_entry_dte_max': max(dtes, default=None),
        'minimum_exit_dte': min(remaining_dtes, default=None),
        'worst_loss': max((max(-t['profit'], 0) for t in trades), default=0),
        'largest_win': max((max(t['profit'], 0) for t in trades), default=0),
        'profit_without_best_trade': result['profit'] - max((max(t['profit'], 0) for t in trades), default=0),
        'median_mfe_pct': median(mfes) if mfes else None, 'max_mfe_pct': max(mfes, default=None),
        'mfe_ge_100_count': sum(v >= 100 for v in mfes),
        'mfe_ge_500_count': sum(v >= 500 for v in mfes),
        'mfe_ge_1000_count': sum(v >= 1000 for v in mfes),
        'exits': dict(Counter(t['reasonClosed'] for t in trades)),
        'audit_errors': sorted(set(errors)), 'audit_warnings': sorted(set(warnings)),
        'audit_details': details, 'runId': job['runId'],
    }


def summarize(output):
    output = Path(output)
    rows, batches = [], []
    for manifest in output.glob('*-jobs.json'):
        jobs = json.loads(manifest.read_text())
        batches.append({'batch': manifest.stem, 'states': dict(Counter(j['state'] for j in jobs)),
                        'failures': [{'name': j['spec']['name'], 'error': j.get('error')} for j in jobs if j['state'] == 'failed']})
        for job in jobs:
            if job['state'] != 'done':
                continue
            name = job['spec']['name']
            result = json.loads((output / (name + '-results.json')).read_text())
            log = json.loads((output / (name + '-trades.json')).read_text())
            rows.append(audit_job(job, result, log))
    write_json(output / 'asset-audit.json', rows)
    write_json(output / 'asset-batch-coverage.json', batches)
    if rows:
        keys = [key for key in rows[0] if key not in {'audit_details', 'exits'}]
        with (output / 'asset-results.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=keys, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
    print('Audited', len(rows), 'runs; errors', sum(bool(r['audit_errors']) for r in rows),
          'warnings', sum(bool(r['audit_warnings']) for r in rows))
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    summarize(parser.parse_args().output)
