# Resume without duplicate jobs

The runner saves submission intent before a mutating request. Once an ID is recorded, subsequent runs poll that ID rather than submit another job. Frozen definitions are compared on resume.

## Recorded ID

If a task is `started` and has an optimization ID, rerun the same command against the same directory after confirming no existing runner owns it. The mass runner's Windows file lock prevents concurrent writers. A leftover `runner.json` PID is not proof that the process is still alive; PIDs can be reused.

## Uncertain submission

A `submitting` or `uncertain` state can mean the server accepted a request but the response was lost. Stop. Inspect the local journal and the corresponding saved strategy/job in Option Omega. Match the parameters and timestamps before reconciling an ID. Never clear that state and resubmit merely because no response was received.

Do not edit checkpoints while a runner is active. Preserve a copy of only the affected small manifest before a deliberate reconciliation, record the reason, and verify the ID belongs to this request. If acceptance cannot be established, leave the run stopped and consult the provider.

## Quota, transport and rate lock

If quota is exhausted, wait for the provider's reset and resume the same directory. Inspect `get_usage_limits` rather than assuming published plan limits apply to your account.

The transport requires the npm layout described in the README. A missing launcher or unsupported experimental RPC should be fixed before starting jobs. Do not implement automatic retries around mutating RPC calls.

The local rate lock is a directory under `LOCALAPPDATA/omega-research` (or the home directory fallback). A process killed while holding that lock can leave it behind. Inspect all local research processes before removing a stale rate lock. Never remove an active runner's lock.

Other original entry points do not all have the mass runner's OS lock. Run only one writer per output directory for every study.

## Reporting

Read compact progress first. Keep failed cells and zero-trade cases in exports. Rank development and confirmation separately. Before sharing a strategy result, inspect actual expiry, costs, drawdown, trade count, best-trade concentration and account-cap skips. No historical ranking establishes a strategy as trustworthy.
