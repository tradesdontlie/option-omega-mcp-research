# Fixed historical study protocol

The mass study uses 12 assets: SPX, SPY, QQQ, IWM, AAPL, TSLA, MSFT, NVDA, AMZN, GLD, TLT and IBIT. Availability is study-specific, not a live inventory guarantee.

Each asset has six structures: long calls, long puts, call debit spreads, put debit spreads, call credit spreads and put credit spreads. Vertical spreads use fixed five-point wings. Each has two horizons, for 144 grids.

Each grid varies delta (15, 35, 55), profit target (seven steps), and entry time (10:29, 12:29, 14:29 ET). Targets range from 100% to 700% for debit positions and 10% to 70% for credit positions. The count is 144 x 3 x 7 x 3 = 9,072 development cells.

Short-horizon singles request 7 DTE; short verticals request 0 DTE. Longer singles request 42 DTE; longer verticals request 7 DTE. Long-horizon entries add a direction-aligned 50-day SMA filter. Horizon and indicator effects are therefore not isolated from each other.

Account assumptions: $25,000 initial funds, one contract, one open trade, $1,000 allocation cap, $1 commission per contract per side, and $0.10 slippage per side. Later variants double slippage to $0.20 or use a $10,000 account with a $500 cap. Stops and exits are in the generated definitions.

Development is 2018 through 2023, validation is 2024, and confirmation is January 2025 through September 25, 2026. IBIT uses November 19, 2024 through June 2025, July through December 2025, then January through September 25, 2026. These periods had already been inspected in previous work. They are not pristine holdouts.

Up to two positive cells per grid advance with at least 30 trades and drawdown no worse than 25%. Validation retains at most one positive candidate per grid with at least 10 trades and the same drawdown threshold. Frozen candidates then run base, stress and smaller-account variants. A numerical survivor needs positive base and stress results, at least 20 trades and drawdown no worse than 25% in each. Passing does not establish future profitability.

In the completed study, 46 validation runs led to seven frozen candidates and 21 confirmation treatments. No candidate passed the entire final screen. GLD was profitable at both cost levels but had only 12 trades. The method remains useful for research even when a particular sweep finds no full-screen survivor.

## Boundaries that matter

- `useExactDte=False`: inspect actual expirations before describing anything as same-day trading.
- High debit-spread targets can exceed the payoff possible for a given entry debit and wing width. Those cells can still exit by stop or time; a profit does not prove the stated target was reached.
- Default backtest export requests up to 5,000 trades without pagination. Large extensions need a completeness audit or paginated trade retrieval. The mass optimizer itself paginates and checks all unique cells.
- Native indicator semantics, fills, splits, corporate actions and holiday behavior need independent inspection. Code consistency tests are not validation of vendor quotes.
- NQ data adjustment and minute start/end labeling remain unverified. Optional signals use bars through 10:27 ET for entries at 10:29 ET or later.
- Local pacing coordinates scripts on one machine, not other machines or concurrent manual app usage.
- Raw responses remain local and may contain sensitive account metadata. Review them before sharing.
