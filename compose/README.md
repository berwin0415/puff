# Local Compose topology

`docker compose -f compose/docker-compose.yml up --build` starts the
deterministic Fixture service, the shared Execution workspace, and long-lived
Runner/Adapter placeholders.  The latter two intentionally expose no harness
API yet; their containers establish the dependency and restart topology for
M1.  Candidate adapters replace the placeholder command as they are added.

The host ports are 18080 (fixtures), 18081 (runner) and 18082 (execution) so
they do not collide with a developer's usual local services.  State and the
execution workspace are mounted under the gitignored `var/` directory.
