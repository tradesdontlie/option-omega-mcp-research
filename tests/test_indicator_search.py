import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from research.asset_search import base_spec
from research.indicator_search import configure, eligible_dates, record_sources


class IndicatorTests(unittest.TestCase):
    def test_asset_filters_do_not_change_option_structure(self):
        p = base_spec('QQQ', 'put', 'trend')['parameters']
        q = configure(p, 'rsi')
        self.assertEqual((q['entryMinRsi'], q['entryMaxRsi']), (30, 50))
        self.assertEqual(q['legs'], p['legs'])
        self.assertEqual(q['exitConditions'], p['exitConditions'])
        self.assertNotIn('entryMinRsi', p)

    def test_nq_uses_only_precomputed_pre_entry_features(self):
        rows = [dict(date='2024-01-02', morning_return='0.01', signal_close='100',
                     vwap='99', momentum15='1'),
                dict(date='2024-01-03', morning_return='0.01', signal_close='98',
                     vwap='99', momentum15='1')]
        self.assertEqual(eligible_dates(rows, '2024-01-01', '2024-12-31', 'Call', True), ['2024-01-02'])
        self.assertEqual(len(eligible_dates(rows, '2024-01-01', '2024-12-31', 'Call', False)), 2)

    def test_empty_filter_never_becomes_unrestricted(self):
        with self.assertRaisesRegex(ValueError, 'No eligible dates'):
            eligible_dates([], '2024-01-01', '2024-12-31', 'Call', True)

    def test_modified_grid_prevents_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'asset-optimization-tasks.json').write_text(json.dumps([{'name': 'grid-QQQ'}]))
            grid = root / 'grid-QQQ-optimization-results.json'
            grid.write_text('{"cells": []}')
            features = root / 'features.csv'
            features.write_text('date\n2024-01-02\n')
            record_sources(root, features, root)
            grid.write_text('{"cells": [1]}')
            with self.assertRaisesRegex(RuntimeError, 'Source evidence changed'):
                record_sources(root, features, root)
