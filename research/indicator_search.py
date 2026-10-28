"""Fixed asset-indicator ablations with matched pre-entry NQ calendars."""

import argparse
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import time

from research.asset_search import ASSETS, choose_rows, freeze, period, result_rows, save_private
from research.batch import run_batch, write_json
from research.holdout import apply_coordinates
from research.mcp_client import OmegaClient

PROFILES = ['baseline', 'rsi', 'ema', 'ivp']


def configure(parameters, profile):
    p = deepcopy(parameters)
    call = p['legs'][0]['type'] == 'Call'
    if profile == 'rsi':
        p.update(entryMinRsi=50 if call else 30, entryMaxRsi=70 if call else 50)
    elif profile == 'ema':
        p.update(entryIntradayEmaLength=50, entryIntradayEmaCondition='AboveSma' if call else 'BelowSma')
    elif profile == 'ivp':
        p.update(entryMaxIvPercentile=50)
    elif profile != 'baseline':
        raise ValueError('Unknown indicator profile')
    return p


def eligible_dates(features, start, end, side, nq):
    dates = []
    direction = 1 if side == 'Call' else -1
    for row in features:
        if not start <= row['date'] <= end:
            continue
        aligned = (direction * float(row['morning_return']) > 0
                   and direction * (float(row['signal_close']) - float(row['vwap'])) > 0
                   and direction * float(row['momentum15']) > 0)
        if not nq or aligned:
            dates.append(row['date'])
    if not dates:
        raise ValueError('No eligible dates; refusing unrestricted fallback')
    return sorted(set(dates))


def dated_spec(spec, features, phase):
    result = deepcopy(spec)
    p = result['parameters']
    start, end = period(spec['asset'], 'screen' if phase == 'training' else phase)
    p.update(rangeStart=start, rangeEnd=end, weeklyEntries=[],
             specificEntryDays=','.join(eligible_dates(features, start, end, p['legs'][0]['type'], spec['nq'])))
    result.update(phase=phase, signal='nq' if spec['nq'] else 'coverage')
    return result


def training_specs(client, source, output, features):
    manifest = output / 'indicator-training-specs.json'
    if manifest.exists():
        return json.loads(manifest.read_text())
    tasks = json.loads((source / 'asset-optimization-tasks.json').read_text())
    specs = []
    for task in tasks:
        if task['state'] != 'done':
            raise RuntimeError('Training grid incomplete')
        saved = client.call('get_saved_backtest', {'savedBacktestId': task['savedBacktestId']})
        write_json(output / (task['asset'] + '-source-settings.json'), saved)
        grid = json.loads((source / (task['name'] + '-optimization-results.json')).read_text())
        cell = choose_rows(grid['cells'], 20, 1)[0]
        base = task['selected']['spec']
        parameters = apply_coordinates(base['parameters'], cell['coordinate'])
        for profile in PROFILES:
            for nq in [False, True]:
                name = f"training-{task['asset']}-{profile}-{'nq' if nq else 'plain'}"
                spec = dict(base, name=name, profile=profile, nq=nq, base_parameters=parameters,
                            source_cell=cell['index'], parameters=configure(parameters, profile))
                specs.append(dated_spec(spec, features, 'training'))
    write_json(manifest, specs)
    return specs


def validation_specs(output, jobs, features):
    rows = result_rows(output, jobs)
    specs = []
    for asset in ASSETS:
        choices = [r for r in rows if r['spec']['asset'] == asset]
        selected = choose_rows(choices, 20, 2)
        baseline = next((r for r in choices if r['spec']['profile'] == 'baseline' and not r['spec']['nq']), None)
        if baseline is not None and baseline not in selected:
            selected.append(baseline)
        for row in selected:
            spec = dated_spec(row['spec'], features, 'validation')
            spec['name'] = spec['name'].replace('training-', 'validation-', 1)
            spec['training_result'] = {k: v for k, v in row.items() if k != 'spec'}
            specs.append(spec)
    return specs


def confirmation_specs(frozen, features):
    specs = []
    for asset, candidate in frozen['candidates'].items():
        for variant in ['base', 'stress', 'control', 'small']:
            spec = deepcopy(candidate['spec'])
            if variant == 'control':
                spec.update(parameters=deepcopy(spec['base_parameters']), profile='baseline', nq=False)
            spec = dated_spec(spec, features, 'confirmation')
            spec.update(name=f'confirmation-{asset}-{variant}', variant=variant)
            p = spec['parameters']
            if variant == 'stress':
                p.update(entrySlippage=.20, exitSlippage=.20, stopLossSlippage=.20)
            elif variant == 'small':
                p.update(startingFunds=10000, maxAllocationAmount=500)
            specs.append(spec)
    return specs


def record_sources(source, features_path, output):
    tasks_path = source / 'asset-optimization-tasks.json'
    tasks = json.loads(tasks_path.read_text())
    grids = [source / (t['name'] + '-optimization-results.json') for t in tasks]
    provenance = {'source_study': str(source.resolve()), 'features': str(features_path.resolve()),
                  'features_sha256': hashlib.sha256(features_path.read_bytes()).hexdigest(),
                  'task_manifest_sha256': hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
                  'grid_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in grids}}
    manifest = output / 'indicator-sources.json'
    if manifest.exists() and json.loads(manifest.read_text()) != provenance:
        raise RuntimeError('Source evidence changed; refusing resume')
    write_json(manifest, provenance)
    return provenance


def run(source, features_path, output, wait):
    if wait:
        deadline = time.monotonic() + 7200
        while not (source / 'asset-report-facts.json').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Baseline study did not finish')
            time.sleep(10)
    if not (source / 'asset-report-facts.json').exists():
        raise RuntimeError('Finish baseline study before indicator tests')
    output.mkdir(parents=True, exist_ok=True)
    with features_path.open(newline='') as stream:
        features = list(csv.DictReader(stream))
    record_sources(source, features_path, output)
    client = OmegaClient(output)
    try:
        write_json(output / 'indicator-start-usage.json', client.call('get_usage_limits'))
        train = run_batch(client, training_specs(client, source, output, features), output, 'training')
        validation = validation_specs(output, train, features)
        write_json(output / 'indicator-validation-specs.json', validation)
        jobs = run_batch(client, validation, output, 'validation')
        frozen = freeze(output, jobs)
        confirmation = confirmation_specs(frozen, features)
        path = output / 'indicator-confirmation-specs.json'
        if path.exists() and json.loads(path.read_text()) != confirmation:
            raise RuntimeError('Frozen indicator confirmation changed')
        write_json(path, confirmation)
        write_json(output / 'indicator-confirmation-hash.json', {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'frozen_at': frozen['frozen_at']})
        completed = run_batch(client, confirmation, output, 'confirmation')
        for row in result_rows(output, completed):
            if row['spec']['variant'] == 'base':
                row['name'] = 'indicator-' + row['name']
                save_private(client, output, row)
        write_json(output / 'indicator-final-usage.json', client.call('get_usage_limits'))
        print('INDICATOR SEARCH COMPLETE', flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('features', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    run(args.source, args.features, args.output, args.wait)
