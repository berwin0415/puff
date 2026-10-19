# DeepSeek Harness

- Source: <https://github.com/deepseek-ai/deepseek-harness>
- License: MIT
- Local checkout: `../deepseek-harness`
- Pinned local commit: `dd6322d604e00eec1ba5e0c8541159906a21094a`
- Runtime smoke: `pnpm dsh --version` and `pnpm dsh --help` passed on Node `24.22.0` / pnpm `11.7.0`; the adapter's `--help` subprocess smoke also returned code 0.
- Adapter: `adapters/deepseek_harness/` performs source preflight, builds the headless command, captures stdout/stderr and maps candidate output to normalized events.
- Framework model API execution: a real headless smoke returned `API_OK` with exit code 0; the run evidence is recorded outside the repository.
- Framework T1/T2 task execution: T1 passed; the common Playwright browser
  baseline passed while the shipped headless profile was blocked as expected.
- Reproducible run script: `scripts/run_deepseek_p0.py`. It launches the
  external built CLI with an artifact-local workspace and isolated `DSH_HOME`,
  so candidate tasks cannot dirty the checkout.
- T1 evidence: run `b14909c9-5758-49c5-b8b5-c0766806599e` under
  `~/harness-lab-data/runs/2026-10-19/deepseek_harness/t1-tools/`; the fixture
  observed item reads, a calculated sum of `60`, and one idempotent side effect
  (`201`).
- T2 evidence: run `37cd25bc-fe6a-4740-80de-e7c498a7d97f` under
  `~/harness-lab-data/runs/2026-10-19/deepseek_harness/t2-browser/`; the
  Execution Playwright baseline submitted the form and produced a screenshot,
  while the headless candidate was marked `blocked` because it exposes no
  browser Connection. The candidate process timed out after 30 seconds while
  attempting a non-browser fallback; this is retained in `result.json`.
- Current headless limitations: the one-shot CLI does not expose a browser
  Connection, native pause/resume, checkpoint, approval or live input through
  this adapter. These are recorded as explicit gaps rather than treated as
  successful recovery.

The local checkout is a read-only source dependency for this lab. Changes belong in the adapter under this repository.
