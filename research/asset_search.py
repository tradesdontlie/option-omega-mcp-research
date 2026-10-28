"""Autonomous, staged MCP search with asset-specific chronological selection."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from research.batch import run_batch, strategy, write_json
from research.holdout import apply_coordinates
from research.mcp_client import OmegaClient
from research.workflows import wait_optimizer

ASSETS = ['SPX', 'SPY', 'QQQ', 'IWM', 'AAPL', 'TSLA', 'MSFT', 'NVDA', 'AMZN', 'GLD', 'TLT', 'IBIT']


def period(asset, phase):
    if asset == 'IBIT':
        return {'screen': ('2024-11-19', '2025-06-30'),
                'validation': ('2025-07-01', '2025-12-31'),
                'confirmation': ('2026-01-01', '2026-09-25')}[phase]
    return {'screen': ('2018-01-01', '2023-12-31'),
            'validation': ('2024-01-01', '2024-12-31'),
            'confirmation': ('2025-01-01', '2026-09-25')}[phase]


def base_spec(asset, side, regime):
    p = strategy(side + '_long', dte=42, funds=25000, slippage=0.10)
    start, end = period(asset, 'screen')
    p.update(underlying=asset, rangeStart=start, rangeEnd=end,
             weeklyEntries=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
             capProfits=True, requireTwoPricesForProfitTarget=True)
    p['legs'][0]['delta'] = 15 if asset == 'SPX' else 35
    p['exitConditions'][0].pop('exitAtDit')
    p['exitConditions'][0].update(maxProfit=100, maxLoss=50, exitAtDte=7)
    if regime == 'trend':
        p.update(entrySmaLength=50, entrySmaCondition='AboveSma' if side == 'call' else 'BelowSma')
    return {'name': f'screen-{asset}-{side}-{regime}', 'asset': asset, 'side': side,
            'regime': regime, 'phase': 'screen', 'signal': 'weekly', 'parameters': p}


def choose_rows(rows, minimum_trades, limit):
    def score(row):
        qualified = row.get('profit', 0) > 0 and row.get('numberOfTrades', 0) >= minimum_trades
        return (qualified, row.get('numberOfTrades', 0) >= minimum_trades,
                row.get('marRatio') if row.get('marRatio') is not None else -1e9,
                row.get('profit', 0))
    return sorted(rows, key=score, reverse=True)[:limit]


def result_rows(output, jobs):
    rows = []
    for job in jobs:
        if job['state'] == 'done':
            result = json.loads((output / (job['spec']['name'] + '-results.json')).read_text())
            rows.append(dict(result, name=job['spec']['name'], spec=job['spec'], runId=job['runId']))
    return rows


def save_private(client, output, row):
    path = output / (row['name'] + '-saved.json')
    if path.exists():
        saved = json.loads(path.read_text())
        if 'savedBacktestId' not in saved:
            raise RuntimeError('Reconcile uncertain save: ' + row['name'])
        return saved['savedBacktestId']
    write_json(path, {'state': 'submitting', 'runId': row['runId']})
    saved = client.call('save_backtest', {
        'runId': row['runId'], 'name': 'TDL Asset Search - ' + row['name'],
        'description': 'Staged historical research. Training/validation/confirmation are separate; not a trading recommendation.',
        'tags': ['TDL-research', 'asset-search'], 'isShared': False,
    })
    write_json(path, saved)
    return saved['savedBacktestId']


def screens(client, output):
    specs = [base_spec(a, side, regime) for a in ASSETS
             for side in ['call', 'put'] for regime in ['none', 'trend']]
    jobs = run_batch(client, specs, output, 'screen')
    corrections = []
    for job in jobs:
        if job['state'] != 'failed':
            continue
        match = re.search(r'before (\d{4}-\d{2}-\d{2}), the earliest', job.get('error', ''))
        if match and match[1] < job['spec']['parameters']['rangeEnd']:
            spec = deepcopy(job['spec'])
            spec['name'] += '-available-history'
            spec['parameters']['rangeStart'] = match[1]
            corrections.append(spec)
    if corrections:
        jobs += run_batch(client, corrections, output, 'screen-corrections')
    return jobs


def optimize(client, output, jobs):
    path = output / 'asset-optimization-tasks.json'
    tasks = json.loads(path.read_text()) if path.exists() else []
    rows = result_rows(output, jobs)
    for asset in ASSETS:
        task = next((t for t in tasks if t['asset'] == asset), None)
        if task:
            wait_optimizer(client, output, task, tasks, path)
            continue
        eligible = [r for r in rows if r['spec']['asset'] == asset]
        if not eligible:
            raise RuntimeError('No usable screen for ' + asset)
        selected = choose_rows(eligible, 20, 1)[0]
        saved_id = save_private(client, output, selected)
        inventory = client.call('list_optimizer_parameters', {'savedBacktestId': saved_id})
        write_json(output / (asset + '-optimizer-parameters.json'), inventory)
        dimensions = [
            {'parameterPath': 'legs.0.daysToExpiration', 'start': 21, 'step': 21, 'end': 63},
            {'parameterPath': 'legs.0.delta', 'start': 10 if asset == 'SPX' else 15,
             'step': 10 if asset == 'SPX' else 20, 'end': 30 if asset == 'SPX' else 55},
            {'parameterPath': 'exitConditions.0.maxProfit', 'start': 100, 'step': 100, 'end': 1000},
        ]
        paths = {p['parameterPath'] for p in inventory['parameters']}
        if not all(d['parameterPath'] in paths for d in dimensions):
            raise RuntimeError('Required optimizer path unavailable: ' + asset)
        p = selected['spec']['parameters']
        request = {'savedBacktestId': saved_id, 'dimensions': dimensions,
                   'rangeStart': p['rangeStart'], 'rangeEnd': p['rangeEnd'], 'startingFunds': 25000}
        task = {'asset': asset, 'name': 'grid-' + asset, 'selected': selected,
                'savedBacktestId': saved_id, 'request': request, 'state': 'submitting'}
        tasks.append(task)
        write_json(path, tasks)
        started = client.call('run_optimizer', request)
        task.update(start=started, state='started',
                    next_poll=time.time() + (started.get('pollAfterMs') or 60000) / 1000)
        write_json(path, tasks)
        print('GRID START', asset, started, flush=True)
        wait_optimizer(client, output, task, tasks, path)
    return tasks


def validation_specs(output, tasks):
    specs = []
    for task in tasks:
        if task['state'] != 'done':
            raise RuntimeError('Optimizer did not complete: ' + task['asset'])
        base = task['selected']['spec']
        result = json.loads((output / (task['name'] + '-optimization-results.json')).read_text())
        cells = choose_rows(result['cells'], 20, 2)
        choices = [('baseline', base['parameters'])] + [
            ('cell-' + str(c['index']), apply_coordinates(base['parameters'], c['coordinate'])) for c in cells]
        seen = set()
        for label, parameters in choices:
            params = deepcopy(parameters)
            start, end = period(task['asset'], 'validation')
            params.update(rangeStart=start, rangeEnd=end)
            fingerprint = json.dumps(params, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            specs.append(dict(base, name='validation-' + task['asset'] + '-' + label,
                              phase='validation', parameters=params, training_family=base['name']))
    return specs


def freeze(output, jobs):
    path = output / 'asset-finalists.json'
    if path.exists():
        return json.loads(path.read_text())
    rows = result_rows(output, jobs)
    selected = {}
    for asset in ASSETS:
        row = choose_rows([r for r in rows if r['spec']['asset'] == asset], 8, 1)[0]
        selected[asset] = {'spec': row['spec'], 'validation_result': {k: v for k, v in row.items() if k != 'spec'},
                           'validation_qualified': row['profit'] > 0 and row['numberOfTrades'] >= 8}
    frozen = {'frozen_at': datetime.now(timezone.utc).isoformat(), 'candidates': selected,
              'warning': 'Prior research observed some confirmation years. No pristine holdout claim.'}
    write_json(path, frozen)
    return frozen


def confirmation_specs(frozen):
    specs = []
    for asset, candidate in frozen.items():
        for variant in ['base', 'stress', 'control', 'small']:
            original = candidate['spec']
            p = deepcopy(original['parameters'])
            start, end = period(asset, 'confirmation')
            p.update(rangeStart=start, rangeEnd=end)
            if variant == 'stress':
                p.update(entrySlippage=.20, exitSlippage=.20, stopLossSlippage=.20)
            elif variant == 'control':
                p.pop('entrySmaCondition', None)
                p.pop('entrySmaLength', None)
            elif variant == 'small':
                p.update(startingFunds=10000, maxAllocationAmount=500)
            specs.append(dict(original, name=f'confirmation-{asset}-{variant}', phase='confirmation',
                              variant=variant, parameters=p))
    return specs


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    client = OmegaClient(output)
    try:
        write_json(output / 'search-start-usage.json', client.call('get_usage_limits'))
        jobs = screens(client, output)
        tasks = optimize(client, output, jobs)
        specs = validation_specs(output, tasks)
        write_json(output / 'asset-validation-specs.json', specs)
        validation = run_batch(client, specs, output, 'validation')
        frozen = freeze(output, validation)
        specs = confirmation_specs(frozen['candidates'])
        path = output / 'asset-confirmation-specs.json'
        if path.exists() and json.loads(path.read_text()) != specs:
            raise RuntimeError('Frozen confirmation treatment changed')
        write_json(path, specs)
        write_json(output / 'asset-confirmation-hash.json', {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'frozen_at': frozen['frozen_at'], 'purpose': 'Exact confirmation treatments',
        })
        completed = run_batch(client, specs, output, 'confirmation')
        for row in result_rows(output, completed):
            if row['spec']['variant'] == 'base':
                save_private(client, output, row)
        write_json(output / 'search-final-usage.json', client.call('get_usage_limits'))
        print('ASSET SEARCH COMPLETE', flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    main(parser.parse_args().output)
