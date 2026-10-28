import unittest

from research.asset_audit import audit_trade, trade_risk
from research.asset_search import base_spec


class AssetAuditTests(unittest.TestCase):
    def row(self):
        return dict(dateOpened='2023-01-03', timeOpened='10:29:00', dateClosed='2023-01-18',
                    timeClosed='10:01:00', daysInTrade=15, underlying='QQQ',
                    legs='1 Feb 17 268 C BTO 9.62', numberOfContracts=1,
                    premiumPerContract=-972., averageClosingCostPerContract=-1944.,
                    profit=970., openingFees=1., closingFees=1., buyingPowerRequired=972.,
                    reasonClosed='Profit Target', highestProfitPercentage=103.09)

    def test_intraday_profit_exit_is_valid(self):
        p = base_spec('QQQ', 'call', 'none')['parameters']
        self.assertEqual(audit_trade(p, self.row())['errors'], [])

    def test_expiration_violates_pre_expiration_policy(self):
        r = self.row()
        r.update(dateClosed='2023-02-17', daysInTrade=45, timeClosed='16:00:00', reasonClosed='Expired')
        self.assertIn('Position reached expiration', audit_trade(base_spec('QQQ', 'call', 'none')['parameters'], r)['errors'])

    def test_R_denominators_do_not_confuse_premium_and_stop(self):
        p = base_spec('QQQ', 'call', 'none')['parameters']
        risk = trade_risk(p, self.row())
        self.assertAlmostEqual(risk['premium'], 972.)
        self.assertAlmostEqual(risk['nominal_stop_risk'], 498.)
        self.assertAlmostEqual(risk['premium_R'], 970 / 972)
        self.assertAlmostEqual(risk['stop_R'], 970 / 498)

    def test_late_profit_target_does_not_override_scheduled_exit(self):
        r = self.row()
        r.update(dateClosed='2023-02-13', daysInTrade=41)
        self.assertIn('Position remained open after configured DTE deadline',
                      audit_trade(base_spec('QQQ', 'call', 'none')['parameters'], r)['errors'])

    def test_next_year_leap_day_expiration(self):
        r = self.row()
        r.update(dateOpened='2023-12-29', dateClosed='2024-01-18', daysInTrade=20,
                 legs='1 Feb 29 268 C BTO 9.62')
        p = base_spec('QQQ', 'call', 'none')['parameters']
        p['rangeEnd'] = '2024-12-31'
        self.assertEqual(audit_trade(p, r)['actual_dte'], 62)

    def test_entry_must_belong_to_explicit_signal_calendar(self):
        p = base_spec('QQQ', 'call', 'none')['parameters']
        p['specificEntryDays'] = '2023-01-04'
        self.assertIn('Entry outside explicit signal calendar', audit_trade(p, self.row())['errors'])

    def test_verified_independence_day_early_exit(self):
        p = base_spec('SPX', 'call', 'none')['parameters']
        p.update(rangeStart='2025-01-01', rangeEnd='2025-12-31')
        p['legs'][0]['daysToExpiration'] = 21
        r = self.row()
        r.update(underlying='SPX', dateOpened='2025-06-20', dateClosed='2025-07-03',
                 daysInTrade=13, timeClosed='12:59:00', legs='1 Jul 11 6230 C BTO 9.62',
                 reasonClosed='Exited at Specified DTE')
        check = audit_trade(p, r)
        self.assertEqual(check['errors'], [])
        self.assertIn('Verified holiday: scheduled exit moved to prior shortened session', check['warnings'])

    def test_verified_2026_holiday_uses_prior_regular_session(self):
        p = base_spec('QQQ', 'put', 'none')['parameters']
        p.update(rangeStart='2026-01-01', rangeEnd='2026-12-31')
        p['legs'][0]['daysToExpiration'] = 21
        r = self.row()
        r.update(dateOpened='2026-06-04', dateClosed='2026-06-18', daysInTrade=14,
                 timeClosed='15:49:00', legs='1 Jun 26 250 P BTO 9.62', reasonClosed='Exited at Specified Time')
        self.assertEqual(audit_trade(p, r)['errors'], [])

    def test_verified_monday_holiday_adjustment_is_exact(self):
        p = base_spec('SPX', 'call', 'none')['parameters']
        p['legs'][0]['daysToExpiration'] = 21
        r = self.row()
        r.update(underlying='SPX', dateOpened='2020-02-03', dateClosed='2020-02-18',
                 daysInTrade=15, timeClosed='15:49:00', legs='1 Feb 24 3340 C BTO 9.62',
                 reasonClosed='Exited at Specified Time')
        self.assertEqual(audit_trade(p, r)['errors'], [])
        r.update(dateClosed='2020-02-19', daysInTrade=16)
        self.assertIn('Position remained open after configured DTE deadline', audit_trade(p, r)['errors'])


if __name__ == '__main__':
    unittest.main()
