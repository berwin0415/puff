# Fixtures

`fixtures.service.server` provides the deterministic HTTP service used by the
local validation tasks.  It exposes health, read-only items, an idempotent
side-effect endpoint, a fail-once endpoint, and a tiny browser page.

Run it locally with:

```sh
.venv/bin/python -m fixtures.service.server --port 8080
```

Set `FIXTURE_STATE_FILE` to persist effects across process/container restarts.
Every side-effect request must carry an `Idempotency-Key`; retries with the
same key and payload are reported as `replayed` rather than creating another
effect.  Reusing a key with a different payload returns HTTP 409.
