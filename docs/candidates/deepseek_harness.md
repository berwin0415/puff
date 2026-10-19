# DeepSeek Harness

- Source: <https://github.com/deepseek-ai/deepseek-harness>
- License: MIT
- Local checkout: `../deepseek-harness`
- Pinned local commit: `dd6322d604e00eec1ba5e0c8541159906a21094a`
- Runtime smoke: `pnpm dsh --version` and `pnpm dsh --help` passed on Node `24.22.0` / pnpm `11.7.0`; the adapter's `--help` subprocess smoke also returned code 0.
- Adapter: `adapters/deepseek_harness/` performs source preflight, builds the headless command, captures stdout/stderr and maps candidate output to normalized events.
- Framework model API execution: a real headless smoke returned `API_OK` with exit code 0; the run evidence is recorded outside the repository.
- Framework tool/browser task execution: pending T1/T2 runs.
- Current headless limitation: the one-shot CLI does not expose native pause/resume, checkpoint, approval or live input through this adapter. These are recorded as explicit gaps rather than treated as successful recovery.

The local checkout is a read-only source dependency for this lab. Changes belong in the adapter under this repository.
