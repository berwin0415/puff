import { useCallback, useEffect, useState } from 'react';

const API_BASE_URL = import.meta.env.PUBLIC_API_BASE_URL ?? '/api';

interface HealthStatus {
  status: string;
  service: string;
  version: string;
  uptime: number;
  timestamp: string;
}

type HealthState =
  | { state: 'loading' }
  | { state: 'ready'; data: HealthStatus }
  | { state: 'error'; message: string };

export default function App() {
  const [health, setHealth] = useState<HealthState>({ state: 'loading' });

  const checkHealth = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/health`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      setHealth({ state: 'ready', data: (await response.json()) as HealthStatus });
    } catch (error) {
      setHealth({
        state: 'error',
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }, []);

  useEffect(() => {
    // State only changes after an await, so this is the usual fetch-on-mount.
    void checkHealth();
  }, [checkHealth]);

  // Triggered by the button, so the loading hint only replaces the result when
  // the user explicitly asks for a fresh check.
  const recheck = () => {
    setHealth({ state: 'loading' });
    void checkHealth();
  };

  return (
    <main className="app">
      <header className="app__header">
        <h1>puff</h1>
        <p className="app__tagline">NestJS API + React client, one pnpm workspace</p>
      </header>

      <section className="card">
        <div className="card__head">
          <h2>API status</h2>
          <button type="button" className="button" onClick={recheck}>
            Re-check
          </button>
        </div>

        {health.state === 'loading' && <p className="hint">Checking {API_BASE_URL}/health…</p>}

        {health.state === 'error' && (
          <p className="hint hint--error">
            Request failed: {health.message}. Is the API running on port 3000?
          </p>
        )}

        {health.state === 'ready' && (
          <dl className="details">
            <div>
              <dt>status</dt>
              <dd className="ok">{health.data.status}</dd>
            </div>
            <div>
              <dt>service</dt>
              <dd>{health.data.service}</dd>
            </div>
            <div>
              <dt>version</dt>
              <dd>{health.data.version}</dd>
            </div>
            <div>
              <dt>uptime</dt>
              <dd>{health.data.uptime}s</dd>
            </div>
          </dl>
        )}
      </section>

      <footer className="app__footer">
        Edit <code>apps/web/src/App.tsx</code> and it hot-reloads. The API lives in{' '}
        <code>apps/api/src</code>.
      </footer>
    </main>
  );
}
