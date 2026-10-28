import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.asset_search import base_spec
from research.indicator_search import confirmation_specs
from research.volatility_search import configure, source_guard, freeze_available
from research.volatility_report import build, ratio_issues


class VolatilityTests(unittest.TestCase):
    def test_exported_ratio_must_satisfy_requested_boundary(self):
        errors, warnings = ratio_issues({'entryMinHv30Iv30Ratio': 1.0}, {'openingHv30Iv30Ratio': 0.92})
        self.assertTrue(errors)
        self.assertEqual(warnings, [])
        self.assertEqual(ratio_issues({'entryMaxIv9DIv30Ratio': 1.0}, {'openingIv9DIv30Ratio': 1.0}), ([], []))

    def test_missing_vix_ratio_is_unverified_not_fabricated(self):
        errors, warnings = ratio_issues({'entryMinVix9DVixRatio': 1.0}, {'openingVix': 20})
        self.assertEqual(errors, [])
        self.assertTrue(warnings)

    def test_failed_validation_asset_is_unavailable_not_reselected(self):
        with TemporaryDirectory() as directory:
            frozen = freeze_available(Path(directory), [
                {'state': 'failed', 'spec': {'asset': 'QQQ', 'name': 'validation-QQQ-baseline'}, 'error': 'rejected'}])
            self.assertNotIn('QQQ', frozen['candidates'])
            self.assertIn('QQQ', frozen['unavailable_assets'])

    def test_failed_confirmation_variant_remains_visible(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'asset-batch-coverage.json').write_text(json.dumps([
                {'batch': 'confirmation', 'states': {'done': 3, 'failed': 1}, 'failures': [{'name': 'confirmation-QQQ-stress', 'error': 'rejected'}]}]))
            (output / 'asset-finalists.json').write_text(json.dumps({'frozen_at': 'test', 'candidates': {'QQQ': {'spec': {'profile': 'baseline'}}}}))
            (output / 'mcp-journal.jsonl').write_text('')
            audit = [{'name': 'confirmation-QQQ-' + v, 'audit_errors': [], 'runId': v} for v in ['base', 'control', 'small']]
            with patch('research.volatility_report.summarize', return_value=audit), \
                 patch('research.volatility_report.available_curves', return_value={}), \
                 patch('research.volatility_report.ASSETS', ['QQQ']), \
                 patch('research.volatility_report.Path'), patch('builtins.print'):
                build(output)
            rows = json.loads((output / 'volatility-candidates.json').read_text())
            self.assertEqual(rows[0]['status'], 'Unavailable, not qualified')
            self.assertIn('stress', ' '.join(rows[0]['reasons']))

    def test_term_structure_routes_index_proxies(self):
        for asset in ['SPX', 'SPY', 'QQQ', 'AAPL']:
            p = base_spec(asset, 'call', 'trend')['parameters']
            q = configure(p, 'term-high')
            key = 'entryMinVix9DVixRatio' if asset in ['SPX', 'SPY'] else 'entryMinIv9DIv30Ratio'
            self.assertEqual(q[key], 1.0)
            other = 'entryMinIv9DIv30Ratio' if asset in ['SPX', 'SPY'] else 'entryMinVix9DVixRatio'
            self.assertNotIn(other, q)
            self.assertEqual(q['legs'], p['legs'])
            self.assertEqual(q['exitConditions'], p['exitConditions'])
            self.assertEqual(q['entrySmaCondition'], p['entrySmaCondition'])
            self.assertNotIn(key, p)

    def test_historical_ratio_is_not_inverse(self):
        p = base_spec('QQQ', 'put', 'trend')['parameters']
        self.assertEqual(configure(p, 'hv-high')['entryMinHv30Iv30Ratio'], 1.0)
        self.assertEqual(configure(p, 'hv-low')['entryMaxHv30Iv30Ratio'], 1.0)
        with self.assertRaises(ValueError):
            configure(p, 'invented')

    def test_source_change_refuses_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / 'asset-optimization-tasks.json'
            tasks.write_text('[]')
            features = root / 'features.csv'
            features.write_text('date\n2024-01-02\n')
            source_guard(root, features, root)
            tasks.write_text('[{}]')
            with self.assertRaisesRegex(RuntimeError, 'Source evidence changed'):
                source_guard(root, features, root)

    def test_control_removes_only_new_volatility_filter(self):
        base = base_spec('QQQ', 'call', 'trend')
        spec = dict(base, profile='hv-high', nq=False, base_parameters=base['parameters'],
                    parameters=configure(base['parameters'], 'hv-high'))
        rows = [dict(date='2025-01-02', morning_return='1', signal_close='2', vwap='1', momentum15='1')]
        result = confirmation_specs({'candidates': {'QQQ': {'spec': spec}}}, rows)
        control = next(s for s in result if s['variant'] == 'control')['parameters']
        self.assertNotIn('entryMinHv30Iv30Ratio', control)
        self.assertEqual(control['entrySmaCondition'], 'AboveSma')
        self.assertEqual(control['legs'], base['parameters']['legs'])
