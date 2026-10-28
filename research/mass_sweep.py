"""Run bounded native MCP grids and export profit-ranked research evidence."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import time

from research.asset_search import ASSETS, period, result_rows, save_private
from research.batch import strategy, run_batch, write_json
from research.mcp_client import OmegaClient


STRUCTURES = ('call_long', 'put_long', 'call_debit', 'put_debit', 'call_credit', 'put_credit')


def definitions():
    specs = []
    for asset in ASSETS:
        for structure in STRUCTURES:
            for horizon in ('short', 'long'):
                single = structure.endswith('long')
                credit = structure.endswith('credit')
                dte = (7 if single else 0) if horizon == 'short' else (42 if single else 7)
                p = strategy(structure, dte=dte, hold=2, funds=25000, slippage=.10)
                start, end = period(asset, 'screen')
                # First available dates are derived from previous server-accepted runs.
                p.update(underlying=asset, rangeStart=start, rangeEnd=end,
                         weeklyEntries=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
                         capProfits=True, requireTwoPricesForProfitTarget=True)
                p['exitConditions'][0].update(maxProfit=30 if credit else 100, maxLoss=100 if credit else 50)
                if horizon == 'long':
                    bullish = structure in ('call_long', 'call_debit', 'put_credit')
                    p.update(entrySmaLength=50, entrySmaCondition='AboveSma' if bullish else 'BelowSma')
                    if single:
                        p['exitConditions'][0].pop('exitAtDit', None)
                        p['exitConditions'][0]['exitAtDte'] = 7
                dimensions = [
                    {'parameterPath': 'legs.0.delta', 'start': 15, 'end': 55, 'step': 20},
                    {'parameterPath': 'exitConditions.0.maxProfit', 'start': 10 if credit else 100,
                     'end': 70 if credit else 700, 'step': 10 if credit else 100},
                    {'parameterPath': 'entryTime', 'start': 629, 'end': 869, 'step': 120},
                ]
                specs.append({'name': f'mass-{asset}-{structure}-{horizon}', 'asset': asset,
                              'structure': structure, 'horizon': horizon, 'signal': 'weekly',
                              'parameters': p, 'dimensions': dimensions})
    return specs


def grid_count(dimensions):
    count = 1
    for d in dimensions:
        if d['step'] == 0 or (d['end'] - d['start']) / d['step'] < 0:
            raise ValueError('Invalid grid step')
        n = (d['end'] - d['start']) / d['step']
        if not math.isclose(n, round(n)):
            raise ValueError('Grid endpoint is not on the step')
        count *= round(n) + 1
    return count


def validate_grid(dimensions, inventory):
    available = {p['parameterPath']: p for p in inventory['parameters']}
    limits = inventory['limits']
    if len(dimensions) > limits['maxDimensions'] or grid_count(dimensions) > limits['maxTotalIterations']:
        raise ValueError('Optimizer grid exceeds provider caps')
    for d in dimensions:
        info = available.get(d['parameterPath'])
        if info is None or info['valueType'] == 'sequence':
            raise ValueError('Unavailable or unsupported optimizer dimension: ' + d['parameterPath'])
        if grid_count([d]) > limits['maxStepsPerDimension']:
            raise ValueError('Too many dimension steps')
        if info['valueType'] == 'time' and not (
                limits['minTimeMinutes'] <= min(d['start'], d['end']) <=
                max(d['start'], d['end']) <= limits['maxTimeMinutes']):
            raise ValueError('Time outside provider window')


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def ranked(rows):
    return sorted(rows, key=lambda r: r['profit'] if not r.get('error') and finite(r.get('profit'))
                  else -math.inf, reverse=True)


def eligible(row, minimum):
    return (not row.get('error') and finite(row.get('profit')) and row['profit'] > 0
            and (row.get('numberOfTrades') or 0) >= minimum
            and finite(row.get('maxDrawdownPercentage')) and row['maxDrawdownPercentage'] >= -25)


def all_result_rows(output, jobs):
    rows = result_rows(output, jobs)
    rows.extend({'name': j['spec']['name'], 'spec': j['spec'], 'profit': None,
                 'state': j['state'], 'error': j.get('error') or j['state']}
                for j in jobs if j['state'] != 'done')
    return rows


def completion_status(progress, counts):
    partial = (progress['completed_cells'] != 9072 or progress.get('failed_cells', 0)
               or progress.get('failed_grids', 0) or counts.get('failed_runs', 0))
    return ('partial coverage; ' if partial else 'completed; ') + 'numerical survivors require audit'


def fetch_cells(client, optimization_id, expected):
    cells, seen_offsets = [], set()
    offset = 0
    result = {}
    while True:
        if offset in seen_offsets:
            raise ValueError('Repeated optimizer page')
        seen_offsets.add(offset)
        result = client.call('get_optimization_results', {
            'optimizationId': optimization_id, 'limit': 500, 'offset': offset,
            'sortBy': 'profit', 'direction': 'desc'})
        cells.extend(result['cells'])
        if result['totalCount'] != expected:
            raise ValueError('Provider grid count differs from protocol')
        offset = result.get('nextOffset')
        if offset is None:
            break
    if len(cells) != expected or len({c['index'] for c in cells}) != expected:
        raise ValueError('Incomplete or duplicate optimizer cells')
    return dict(result, cells=ranked(cells), offset=0, nextOffset=None)


def coordinates(parameters, cells):
    p = deepcopy(parameters)
    for c in cells:
        parts = c['parameterPath'].split('.')
        target = p
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        value = c['value']
        if c['parameterPath'] == 'entryTime':
            minute = int(value)
            value = f'{minute // 60:02d}:{minute % 60:02d}:00'
        target[parts[-1]] = value
    return p


def export(output, rows, stem):
    rows = ranked(rows)
    write_json(output / (stem + '.json'), rows)
    fields = sorted({key for row in rows for key in row if key != 'spec'})
    path = output / (stem + '.csv')
    temporary = path.with_suffix('.csv.tmp')
    with temporary.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                             for k, v in row.items() if k != 'spec'})
    temporary.replace(path)


def rankings(output, tasks):
    rows = []
    for task in tasks:
        path = output / (task['name'] + '-grid.json')
        if task['state'] == 'done':
            for cell in json.loads(path.read_text())['cells']:
                rows.append(dict(cell, grid=task['name'], asset=task['spec']['asset'],
                                 structure=task['spec']['structure'], horizon=task['spec']['horizon'],
                                 optimizationId=task['start']['optimizationId'], stage='development',
                                 qualification='unvalidated'))
    export(output, rows, 'ranked-all-development')
    for asset in ASSETS:
        subset = [r for r in rows if r['asset'] == asset]
        export(output, subset, 'ranked-' + asset + '-development')
        for structure in STRUCTURES:
            export(output, [r for r in subset if r['structure'] == structure],
                   f'ranked-{asset}-{structure}-development')
    write_json(output / 'progress.json', {
        'updated_at': datetime.now(timezone.utc).isoformat(), 'planned_cells': 9072,
        'completed_cells': len(rows), 'completed_grids': sum(t['state'] == 'done' for t in tasks),
        'successful_cells': sum(not r.get('error') and finite(r.get('profit')) for r in rows),
        'failed_cells': sum(bool(r.get('error')) or not finite(r.get('profit')) for r in rows),
        'failed_grids': sum(t['state'] == 'failed' for t in tasks),
        'status': 'research in progress', 'warning': 'Profit rankings are not validated strategies.'})


def optimize(client, output, spec, tasks):
    task_path = output / 'mass-tasks.json'
    task = next((t for t in tasks if t['name'] == spec['name']), None)
    if task is None:
        baseline = run_batch(client, [spec], output, spec['name'] + '-baseline')[0]
        if baseline['state'] != 'done':
            task = {'name': spec['name'], 'spec': spec, 'state': 'failed', 'baseline': baseline}
            tasks.append(task)
            write_json(task_path, tasks)
            return
        saved_id = save_private(client, output, {'name': spec['name'], 'runId': baseline['runId']})
        inventory = client.call('list_optimizer_parameters', {'savedBacktestId': saved_id})
        write_json(output / (spec['name'] + '-parameters.json'), inventory)
        validate_grid(spec['dimensions'], inventory)
        usage = client.call('get_usage_limits')
        if usage['optimizations']['remaining'] < 1:
            raise RuntimeError('Daily optimization quota exhausted; resume after reset')
        request = {'savedBacktestId': saved_id, 'dimensions': spec['dimensions'],
                   'rangeStart': spec['parameters']['rangeStart'],
                   'rangeEnd': spec['parameters']['rangeEnd'], 'startingFunds': 25000}
        task = {'name': spec['name'], 'spec': spec, 'request': request, 'state': 'submitting'}
        tasks.append(task)
        write_json(task_path, tasks)
        started = client.call('run_optimizer', request)
        task.update(start=started, state='started',
                    next_poll=time.time() + (started.get('pollAfterMs') or 60000) / 1000)
        write_json(task_path, tasks)
    if task['state'] == 'submitting':
        raise RuntimeError('Uncertain optimization submission: reconcile ID before resuming ' + task['name'])
    deadline = time.monotonic() + 2400
    while task['state'] == 'started':
        if time.monotonic() > deadline:
            raise TimeoutError('Optimizer stalled; safe to resume recorded ID')
        delay = task['next_poll'] - time.time()
        if delay > 0:
            time.sleep(min(10, delay))
            continue
        status = client.call('get_optimization_status', {'optimizationId': task['start']['optimizationId']})
        task['last_status'] = status
        task['next_poll'] = time.time() + max(1, (status.get('pollAfterMs') or 0) / 1000)
        if status['status'] == 'complete':
            result = fetch_cells(client, task['start']['optimizationId'], grid_count(spec['dimensions']))
            write_json(output / (task['name'] + '-grid.json'), result)
            task['state'] = 'done'
        elif status['status'] in ('failed', 'cancelled'):
            task['state'] = 'failed'
        write_json(task_path, tasks)
    rankings(output, tasks)
    print('GRID', task['name'], task['state'], flush=True)


def freeze_file(path, value):
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != value:
            raise RuntimeError('Frozen protocol or candidate set changed: ' + str(path))
    else:
        write_json(path, value)
    return value


def later_stages(client, output, tasks):
    candidates = []
    for task in tasks:
        if task['state'] != 'done':
            continue
        cells = json.loads((output / (task['name'] + '-grid.json')).read_text())['cells']
        for cell in [c for c in ranked(cells) if eligible(c, 30)][:2]:
            spec = deepcopy(task['spec'])
            spec.pop('dimensions')
            spec.update(name=f"{task['name']}-cell{cell['index']}-validation", grid=task['name'],
                        training=cell, phase='validation')
            spec['parameters'] = coordinates(spec['parameters'], cell['coordinate'])
            start, end = period(spec['asset'], 'validation')
            spec['parameters'].update(rangeStart=start, rangeEnd=end)
            candidates.append(spec)
    freeze_file(output / 'frozen-validation.json', candidates)
    jobs = run_batch(client, candidates, output, 'mass-validation')
    rows = all_result_rows(output, jobs)
    failed_runs = sum(j['state'] != 'done' for j in jobs)
    export(output, rows, 'ranked-validation')
    chosen = []
    for task in tasks:
        matching = [r for r in ranked(rows) if r['spec']['grid'] == task['name'] and eligible(r, 10)]
        if matching:
            chosen.append(matching[0])
    freeze_file(output / 'frozen-confirmation.json', chosen)
    specs = []
    for row in chosen:
        for variant in ('base', 'stress', 'small'):
            spec = deepcopy(row['spec'])
            spec.update(name=spec['name'].replace('-validation', '-confirmation-' + variant),
                        phase='confirmation', variant=variant, validation_profit=row['profit'])
            p = spec['parameters']
            start, end = period(spec['asset'], 'confirmation')
            p.update(rangeStart=start, rangeEnd=end)
            if variant == 'stress':
                p.update(entrySlippage=.20, exitSlippage=.20, stopLossSlippage=.20)
            if variant == 'small':
                p.update(startingFunds=10000, maxAllocationAmount=500)
            specs.append(spec)
    freeze_file(output / 'confirmation-specs.json', specs)
    jobs = run_batch(client, specs, output, 'mass-confirmation')
    rows = all_result_rows(output, jobs)
    failed_runs += sum(j['state'] != 'done' for j in jobs)
    export(output, rows, 'ranked-confirmation')
    for asset in ASSETS:
        export(output, [r for r in rows if r['spec']['asset'] == asset], 'ranked-' + asset + '-confirmation')
    survivors = []
    for chosen_row in chosen:
        group = [r for r in rows if r['spec']['grid'] == chosen_row['spec']['grid']]
        variants = {r['spec']['variant']: r for r in group}
        if all(v in variants and eligible(variants[v], 20) for v in ('base', 'stress')):
            survivors.append(dict(variants['base'], stress_profit=variants['stress']['profit'],
                                  qualification='numerical screen only; trade audit and forward test pending'))
    export(output, survivors, 'ranked-screen-survivors')
    return {'validation_runs': len(candidates), 'confirmation_runs': len(specs),
            'failed_runs': failed_runs, 'numerical_survivors': len(survivors), 'trusted_strategies': 0}


@contextmanager
def runner_lock(output):
    # OS lock releases automatically on process death; never infer safety from PID reuse.
    import msvcrt
    path = output / 'runner.lock'
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def artifact_manifest(output):
    """Hash evidence, excluding the operational lock and manifest itself."""
    files = sorted(p for p in output.iterdir() if p.is_file()
                   and p.name not in {'artifact-manifest.json', 'runner.lock'})
    rows = [{'path': str(p), 'size': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    write_json(output / 'artifact-manifest.json', rows)
    return rows


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    with runner_lock(output):
        specs = definitions()
        freeze_file(output / 'protocol.json', {'version': 1, 'specs': specs, 'planned_cells': 9072,
                    'warning': 'Exploratory historical research. No pristine holdout or proven profitability.'})
        write_json(output / 'runner.json', {'pid': os.getpid(), 'started': datetime.now(timezone.utc).isoformat()})
        client = OmegaClient(output)
        try:
            client.inventory()
            write_json(output / 'usage-start.json', client.call('get_usage_limits'))
            path = output / 'mass-tasks.json'
            tasks = json.loads(path.read_text()) if path.exists() else []
            for spec in specs:
                optimize(client, output, spec, tasks)
            counts = later_stages(client, output, tasks)
            progress = json.loads((output / 'progress.json').read_text())
            progress.update(counts, status=completion_status(progress, counts))
            write_json(output / 'progress.json', progress)
            write_json(output / 'usage-end.json', client.call('get_usage_limits'))
        except Exception as error:
            write_json(output / 'runner-error.json', {'error': str(error), 'time': datetime.now(timezone.utc).isoformat()})
            raise
        finally:
            client.close()
        artifact_manifest(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    main(parser.parse_args().output)
