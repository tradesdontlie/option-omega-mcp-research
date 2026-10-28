import unittest

from research.asset_report import classification, classify_with_prior_audits


class ClassificationTests(unittest.TestCase):
    def test_report_caller_attaches_selected_validation_errors(self):
        row = dict(profit=1000, trades=25, drawdown=-10, profit_factor=1.5, audit_errors=[])
        candidate = dict(validation_qualified=True, spec={'name': 'validation-cell-1'})
        result = classify_with_prior_audits(
            dict(profit=2000, numberOfTrades=30), candidate, row, row,
            dict(ending_equity_matches=True, drawdown_matches=True),
            {'validation-cell-1': {'audit_errors': ['late exit']}})
        self.assertIn('selected training/validation audit errors', result[1])
        self.assertNotIn('earlier_audit_errors', candidate)

    def classify(self, **changes):
        base = dict(profit=1000, trades=25, drawdown=-10, profit_factor=1.5, audit_errors=[])
        stress = dict(base, **changes)
        return classification(dict(profit=2000, numberOfTrades=30),
                              dict(validation_qualified=True), base, stress,
                              dict(ending_equity_matches=True, drawdown_matches=True))

    def test_cost_stress_loss_rejects_profitable_baseline(self):
        label, reasons = self.classify(profit=-1)
        self.assertEqual(label, 'Profitable, not qualified')
        self.assertIn('cost-stress loss', reasons)

    def test_zero_trade_result_is_not_labeled_a_loss(self):
        row = dict(profit=0, trades=0, drawdown=0, profit_factor=None, audit_errors=[])
        label, reasons = classification(dict(profit=1000, numberOfTrades=30),
                                        dict(validation_qualified=True), row, row,
                                        dict(ending_equity_matches=True, drawdown_matches=True))
        self.assertEqual(label, 'No confirmation trades')
        self.assertNotIn('confirmation loss', reasons)

    def test_small_confirmation_sample_does_not_qualify(self):
        self.assertIn('fewer than 20 confirmation trades', self.classify(trades=19)[1])

    def test_clean_candidate_passes_only_all_gates(self):
        self.assertEqual(self.classify(), ('Passed research screen', []))

    def test_positive_gains_without_losses_pass_profit_factor_gate(self):
        self.assertEqual(self.classify(profit_factor=None, gross_gains=1000, gross_losses=0),
                         ('Passed research screen', []))

    def test_missing_profit_factor_without_denominator_evidence_fails(self):
        self.assertIn('profit factor below 1.1', self.classify(profit_factor=None)[1])

    def test_earlier_execution_errors_prevent_qualification(self):
        row = dict(profit=1000, trades=25, drawdown=-10, profit_factor=1.5, audit_errors=[])
        result = classification(dict(profit=2000, numberOfTrades=30),
                                dict(validation_qualified=True, earlier_audit_errors=['late exit']), row, row,
                                dict(ending_equity_matches=True, drawdown_matches=True))
        self.assertIn('selected training/validation audit errors', result[1])
