import unittest

from research.asset_search import base_spec, choose_rows, confirmation_specs


class AssetSearchTests(unittest.TestCase):
    def test_long_options_have_targets_and_pre_expiration_exit(self):
        p = base_spec('QQQ', 'put', 'trend')['parameters']
        self.assertEqual(p['legs'][0]['buySell'], 'Buy')
        self.assertEqual(p['legs'][0]['type'], 'Put')
        self.assertEqual(p['exitConditions'][0]['exitAtDte'], 7)
        self.assertEqual(p['exitConditions'][0]['maxProfit'], 100)
        self.assertEqual(p['entrySmaCondition'], 'BelowSma')
        self.assertFalse(p['capLosses'])

    def test_selection_does_not_prefer_tiny_sample_winner(self):
        rows = [{'name': 'tiny', 'profit': 1000, 'numberOfTrades': 1, 'marRatio': 100},
                {'name': 'sample', 'profit': 500, 'numberOfTrades': 30, 'marRatio': 1}]
        self.assertEqual(choose_rows(rows, 20, 1)[0]['name'], 'sample')

    def test_confirmation_uses_frozen_rules_and_independent_copy(self):
        original = base_spec('QQQ', 'call', 'trend')
        frozen = {'QQQ': {'spec': original, 'validation_qualified': True}}
        specs = confirmation_specs(frozen)
        self.assertEqual(len(specs), 4)
        self.assertEqual(specs[0]['parameters']['rangeStart'], '2025-01-01')
        self.assertEqual(original['parameters']['rangeStart'], '2018-01-01')
        control = next(s for s in specs if s['variant'] == 'control')
        self.assertNotIn('entrySmaCondition', control['parameters'])
        small = next(s for s in specs if s['variant'] == 'small')
        self.assertEqual(small['parameters']['maxAllocationAmount'], 500)


if __name__ == '__main__':
    unittest.main()
