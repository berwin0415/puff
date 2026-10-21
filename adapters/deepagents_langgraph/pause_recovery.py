"""Opt-in I22 binding and private index; SQLite remains the execution state.

The host/bench owns the read-only binding. This helper never reconstructs
messages, advances a graph node, or chooses the remaining business steps.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time


HASH = re.compile(r"[0-9a-f]{64}\Z")
BINDING_FIELDS = frozenset(("schema", "candidate", "mode", "chain_id", "attempt_id", "scope", "session_id",
    "checkpoint_ns", "source_export_sha256", "adapter_export_sha256", "runtime_sha256", "budget", "recovery"))
RECOVERY_FIELDS = frozenset(("attempt_id", "pause_index_sha256", "checkpoint_id", "exact_snapshot_sha256",
    "head_snapshot_sha256", "state_files_sha256", "workspace_files_sha256"))
LOCK = ".i22-chain.lock"
INDEX = "pause-index.json"
TOOLS_SYSTEM_PROMPT = "Use the provided lab_* tools to carry out the user's task. These are the shared experiment tools; their input and output are authoritative."
BUILTIN_TOOL_EXCLUSIONS = frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task", "write_todos"})


def runtime_identity(*, model_name, max_output_tokens, max_retries, candidate_options, tool_definitions):
    """Pure stdlib function: trusted bench can compute before A is started.

    tool_definitions is the trusted tool service's original [{name,
    description,input_schema}] listing, not a candidate-produced hash.
    No file/environment/network access, candidate imports or graph execution.
    """
    if candidate_options != {"case": "tools", "trusted_scope_file": "/config/scope.json"}:
        raise ValueError("Unexpected frozen tools candidate options")
    if (not isinstance(model_name, str) or not model_name or type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= 16384 or type(max_retries) is not int or not 0 <= max_retries <= 2
            or not isinstance(tool_definitions, list) or not tool_definitions):
        raise ValueError("Invalid trusted runtime identity inputs")
    tools, names = [], set()
    for item in tool_definitions:
        if not isinstance(item, dict) or set(item) != {"name", "description", "input_schema"}:
            raise ValueError("Invalid trusted tool identity fields")
        name = item["name"]
        if (not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in names
                or not isinstance(item["description"], str) or not isinstance(item["input_schema"], dict)
                or item["input_schema"].get("type") != "object"):
            raise ValueError("Invalid or duplicate trusted tool identity")
        names.add(name)
        tools.append({"name": "lab_" + name, "description": item["description"], "input_schema": item["input_schema"]})
    return {"candidate": "deepagents_langgraph", "candidate_options": candidate_options,
        "model": {"name": model_name, "base_url": "http://model-proxy:8082/v1", "use_responses_api": False,
            "max_output_tokens": max_output_tokens, "max_retries": max_retries},
        "graph": {"factory": "deepagents.create_deep_agent", "recursion_limit": 64, "checkpoint_ns": "",
            "durability": "sync", "interrupt_on": None, "general_purpose_subagent": False,
            "system_prompt": TOOLS_SYSTEM_PROMPT, "builtin_tool_exclusions": sorted(BUILTIN_TOOL_EXCLUSIONS)},
        "tools": sorted(tools, key=lambda item: item["name"])}


def runtime_identity_sha256(**kwargs):
    """The bench independently derives this from trusted frozen tool definitions."""
    data = json.dumps(runtime_identity(**kwargs), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_lifecycle(request):
    if "lifecycle" not in request:
        return None
    value = request["lifecycle"]
    if not isinstance(value, dict) or set(value) != {"mode", "chain_id", "attempt_id", "binding_file"}:
        raise ValueError("Invalid lifecycle request fields")
    if (value["mode"], value["attempt_id"]) not in {("new", "worker-a"), ("resume", "worker-b")}:
        raise ValueError("Unexpected I22 mode/attempt")
    if value["chain_id"] != request.get("run_id") or value["binding_file"] != "/config/lifecycle.json":
        raise ValueError("Lifecycle chain or trusted binding path mismatch")
    if value["mode"] == "resume" and "prompt" in request:
        raise ValueError("Resume must omit prompt; no new user message is accepted")
    if request.get("candidate_options", {}).get("case") != "tools":
        raise ValueError("I22 lifecycle is restricted to the frozen tools case")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _read_file(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Expected a bounded regular lifecycle file")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) > limit or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("Lifecycle file changed during read")
        return data


def _json(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")))


def _hash(data):
    return hashlib.sha256(data).hexdigest()


class PauseRecovery:
    def __init__(self, request, scope, provenance, serialize_native, started):
        self.request, self.scope, self.provenance = request, scope, provenance
        self.serialize_native, self.started = serialize_native, started
        self.lifecycle = validate_lifecycle(request)
        self.state, self.workspace = Path(request["state_dir"]), Path(request["workspace"])
        self.pending = None
        self.lock_descriptor = None
        self.active_http, self.uncertain_http = {}, []
        if not self.lifecycle:
            raise ValueError("PauseRecovery requires an explicit lifecycle request")
        path = Path(self.lifecycle["binding_file"])
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError("Lifecycle binding requires a separate read-only mount")
        self.binding = _json(_read_file(path, 65536))
        self._validate_binding()
        self.remaining()
        if any(Path(request["output_dir"]).iterdir()):
            raise ValueError("Every lifecycle attempt requires a fresh evidence directory")
        self.lock_descriptor = os.open(self.state / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.lock_descriptor).st_mode):
                raise ValueError("Lifecycle lock must be a regular private file")
            try:
                fcntl.flock(self.lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another executor holds the chain lock") from None
            if self.lifecycle["mode"] == "new":
                if any(path.name != LOCK for path in self.state.iterdir()):
                    raise ValueError("A new lifecycle chain requires empty native state")
                self.saved_index = None
            else:
                self.saved_index = self._verify_closed_files()
        except BaseException:
            self.close()
            raise

    def canonical(self, value):
        return json.dumps(value, default=self.serialize_native, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def digest(self, value):
        return _hash(self.canonical(value))

    def _validate_binding(self):
        value = self.binding
        if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
            raise ValueError("Invalid trusted lifecycle binding fields")
        if value["schema"] != "harness-lab-lifecycle/1" or value["candidate"] != "deepagents_langgraph":
            raise ValueError("Wrong lifecycle binding schema/candidate")
        for key in ("mode", "chain_id", "attempt_id"):
            if value[key] != self.lifecycle[key]:
                raise ValueError("Lifecycle request/binding mismatch: " + key)
        if (not self.scope or value["scope"] != self.scope or self.scope["variant"] != value["chain_id"]
                or value["session_id"] != self.request["session_id"] or value["checkpoint_ns"] != ""):
            raise ValueError("Lifecycle scope/thread/namespace mismatch")
        for key in ("source_export_sha256", "adapter_export_sha256"):
            if value[key] != self.provenance[key]:
                raise ValueError("Lifecycle source/adapter mismatch: " + key)
        if not isinstance(value["runtime_sha256"], str) or not HASH.fullmatch(value["runtime_sha256"]):
            raise ValueError("Invalid trusted runtime identity hash")
        budget = value["budget"]
        if not isinstance(budget, dict) or set(budget) != {"active_limit_seconds", "used_active_seconds", "remaining_seconds"}:
            raise ValueError("Invalid trusted chain budget")
        used, remaining = budget["used_active_seconds"], budget["remaining_seconds"]
        if (type(budget["active_limit_seconds"]) is not int or budget["active_limit_seconds"] != 300
                or type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used < 300
                or type(remaining) is not int or not 1 <= remaining <= 300 - used
                or self.request["timeout_seconds"] != remaining):
            raise ValueError("Request must use the trusted remaining chain budget")
        recovery = value["recovery"]
        if value["mode"] == "new":
            if recovery is not None or used != 0:
                raise ValueError("New lifecycle must start with zero used budget and no recovery")
        else:
            if not isinstance(recovery, dict) or set(recovery) != RECOVERY_FIELDS or recovery["attempt_id"] != "worker-a":
                raise ValueError("Resume requires the protected worker-a recovery binding")
            if not isinstance(recovery["checkpoint_id"], str) or not recovery["checkpoint_id"]:
                raise ValueError("Missing original native checkpoint ID")
            for key in RECOVERY_FIELDS - {"attempt_id", "checkpoint_id"}:
                if not isinstance(recovery[key], str) or not HASH.fullmatch(recovery[key]):
                    raise ValueError("Invalid protected recovery hash: " + key)

    def remaining(self):
        remaining = self.request["timeout_seconds"] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("Remaining cumulative active chain budget exhausted")
        return remaining

    def inventory(self, root, *, state=False, sync=False):
        files = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if state and relative in {LOCK, INDEX}:
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Lifecycle inventory refuses symlinks and nonregular files")
            data = _read_file(path, 128 * 1024 * 1024)
            if sync:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            files[relative] = {"bytes": len(data), "mode": stat.S_IMODE(info.st_mode), "sha256": _hash(data)}
        if state and "checkpoints.sqlite" not in files:
            raise ValueError("Missing original native SQLite database")
        return files

    def _verify_closed_files(self):
        recovery = self.binding["recovery"]
        data = _read_file(self.state / INDEX, 64 * 1024 * 1024)
        if _hash(data) != recovery["pause_index_sha256"]:
            raise ValueError("Private pause index differs from protected recovery binding")
        saved = _json(data)
        if (saved.get("schema") != "harness-lab-pause-index/1" or saved.get("candidate") != "deepagents_langgraph"
                or saved.get("attempt_id") != recovery["attempt_id"] or saved.get("chain_id") != self.binding["chain_id"]
                or saved.get("scope") != self.scope or saved.get("session_id") != self.request["session_id"]
                or saved.get("checkpoint_ns") != "" or saved.get("saver_closed") is not True
                or saved.get("tools_closed") is not True or saved.get("safe_pause") is not True):
            raise ValueError("Protected index does not describe the same closed safe pause")
        for key in ("source_export_sha256", "adapter_export_sha256", "runtime_sha256"):
            if saved.get(key) != self.binding[key]:
                raise ValueError("Original pause identity mismatch: " + key)
        for key in RECOVERY_FIELDS - {"attempt_id", "pause_index_sha256"}:
            if saved.get(key) != recovery[key]:
                raise ValueError("Protected pause reference mismatch: " + key)
        if (type(saved.get("total_active_seconds")) not in (int, float)
                or not math.isfinite(saved["total_active_seconds"])
                or self.binding["budget"]["used_active_seconds"] < saved["total_active_seconds"]):
            raise ValueError("Resume budget omits the previous active execution")
        for field, root, is_state in (("state_files", self.state, True), ("workspace_files", self.workspace, False)):
            actual = self.inventory(root, state=is_state)
            if actual != saved.get(field) or self.digest(actual) != recovery[field + "_sha256"]:
                raise ValueError("Closed native state/workspace changed before resume")
        return saved

    def bind_runtime(self, tools, system_prompt, builtins):
        if (system_prompt != TOOLS_SYSTEM_PROMPT or frozenset(builtins) != BUILTIN_TOOL_EXCLUSIONS
                or os.environ["MODEL_API_BASE_URL"] != "http://model-proxy:8082/v1"):
            raise ValueError("Actual graph configuration differs from the frozen tools runtime")
        definitions = []
        for tool in tools:
            if not tool.name.startswith("lab_") or not isinstance(tool.args_schema, dict):
                raise ValueError("Expected unchanged public HTTP tool schemas")
            definitions.append({"name": tool.name.removeprefix("lab_"), "description": tool.description,
                "input_schema": tool.args_schema})
        runtime = runtime_identity(model_name=os.environ["MODEL_NAME"], max_output_tokens=self.request["max_output_tokens"],
            max_retries=self.request["max_retries"], candidate_options=self.request["candidate_options"],
            tool_definitions=definitions)
        if self.digest(runtime) != self.binding["runtime_sha256"]:
            raise ValueError("Runtime graph/model/tool identity differs from trusted binding")
        return runtime

    def observe_http(self, event):
        call_id = event["call_id"]
        if event["phase"] == "start":
            if call_id in self.active_http:
                self.uncertain_http.append({"call_id": call_id, "reason": "duplicate_start"})
            self.active_http[call_id] = event["tool"]
        elif event["phase"] in {"finish", "error"}:
            if call_id not in self.active_http:
                self.uncertain_http.append({"call_id": call_id, "reason": "terminal_without_start"})
            self.active_http.pop(call_id, None)
            if event["phase"] == "error" or event.get("status", 500) >= 400:
                self.uncertain_http.append({"call_id": call_id, "reason": "error_or_unknown_result"})

    def _snapshot(self, snapshot):
        return _json(self.canonical(snapshot._asdict()))

    def _safe_snapshot(self, snapshot):
        config = snapshot.config.get("configurable", {})
        if (config.get("thread_id") != self.request["session_id"] or config.get("checkpoint_ns", "") != ""
                or not isinstance(config.get("checkpoint_id"), str) or not config["checkpoint_id"]
                or not snapshot.next or not snapshot.tasks or snapshot.interrupts
                or any(task.error or task.interrupts for task in snapshot.tasks)):
            raise ValueError("Native checkpoint is not an unfinished clean drain boundary")
        return config["checkpoint_id"]

    def execution_config(self):
        # Construct from the trusted thread/namespace, never snapshot.config.
        return {"configurable": {"thread_id": self.request["session_id"],
            "checkpoint_ns": self.binding["checkpoint_ns"]}, "recursion_limit": 64}

    def _validate_execution_config(self, config):
        if config != self.execution_config():
            raise ValueError("Execution config must use only the bound thread/namespace and no checkpoint_id")

    async def check_resume(self, graph, config):
        self._validate_execution_config(config)
        expected = self.binding["recovery"]["checkpoint_id"]
        async with asyncio.timeout(self.remaining()):
            exact = await graph.aget_state({**config, "configurable": {**config["configurable"], "checkpoint_id": expected}})
            head = await graph.aget_state(config)
        if self._safe_snapshot(exact) != expected or self._safe_snapshot(head) != expected:
            raise ValueError("Original checkpoint is no longer the current head")
        for name, snapshot in (("exact_snapshot", exact), ("head_snapshot", head)):
            # Specific-ID and live head are intentionally compared with their own
            # A-side projection, since native pending-write projection differs.
            if self.digest(self._snapshot(snapshot)) != self.binding["recovery"][name + "_sha256"]:
                raise ValueError("Native recovery snapshot differs from original pause: " + name)
        return {"checkpoint_id": expected, "public_api": "graph.aget_state", "exact_and_head_verified": True,
            "execution_input": None, "execution_checkpoint_id_present": False}

    async def prepare_pause(self, graph, config, control, observation):
        self._validate_execution_config(config)
        if (not observation.get("pause_boundary_observed") or control.tasks or self.active_http or self.uncertain_http):
            raise ValueError("Pause lacks a clean native boundary with zero active observed operations")
        async with asyncio.timeout(self.remaining()):
            head = await graph.aget_state(config)
            checkpoint_id = self._safe_snapshot(head)
            exact = await graph.aget_state({**config, "configurable": {**config["configurable"], "checkpoint_id": checkpoint_id}})
        if self._safe_snapshot(exact) != checkpoint_id:
            raise ValueError("Exact pause checkpoint differs from current head")
        self.pending = {"checkpoint_id": checkpoint_id, "exact_snapshot": self._snapshot(exact),
            "head_snapshot": self._snapshot(head), "native_pause_observation": observation,
            "observed_active_graph_tasks": 0, "observed_active_http_calls": 0,
            "observed_http_errors_or_unknown_results": 0}
        for name in ("exact_snapshot", "head_snapshot"):
            self.pending[name + "_sha256"] = self.digest(self.pending[name])

    def publish(self, *, saver_closed, tools_closed):
        if not self.pending or saver_closed is not True or tools_closed is not True:
            raise ValueError("Cannot publish pause index before successful native/tool close")
        self.remaining()
        state_files = self.inventory(self.state, state=True, sync=True)
        workspace_files = self.inventory(self.workspace)
        value = {"schema": "harness-lab-pause-index/1", "candidate": "deepagents_langgraph",
            "chain_id": self.binding["chain_id"], "attempt_id": self.binding["attempt_id"],
            "scope": self.scope, "session_id": self.request["session_id"], "checkpoint_ns": "",
            **{key: self.binding[key] for key in ("source_export_sha256", "adapter_export_sha256", "runtime_sha256")},
            **self.pending, "saver_closed": True, "tools_closed": True, "safe_pause": True,
            "state_files": state_files, "state_files_sha256": self.digest(state_files),
            "workspace_files": workspace_files, "workspace_files_sha256": self.digest(workspace_files),
            "total_active_seconds": self.binding["budget"]["used_active_seconds"] + time.monotonic() - self.started}
        self.remaining()
        data = self.canonical(value) + b"\n"
        temporary = self.state / (".pause-index.json.tmp." + str(os.getpid()))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        published = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state / INDEX)
            published = True
            descriptor = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.remaining()
        except BaseException:
            if published:
                (self.state / INDEX).unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": INDEX, "sha256": _hash(data), "checkpoint_id": value["checkpoint_id"],
            "saver_closed": True, "tools_closed": True, "total_active_seconds": value["total_active_seconds"]}

    def close(self):
        if self.lock_descriptor is not None:
            os.close(self.lock_descriptor)
            self.lock_descriptor = None
