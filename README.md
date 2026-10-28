# Option Omega MCP research scripts

Companion code for **How to Backtest Options with Claude**, by Tradesdontlie. These are the Python scripts behind our options research: 9,072 development combinations in 144 native optimizer jobs, followed by separate validation, confirmation, cost-stress and account-size runs.

The article explains conversational research with Claude. The automation here uses **Codex CLI's authenticated, experimental app-server MCP transport**, not the Claude SDK. It starts a transient tool context, then calls MCP tools directly without starting a model turn for each request. This is an independent research project, not an official Option Omega SDK.

## Why scripts help

Let the model design and explain the experiment. Let Python submit jobs, poll status, save full responses and rank results. Give the model compact progress and selected summaries when you need interpretation.

- A native optimizer runs a grid server side, instead of requiring a separate agent-driven backtest for every combination.
- Local JSON and CSV retain the evidence without filling the conversation with thousands of responses.
- Durable manifests preserve IDs and submission intent across interruptions.
- Each asset gets a descending-profit ranking, with zero-trade and error cells retained.

Our large study used 1,712 MCP calls over approximately 5 hours 16 minutes. That is an observed call count, **not a token benchmark**. We did not measure a token-saving percentage or dollar saving. Plan quotas and rate limits still apply.

## Requirements and compatibility

- Windows and Python 3.12. The mass runner uses `msvcrt` for its process lock.
- An npm installation of Codex CLI. `research/mcp_client.py` expects `node.exe` beside the launcher and `node_modules/@openai/codex/bin/codex.js` underneath it. Native-binary and some npm layouts need an adapter; do not assume they work.
- An Option Omega account with MCP access and available quota. Readers authenticate their own account.
- The transport is experimental. CLI 0.164.0 was installed when this source export was prepared; this is not a promise of compatibility with every CLI release.

The scripts preserve the original study logic and fixed historical windows, including the September 25, 2026 end date. They do not claim to run on every platform or exhaust every strategy the product supports.

## Setup

From the repository root in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
codex mcp add option-omega --url https://mcp.optionomega.com/mcp
codex mcp login option-omega
.venv\Scripts\python -m unittest discover -s tests -v
```

Complete OAuth in your browser. Do not paste credentials into a prompt or source file. See the [official MCP product page](https://optionomega.com/mcp) and [Codex app-server documentation](https://github.com/openai/codex/tree/main/codex-rs/app-server).

## Start offline: inspect the plan

This command generates no remote jobs and requires no authentication:

```powershell
@'
from research.mass_sweep import definitions, grid_count
specs = definitions()
print({"grids": len(specs), "combinations": sum(grid_count(s["dimensions"]) for s in specs)})
print(specs[0])
'@ | .venv\Scripts\python -
```

Expected totals: **144 grids, 9,072 combinations**. Each grid contains three deltas, seven profit targets and three entry times. Read [the protocol](docs/protocol.md) before running it.

## Check connection without starting a backtest

Run this Python snippet from the repository root using the virtual environment. It makes a usage query and writes tool inventory locally. It does not start an optimizer, backtest or model turn.

```python
from pathlib import Path
from research.mcp_client import OmegaClient

output = Path.home() / "ProjectData" / "option-omega-mcp-research" / "connection-check"
client = OmegaClient(output)
try:
    print(client.call("get_usage_limits"))
    client.inventory()
finally:
    client.close()
```

## Run or resume the full study

**This command consumes real account quota and creates private saved backtests.** It runs the full fixed study, not a dry run. Start only after inspecting the plan and your allowance.

```powershell
$studyOutput = Join-Path $env:USERPROFILE 'ProjectData/option-omega-mcp-research/my-mass-sweep'
.venv\Scripts\python -m research.mass_sweep $studyOutput
```

Use one process per output directory. Reusing that exact directory resumes recorded work. Use a new directory for a different protocol. Do not delete checkpoints to get past an uncertain submission. Read [recovery and limitations](docs/recovery.md).

The mass study does **not** require local NQ data. Optional NQ studies require your own correctly licensed minute-bar dataset. Data is not included.

## Ask the agent for compact output

```text
Read only progress.json and the top 5 rows of each ranked-*-confirmation.csv.
Summarize completed combinations, failed cells, stage, profit, trade count and
drawdown. Keep development rankings separate from later-period results.
Do not start another runner or retry an uncertain submission.
```

Full responses stay local. Pull a specific trade log only when investigating a result.

## Outputs

| File | Purpose |
| --- | --- |
| `protocol.json` | Frozen definitions and planned combination count |
| `mass-tasks.json` | Optimizer submission state and recorded IDs |
| `progress.json` | Compact completion and failure counts |
| `ranked-ASSET-development.csv` | Descending-profit development cells per asset |
| `ranked-ASSET-STRUCTURE-development.csv` | Development cells per asset and structure |
| `frozen-validation.json`, `frozen-confirmation.json` | Selected settings before later stages |
| `ranked-ASSET-confirmation.csv` | Base, cost-stress and smaller-account results |
| `ranked-screen-survivors.csv` | Numerical-screen survivors, not proven strategies |
| `mcp-journal.jsonl` | Full local request/response journal |
| `artifact-manifest.json` | Evidence sizes and SHA-256 hashes |

## Source guide

- `research/mcp_client.py`: authenticated transport and cross-process pacing.
- `research/batch.py`: strategy construction, resumable backtests and local evidence.
- `research/mass_sweep.py`: grids, pagination, ranking, frozen stages and process lock.
- `research/asset_search.py`: per-asset screens and 90-cell grids.
- `research/indicator_search.py`, `research/volatility_search.py`: native indicators and volatility filters.
- `research/signals.py`: optional NQ signals for explicit entry-date tests.
- `research/*audit.py`, `research/*report.py`: consistency checks and reports. Some reports fetch equity curves through MCP; they are not all offline commands.
- `research/workflows.py`: original portfolio and saved-item capability experiments. Its `coverage` mode edits/replaces research-owned saved records and calls cancellation tools. It is not a beginner smoke test.
- `tests/`: offline tests with synthetic fixtures. They do not submit paid jobs.

## Interpretation and scope

This release explains how the research ran, not how to guarantee profits. Positive cells were selected after examining historical results. Later windows had already been inspected during earlier research. Nothing was forward tested, and none of the seven frozen finalists passed every screen in the largest batch.

Requested DTE can differ from actual expiration. Costs, small samples, one large winning trade, account caps and data anomalies can dominate the outcome. AAPL development outliers remain unaudited. Read [the protocol](docs/protocol.md) for the study boundaries.

Only source, tests and documentation are published. No licensed price data, raw account exports, credentials, private job IDs, video assets or private Git history are included. `source-manifest.json` identifies the exact exported Python files. Original research files were uncommitted, so the source Git revision alone does not identify their contents.

Historical, hypothetical research only. Not investment advice or a live trading system.
