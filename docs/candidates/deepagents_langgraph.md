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

## Checkpoint inspection

`worker.py --mode inspect` reads the candidate SQLite checkpointer without a
model call or API key. The adapter exposes `inspect_checkpoint()` and
`snapshot()`; `tests/adapters/test_deepagents_snapshot.py` seeds a model-free
LangGraph checkpoint with the external environment and verifies that the
adapter returns the same `checkpoint_id`, checkpoint count and state summary.
This proves checkpoint readability, not task resumption.

## Non-approval pause and cross-process resume

A cloud Harness pause does not require human approval. The primary mechanism is
a model-node safe point: the worker recompiles the graph with
`interrupt_after=["model"]`, emits `pause_requested` / `graph_paused`, and
exits after the checkpoint is written. A new process resumes the same
`thread_id` with `Command(resume=True)` and runs to completion.

Real-model evidence run
`deepagents-pause-d25e50a2-caec-4826-9ef3-6a566e6c93cb`:

- first process: `graph_paused`, `pause_kind=after_model_safe_point`,
  `approval_required=false`, checkpoint recorded;
- resume process: `graph_completed`, answer `fixture-alpha`;
- both processes exited with code 0.

The pause happens after a model turn, not mid-call; `cancel`, live `send_input`
and container-level recovery are still unverified. `scripts/run_deepagents_pause.py`
reproduces the cycle, and the live test is opt-in via `RUN_LIVE_MODEL_TESTS=1`.

Approval interrupts remain available as an optional capability via
`interrupt_on` (`scripts/run_deepagents_interrupt.py`), but they are not the
cloud lifecycle requirement.

The adapter records a SQLite checkpoint and supports offline inspection plus
tool-approval interrupt/resume. Arbitrary pause/cancel/live input and unified
browser bridging remain pending.

## Unified browser, process recovery and T6

- **T2 (adapter layer)**: the worker exposes `browser_fixture`, which invokes the
  shared Execution Playwright smoke. A real-model run returned
  `accepted:harness-lab` and produced a screenshot. This is a unified execution
  bridge, not a native candidate browser.
- **Process recovery**: the graph pauses after the `tools` node, the worker
  exits with a SQLite checkpoint, and a new process resumes the same thread.
  The fixture contained exactly one side effect after resume
  (`duplicate_side_effects=0`).
- **T6 structural measurement**: adding `t6_word_count`, an approval gate and a
  `t6_stage` state middleware required about 36 core lines across the worker and
  adapter; the first run took 4.6s and covered 9 event types.

Remaining: container-level restart, immediate preemption/cancel/live input,
browser session rebuild after pause, normalized token/cost and multi-tenant
isolation.
