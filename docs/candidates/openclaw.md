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
- Unified adapter: `adapters/openclaw/` runs `openclaw agent exec --json`,
  maps the stable JSON envelope to normalized events, and writes a redacted
  manifest/events/envelope. `preflight()` verifies the pinned commit, CLI,
  package metadata and license.
- T1 tool smoke: passed through the adapter. The prompt asked the agent to use
  the shell/curl against the Fixture; the final answer was `60`.
- T2 browser: blocked in this environment. `tools.alsoAllow: ["browser"]` was
  enabled, but the run timed out after 240s and the state directory exposed a
  `browser-extension-relay.secret`, indicating the browser tool expects an
  interactive extension relay that is not available in the headless lab.
- Pause/resume: `agent exec` is one-shot; no native checkpoint/resume or live
  approval evidence yet. These remain explicit gaps.

Evidence reports:

- `docs/validation/reports/2026-10-19-openclaw.md`
- `docs/validation/reports/2026-10-19-interrupt-resume-and-cloud-browser.md`

The Hogee repository is the business baseline and is not the OpenClaw source dependency for this lab.
