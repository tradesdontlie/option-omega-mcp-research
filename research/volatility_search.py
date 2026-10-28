"""Fixed native volatility ratios with durable, chronological MCP research."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path

from research.asset_search import ASSETS, choose_rows, result_rows, save_private
from research.batch import run_batch, write_json
from research.indicator_search import dated_spec, validation_specs, confirmation_specs
from research.mcp_client import OmegaClient

PROFILES = ['baseline', 'hv-high', 'hv-low', 'term-high', 'term-low']


def configure(parameters, profile):
    p = deepcopy(parameters)
    if profile == 'baseline':
        return p
    if profile not in PROFILES:
        raise ValueError('Unknown volatility profile')
    side = 'Min' if profile.endswith('high') else 'Max'
    metric = 'Hv30Iv30Ratio'
    if profile.startswith('term-'):
        metric = 'Vix9DVixRatio' if p['underlying'] in ['SPX', 'SPY'] else 'Iv9DIv30Ratio'
    p['entry' + side + metric] = 1.0
    return p


def source_guard(source, features_path, output):
    tasks = source / 'asset-optimization-tasks.json'
    record = {'source_study': str(source.resolve()), 'features': str(features_path.resolve()),
              'task_manifest_sha256': hashlib.sha256(tasks.read_bytes()).hexdigest(),
              'features_sha256': hashlib.sha256(features_path.read_bytes()).hexdigest(),
              'profiles': PROFILES, 'protocol': 'original-screen-structure-v1'}
    path = output / 'volatility-sources.json'
    if path.exists() and json.loads(path.read_text()) != record:
        raise RuntimeError('Source evidence changed; refusing resume')
    write_json(path, record)


def training_specs(source, features):
    tasks = json.loads((source / 'asset-optimization-tasks.json').read_text())
    specs = []
    for task in tasks:
        if task['state'] != 'done':
            raise RuntimeError('Source training selection incomplete')
        original = task['selected']['spec']
        base = original['parameters']
        for profile in PROFILES:
            spec = dict(original, name=f"training-{task['asset']}-{profile}-plain",
                        profile=profile, nq=False, base_parameters=deepcopy(base),
                        parameters=configure(base, profile))
            specs.append(dated_spec(spec, features, 'training'))
    return specs


def freeze_available(output, jobs):
    path = output / 'asset-finalists.json'
    if path.exists():
        return json.loads(path.read_text())
    rows = result_rows(output, jobs)
    candidates, unavailable = {}, {}
    for asset in ASSETS:
        choices = [r for r in rows if r['spec']['asset'] == asset]
        if not choices:
            unavailable[asset] = 'No successful validation; see batch failure evidence'
            continue
        row = choose_rows(choices, 8, 1)[0]
        candidates[asset] = {'spec': row['spec'], 'validation_result': {k: v for k, v in row.items() if k != 'spec'},
                             'validation_qualified': row['profit'] > 0 and row['numberOfTrades'] >= 8}
    frozen = {'frozen_at': datetime.now(timezone.utc).isoformat(), 'candidates': candidates,
              'unavailable_assets': unavailable, 'warning': 'Previously researched history, not a pristine holdout'}
    write_json(path, frozen)
    return frozen


def run(source, features_path, output):
    output.mkdir(parents=True, exist_ok=True)
    source_guard(source, features_path, output)
    with features_path.open(newline='') as stream:
        features = list(csv.DictReader(stream))
    client = OmegaClient(output)
    try:
        write_json(output / 'volatility-start-usage.json', client.call('get_usage_limits'))
        specs = training_specs(source, features)
        path = output / 'volatility-training-specs.json'
        if path.exists() and json.loads(path.read_text()) != specs:
            raise RuntimeError('Training specifications changed; refusing resume')
        write_json(path, specs)
        training = run_batch(client, specs, output, 'training')
        validation = validation_specs(output, training, features)
        write_json(output / 'volatility-validation-specs.json', validation)
        jobs = run_batch(client, validation, output, 'validation')
        frozen = freeze_available(output, jobs)
        specs = confirmation_specs(frozen, features)
        path = output / 'volatility-confirmation-specs.json'
        if path.exists() and json.loads(path.read_text()) != specs:
            raise RuntimeError('Frozen confirmation specifications changed')
        write_json(path, specs)
        write_json(output / 'volatility-confirmation-hash.json', {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'frozen_at': frozen['frozen_at']})
        completed = run_batch(client, specs, output, 'confirmation')
        for row in result_rows(output, completed):
            if row['spec']['variant'] == 'base':
                row['name'] = 'volatility-' + row['name']
                save_private(client, output, row)
        write_json(output / 'volatility-final-usage.json', client.call('get_usage_limits'))
        print('VOLATILITY SEARCH COMPLETE', flush=True)
    finally:
        client.close()
    from research.volatility_report import build
    build(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('features', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    run(args.source, args.features, args.output)
