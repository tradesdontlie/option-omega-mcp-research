import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from research.mass_sweep import definitions, ranked, grid_count, eligible, fetch_cells, all_result_rows, completion_status


class MassSweepTests(unittest.TestCase):
    def test_manifest_excludes_active_lock_but_hashes_evidence(self):
        from research.mass_sweep import artifact_manifest, runner_lock
        with TemporaryDirectory() as folder:
            output = Path(folder)
            (output / 'evidence.json').write_text('{}')
            with runner_lock(output):
                rows = artifact_manifest(output)
            self.assertEqual([Path(r['path']).name for r in rows], ['evidence.json'])
            self.assertEqual(rows[0]['size'], 2)

    def test_protocol_count_and_spread_expirations(self):
        specs = definitions()
        self.assertEqual(len(specs), 144)
        self.assertEqual(sum(grid_count(s['dimensions']) for s in specs), 9072)
        self.assertEqual(len({s['name'] for s in specs}), 144)
        for s in specs:
            legs = s['parameters']['legs']
            self.assertEqual(len({leg['daysToExpiration'] for leg in legs}), 1)
            if s['structure'].endswith('credit'):
                self.assertLessEqual(s['dimensions'][1]['end'], 100)
            self.assertFalse(s['parameters']['ignoreMarginRequirements'])

    def test_profit_ranking_preserves_errors_last(self):
        rows = [{'profit': None}, {'profit': -5}, {'profit': 20}, {'profit': 0},
                {'profit': float('nan')}, {'profit': 100, 'error': 'bad'}]
        self.assertEqual([r['profit'] for r in ranked(rows)[:3]], [20, 0, -5])

    def test_screen_rejects_empty_loss_and_excess_drawdown(self):
        self.assertTrue(eligible({'profit': 1, 'numberOfTrades': 30, 'maxDrawdownPercentage': -25}, 30))
        for row in ({'profit': 1}, {'profit': -1, 'numberOfTrades': 40},
                    {'profit': 1, 'numberOfTrades': 40, 'maxDrawdownPercentage': -26}):
            self.assertFalse(eligible(row, 30))

    def test_pagination_requires_all_unique_cells(self):
        class Client:
            def call(self, tool, args):
                offset = args.get('offset', 0)
                return {'cells': [{'index': offset, 'profit': 3-offset}],
                        'totalCount': 2, 'nextOffset': 1 if offset == 0 else None}
        cells = fetch_cells(Client(), 'id', 2)['cells']
        assert isinstance(cells, list)
        self.assertEqual(len(cells), 2)
        with self.assertRaises(ValueError):
            fetch_cells(Client(), 'id', 3)

    def test_bad_grid_steps_rejected(self):
        with self.assertRaises(ValueError):
            grid_count([{'start': 1, 'end': 3, 'step': 0}])

    def test_failed_runs_remain_in_export(self):
        with TemporaryDirectory() as folder:
            rows = all_result_rows(Path(folder), [{'state': 'failed', 'error': 'rejected',
                                                  'spec': {'name': 'test', 'asset': 'QQQ'}}])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['error'], 'rejected')
            self.assertIsNone(rows[0]['profit'])

    def test_partial_results_not_labelled_complete(self):
        self.assertIn('partial', completion_status({'completed_cells': 9000, 'failed_cells': 0}, {}))
        self.assertIn('partial', completion_status({'completed_cells': 9072, 'failed_cells': 1}, {}))
        self.assertIn('partial', completion_status({'completed_cells': 9072, 'failed_cells': 0}, {'failed_runs': 1}))
        self.assertNotIn('partial', completion_status({'completed_cells': 9072, 'failed_cells': 0}, {'failed_runs': 0}))


if __name__ == '__main__':
    unittest.main()
