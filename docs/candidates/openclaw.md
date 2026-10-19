# OpenClaw

- Source: <https://github.com/openclaw/openclaw>
- License: MIT (`package.json` and `LICENSE`)
- Local checkout: `../openclaw` (repository-external, detached and clean)
- Pinned source commit: `b6e0872bdb7228e22fac7608345e0529a35ec46e`
- Version/runtime: OpenClaw `2026.10.5`, Node `24.22.0`, pnpm `12.11.1`;
  `pnpm install --frozen-lockfile` and `pnpm build` passed.
- Gateway smoke: passed on an isolated loopback state directory. `healthz` and
  `readyz` returned HTTP 200, Gateway health/status RPC succeeded, and 20
  plugins loaded.
- Remote model smoke: passed through the embedded local agent with a custom
  OpenAI-compatible provider. The exact prompt returned `OPENCLAW_API_OK`;
  the run used model `deepseek-v4-flash` and did not persist credentials.
- Agent tool surface: the runtime exposes file, shell/process, message and
  deferred browser tools. Unified T1/T2 adapter integration and pause/resume
  recovery are not yet implemented; these remain the next comparison step.

The Hogee repository is the business baseline and is not the OpenClaw source dependency for this lab.
