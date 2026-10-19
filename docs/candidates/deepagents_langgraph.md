# Deep Agents + LangGraph

- Deep Agents source: <https://github.com/langchain-ai/deepagents>, MIT, pinned source ref `0089e635dce525055272883b53bc222f385beac1`.
- LangGraph source: <https://github.com/langchain-ai/langgraph>, MIT, pinned commit `98043c37abc60374030f960a03a6e0d4c0112643`.
- External source root: `~/harness-lab-sources/` (never vendored into this repository).
- Adapter: `adapters/deepagents_langgraph/` launches a candidate Python worker over JSONL.
- Runtime smoke: passed with the configured remote OpenAI-compatible API and the deterministic `fixture_lookup` tool.

## Source and environment evidence

The Deep Agents commit is available as the official GitHub archive URL because a
full Git fetch of that historical commit was not reachable from the local network:

`https://github.com/langchain-ai/deepagents/archive/0089e635dce525055272883b53bc222f385beac1.tar.gz`

The downloaded archive SHA-256 is
`2b0cb8cd05f7faeb8e8745845958056a9e974658fc85f4682643874c99d8a548`.
The extracted source reports package version `0.7.28` and contains the MIT
license. LangGraph is a clean Git checkout at the pinned commit; its package
version is `1.2.16` and its repository license is MIT.

The external Deep Agents environment uses Python 3.12 and installs Deep Agents
from the archive source plus LangGraph's `langgraph`, `langgraph-prebuilt`,
`langgraph-checkpoint`, and `langgraph-sdk` packages from the pinned source
checkout. The installed versions are Deep Agents `0.7.28`, LangGraph `1.2.16`,
LangChain `1.4.6`, `langchain-openai` `1.7.2`, and
`langgraph-checkpoint-sqlite` `3.1.2`.

## Smoke result

The worker completed a real remote-model task with `fixture_lookup("alpha")`:

- API base: `http://api.dbh.baidu-int.com/v1` (credential value is not recorded)
- model: `deepseek-v4-flash`
- normalized events: 9 (`process_started`, `graph_started`, five `graph_update`, `graph_completed`, `process_exited`)
- final answer: `fixture-alpha`
- run ID: `52921584-baec-47cf-a4eb-acd9cb041781`
- candidate run IDs and the SQLite checkpoint file are retained under the configured external artifact root

The adapter records a SQLite checkpoint, but its current worker is one-shot. Native
pause/cancel/live approval transport is therefore explicitly pending the lifecycle
phase; the current result only establishes import, model, tool, graph event, and
checkpoint backend viability.
