"""UNRUN I25 calibration of the baked resident CLI and real native objects.

Run prepare-materials in the pinned bench, then probe in the pinned candidate.
Both containers use --network none and receive no remote model credentials.
The candidate is never imported into this controller: two real subprocesses
use only prepare_history/state/close on the original baked application entry.
This proves neither an Agent business task nor isolation/capacity/cost savings.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


ENTRY = Path("/opt/materials/harness-lab/resident_runtime.py")
OBJECTS = {"model": "langchain_openai.chat_models.base.ChatOpenAI", "tools": "http_tools.HttpTools",
    "graph": "langgraph.graph.state.CompiledStateGraph", "saver": "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver"}
KEYS = {"MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "AZURE_OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"}


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.write("\n")


def offline_container():
    need(Path("/.dockerenv").is_file(), "Run only inside Docker")
    need(set(os.listdir("/sys/class/net")) == {"lo"}, "Require Docker --network none")
    need(not KEYS.intersection(os.environ), "Do not inject remote model credentials")


def prepare(args):
    # The exact same frozen bench preparation is the first step of
    # runner.instance_cost_full.prepare. No Fixture is needed for history only.
    sys.path.insert(0, "/app")
    from runner.instance_cost import exclusive, make_plan, prepare_idle_materials
    root = Path(args.root)
    need(root.is_dir() and not any(root.iterdir()), "Use a new empty calibration root")
    plan = make_plan(trial_id=args.trial_id, pair_id=args.trial_id, strategy="resident", users=2,
        candidate_image=args.candidate_image, bench_image=args.bench_image, model_name=args.model_name)
    exclusive(root / "protected/plan.json", plan)
    prepare_idle_materials(root)
    (root / "probe").mkdir()
    save(root / "protected/probe-preparation.json", {"kind": "no_model_lifecycle_calibration_materials",
        "owner_to_probe": "user-001", "second_owner_started": False,
        "source": "runner.instance_cost.prepare_idle_materials; same first step as instance_cost_full.prepare",
        "business_tasks_prepared": 0, "remote_credentials_injected": False})
    print(json.dumps({"status": "materials_prepared", "business_task_completed": False}))


def original_projection(history, summary):
    ref = summary.get("source", {})
    filename = ref.get("path")
    need(isinstance(filename, str) and Path(filename).name == filename and filename.startswith("native-state-"),
        "Expected original public native projection")
    path = Path(history["output_dir"]) / filename
    data = path.read_bytes()
    need(sha(data) == ref.get("sha256") and ref.get("public_api") == "CompiledStateGraph.aget_state",
        "Native projection hash/API differs")
    value = json.loads(data)
    configurable = value.get("config", {}).get("configurable", {})
    need(summary.get("scope") == history["scope"] and summary.get("state_is_nonempty") is True
        and summary.get("checkpoint_id") and configurable.get("checkpoint_id") == summary["checkpoint_id"]
        and configurable.get("thread_id") == history["scope"]["session_id"]
        and configurable.get("checkpoint_ns", "") == ""
        and not value.get("next") and not value.get("tasks") and not value.get("interrupts"),
        "Expected the original nonempty idle history checkpoint")
    messages = value.get("values", {}).get("messages", [])
    need(any(item.get("id") == "prepared-history-" + history["task_id"]
        and item.get("type") == "human" and item.get("content") == history["sentinel"] for item in messages),
        "Original application-prepared history sentinel is missing")
    return value


def store_inventory(history, checkpoint):
    root = Path(history["state_dir"])
    result = {}
    for path in sorted(root.iterdir()):
        need(path.is_file() and not path.is_symlink(), "Native state contains a nonregular file")
        data = path.read_bytes()
        result[path.name] = {"bytes": len(data), "sha256": sha(data)}
    need("checkpoints.sqlite" in result, "Real SQLite saver did not persist a database")
    # The original database is never opened by this observer. A bounded SQL
    # lookup on copies corroborates the public native checkpoint identity.
    with tempfile.TemporaryDirectory(prefix="resident-probe-") as temporary:
        copied = Path(temporary) / "checkpoints.sqlite"
        copied.write_bytes((root / copied.name).read_bytes())
        wal = root / "checkpoints.sqlite-wal"
        if wal.is_file():
            copied.with_name(wal.name).write_bytes(wal.read_bytes())
        with sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            found = connection.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                (history["scope"]["session_id"], "", checkpoint)).fetchall()
        need(found == [(checkpoint,)], "Public checkpoint was not found in the original native store")
    lock = os.open(root / ".resident-runtime.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)
    return result


def child(output, stage, binding, actions):
    commands = [{"command_id": stage + "-" + str(index), "action": action,
        **({"task_id": binding["history"]["task_id"]} if action == "state" else {})}
        for index, action in enumerate(actions, 1)]
    need(all(item["action"] in {"prepare_history", "state", "close"} for item in commands),
        "Model/business execution is outside this probe")
    save(output / (stage + "-commands.json"), commands)
    process = subprocess.Popen([sys.executable, str(ENTRY), "--binding", "/config/resident.json"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate("".join(json.dumps(command) + "\n" for command in commands), timeout=120)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate(timeout=15)
    (output / (stage + "-stdout.jsonl")).write_text(stdout)
    (output / (stage + "-stderr.log")).write_text(stderr)
    save(output / (stage + "-process.json"), {"pid": process.pid, "returncode": process.returncode,
        "timeout_kill_used": timed_out, "commands": [item["action"] for item in commands]})
    need(not timed_out and process.returncode == 0, "Resident child did not exit normally; retain diagnostics")
    responses = [json.loads(line) for line in stdout.splitlines()]
    need(len(responses) == len(commands) + 1 and responses[0].get("phase") == "ready"
        and responses[0].get("owner") == binding["owner"], "Missing actual resident readiness/responses")
    identity = responses[0].get("objects", {})
    need(identity.get("pid") == process.pid and identity.get("scope") == binding["history"]["scope"]
        and {key: item.get("type") for key, item in identity.get("objects", {}).items()} == OBJECTS,
        "The baked entry did not report the four expected real native object classes")
    results = {}
    for command, response in zip(commands, responses[1:]):
        need(response.get("command_id") == command["command_id"], "Rejected or mismatched resident command")
        if command["action"] == "close":
            need(response.get("status") == "closed", "Native close acknowledgement is missing")
        else:
            need(isinstance(response.get("result"), dict), "Missing original resident result")
            results[command["action"]] = response["result"]
    return process.pid, identity, results


def probe(args):
    output = Path(args.output)
    need(output.is_dir() and not any(output.iterdir()), "Use a new empty probe output directory")
    need(ENTRY.is_file() and not ENTRY.is_symlink(), "Use the baked resident entry, never a host adapter mount")
    binding = read("/config/resident.json")
    history = binding["history"]
    need(not any(Path(history["state_dir"]).iterdir()) and not any(Path(history["workspace"]).iterdir())
        and not any(Path(history["output_dir"]).iterdir()), "History preparation requires new empty original directories")
    first_pid, first_objects, first = child(output, "prepare", binding, ["prepare_history", "state", "close"])
    prepared = first["prepare_history"]
    need(prepared.get("agent_task_completed") is False and prepared.get("model_invocation_requested_by_this_method") is False
        and prepared.get("tool_call_requested_by_this_method") is False, "Preparation must remain explicitly non-business")
    prepared_projection = original_projection(history, prepared)
    retained_projection = original_projection(history, first["state"])
    need(prepared_projection == retained_projection and first["state"].get("objects") == first_objects,
        "The first process did not retain the original objects and state")
    first_inventory = store_inventory(history, prepared["checkpoint_id"])
    second_pid, second_objects, second = child(output, "cold", binding, ["state", "close"])
    cold_projection = original_projection(history, second["state"])
    need(second_pid != first_pid and cold_projection == prepared_projection
        and second["state"].get("objects") == second_objects, "A fresh real process did not cold-read the same native checkpoint")
    final_inventory = store_inventory(history, prepared["checkpoint_id"])
    marker = read(Path(history["workspace"]) / "prepared-history.json")
    need(marker.get("sentinel") == history["sentinel"] and marker.get("agent_task_completed") is False
        and marker.get("scope") == history["scope"], "Prepared artifact was replaced or misclassified")
    lifecycle = [json.loads(line) for line in (Path(history["output_dir"]) / "resident-lifecycle.jsonl").read_text().splitlines()]
    need([row.get("phase") for row in lifecycle] == ["native_objects_opened", "history_native_objects_closed"] * 2,
        "Expected actual open/normal-close events for both native runtimes")
    closes = lifecycle[1::2]
    need([row.get("pid") for row in closes] == [first_pid, second_pid]
        and all(row.get("saver_context_closed") is True and row.get("tool_and_owned_model_http_clients_closed") is True for row in closes),
        "Real saver/client close events are incomplete")
    source, adapter = read("/opt/materials/SOURCE_MANIFEST.json"), read("/opt/materials/LAB_ADAPTER_MANIFEST.json")
    save(output / "verdict.json", {"schema": "harness-lab-resident-native-probe/1", "passed": True,
        "source_export_sha256": source["export_sha256"], "adapter_export_sha256": adapter["export_sha256"],
        "probe_sha256": sha(Path(__file__).read_bytes()), "binding_sha256": sha(Path("/config/resident.json").read_bytes()),
        "tool_schema_sha256": sha(Path(history["tools_schema_file"]).read_bytes()),
        "pids": [first_pid, second_pid], "checkpoint_id": prepared["checkpoint_id"],
        "objects": first_objects["objects"], "store_after_prepare": first_inventory, "store_after_cold_close": final_inventory,
        "network_interfaces": ["lo"], "model_or_business_execution_commands_sent": 0,
        "claims": "real objects loaded; application history persisted; same checkpoint cold-read; both processes closed",
        "business_task_completed": False, "i24_isolation_verified": False, "i25_complete": False,
        "cost_or_capacity_measured": False})
    print(json.dumps({"passed": True, "business_task_completed": False, "i25_complete": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    materials = sub.add_parser("prepare-materials")
    materials.add_argument("--root", default="/trial")
    materials.add_argument("--trial-id", default="i25-native-probe-001")
    for name in ("candidate-image", "bench-image", "model-name"):
        materials.add_argument("--" + name, required=True)
    native = sub.add_parser("probe")
    native.add_argument("--output", default="/probe")
    args = parser.parse_args()
    offline_container()
    prepare(args) if args.action == "prepare-materials" else probe(args)


if __name__ == "__main__":
    main()
