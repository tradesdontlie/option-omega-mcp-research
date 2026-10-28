# Release verification

Verified October 28, 2026 on Windows with Python 3.12 and Codex CLI 0.164.0.

- All 55 offline unit tests passed in the exported source directory.
- Offline plan generation returned 144 grids and 9,072 combinations.
- A live, authenticated `get_usage_limits` call succeeded through the exported `OmegaClient`. No new backtest, optimizer or model turn was started for this check.
- The 27 exported Python source/test files are byte-identical to their originals, with sizes and SHA-256 values in `source-manifest.json`.
- Export excludes original Git history, licensed datasets, raw results, account journals, credentials and production media.

The full 9,072-cell study was not rerun for this release. Passing unit tests and a read-only connection check do not guarantee identical historical results or compatibility with future transport/schema versions.

The README offline command was tested in Windows PowerShell. The original transport's npm layout constraint remains documented. Other operating systems have not been validated.
