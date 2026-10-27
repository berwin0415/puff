"""I25 初始校准：验证镜像中固定的常驻 CLI 和真实原生对象。

先在固定验收镜像执行 prepare-materials，再在固定候选镜像执行探针。
两容器均使用 --network none，不接收远程模型凭证。
控制器不导入候选；两个真实子进程只调用原镜像入口的
prepare_history、state、close。
不证明 Agent 业务、隔离、容量或成本节省。
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
    need(Path("/.dockerenv").is_file(), "仅在 Docker 内运行")
    need(set(os.listdir("/sys/class/net")) == {"lo"}, "必须使用 Docker --network none")
    need(not KEYS.intersection(os.environ), "不得注入远程模型凭证")


def prepare(args):
    # 使用与 runner.instance_cost_full.prepare 首步完全相同的
    # 冻结验收准备；仅准备历史不需要模拟业务服务。
    sys.path.insert(0, "/app")
    from runner.instance_cost import exclusive, make_plan, prepare_idle_materials
    root = Path(args.root)
    need(root.is_dir() and not any(root.iterdir()), "使用全新空校准根目录")
    plan = make_plan(trial_id=args.trial_id, pair_id=args.trial_id, strategy="resident", users=2,
        candidate_image=args.candidate_image, bench_image=args.bench_image, model_name=args.model_name)
    exclusive(root / "protected/plan.json", plan)
    prepare_idle_materials(root)
    (root / "probe").mkdir()
    save(root / "protected/probe-preparation.json", {"kind": "no_model_lifecycle_calibration_materials",
        "owner_to_probe": "user-001", "second_owner_started": False,
        "source": "runner.instance_cost.prepare_idle_materials；与 instance_cost_full.prepare 的首步相同",
        "business_tasks_prepared": 0, "remote_credentials_injected": False})
    print(json.dumps({"status": "materials_prepared", "business_task_completed": False}))


def original_projection(history, summary):
    ref = summary.get("source", {})
    filename = ref.get("path")
    need(isinstance(filename, str) and Path(filename).name == filename and filename.startswith("native-state-"),
        "预期为原始公共原生投影")
    path = Path(history["output_dir"]) / filename
    data = path.read_bytes()
    need(sha(data) == ref.get("sha256") and ref.get("public_api") == "CompiledStateGraph.aget_state",
        "原生投影哈希或 API 不同")
    value = json.loads(data)
    configurable = value.get("config", {}).get("configurable", {})
    need(summary.get("scope") == history["scope"] and summary.get("state_is_nonempty") is True
        and summary.get("checkpoint_id") and configurable.get("checkpoint_id") == summary["checkpoint_id"]
        and configurable.get("thread_id") == history["scope"]["session_id"]
        and configurable.get("checkpoint_ns", "") == ""
        and not value.get("next") and not value.get("tasks") and not value.get("interrupts"),
        "预期为原始非空空闲历史检查点")
    messages = value.get("values", {}).get("messages", [])
    need(any(item.get("id") == "prepared-history-" + history["task_id"]
        and item.get("type") == "human" and item.get("content") == history["sentinel"] for item in messages),
        "缺少原始应用准备历史的哨兵标记")
    return value


def store_inventory(history, checkpoint):
    root = Path(history["state_dir"])
    result = {}
    for path in sorted(root.iterdir()):
        need(path.is_file() and not path.is_symlink(), "原生状态包含非普通文件")
        data = path.read_bytes()
        result[path.name] = {"bytes": len(data), "sha256": sha(data)}
    need("checkpoints.sqlite" in result, "真实 SQLite saver 未持久化数据库")
    # 观察器不打开原数据库；在副本上执行有界 SQL 查询，
    # 交叉确认公开原生 checkpoint 身份。
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
        need(found == [(checkpoint,)], "原始原生存储内未找到公共检查点")
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
        "此探针范围不包含模型或业务执行")
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
    need(not timed_out and process.returncode == 0, "常驻子进程未正常退出；保留诊断")
    responses = [json.loads(line) for line in stdout.splitlines()]
    need(len(responses) == len(commands) + 1 and responses[0].get("phase") == "ready"
        and responses[0].get("owner") == binding["owner"], "缺少真实常驻就绪记录或响应")
    identity = responses[0].get("objects", {})
    need(identity.get("pid") == process.pid and identity.get("scope") == binding["history"]["scope"]
        and {key: item.get("type") for key, item in identity.get("objects", {}).items()} == OBJECTS,
        "镜像内置入口未报告预期的四个真实原生对象类")
    results = {}
    for command, response in zip(commands, responses[1:]):
        need(response.get("command_id") == command["command_id"], "常驻命令被拒绝或不匹配")
        if command["action"] == "close":
            need(response.get("status") == "closed", "缺少原生关闭确认")
        else:
            need(isinstance(response.get("result"), dict), "缺少原始常驻结果")
            results[command["action"]] = response["result"]
    return process.pid, identity, results


def probe(args):
    output = Path(args.output)
    need(output.is_dir() and not any(output.iterdir()), "使用全新空探针输出目录")
    need(ENTRY.is_file() and not ENTRY.is_symlink(), "使用镜像内置常驻入口，不能使用宿主机适配器挂载")
    binding = read("/config/resident.json")
    history = binding["history"]
    need(not any(Path(history["state_dir"]).iterdir()) and not any(Path(history["workspace"]).iterdir())
        and not any(Path(history["output_dir"]).iterdir()), "历史准备需要全新空原始目录")
    first_pid, first_objects, first = child(output, "prepare", binding, ["prepare_history", "state", "close"])
    prepared = first["prepare_history"]
    need(prepared.get("agent_task_completed") is False and prepared.get("model_invocation_requested_by_this_method") is False
        and prepared.get("tool_call_requested_by_this_method") is False, "准备过程必须明确保持非业务性质")
    prepared_projection = original_projection(history, prepared)
    retained_projection = original_projection(history, first["state"])
    need(prepared_projection == retained_projection and first["state"].get("objects") == first_objects,
        "首个进程未保留原始对象和状态")
    first_inventory = store_inventory(history, prepared["checkpoint_id"])
    second_pid, second_objects, second = child(output, "cold", binding, ["state", "close"])
    cold_projection = original_projection(history, second["state"])
    need(second_pid != first_pid and cold_projection == prepared_projection
        and second["state"].get("objects") == second_objects, "新的真实进程未冷读取同一原生检查点")
    final_inventory = store_inventory(history, prepared["checkpoint_id"])
    marker = read(Path(history["workspace"]) / "prepared-history.json")
    need(marker.get("sentinel") == history["sentinel"] and marker.get("agent_task_completed") is False
        and marker.get("scope") == history["scope"], "准备产物被替换或分类错误")
    lifecycle = [json.loads(line) for line in (Path(history["output_dir"]) / "resident-lifecycle.jsonl").read_text().splitlines()]
    need([row.get("phase") for row in lifecycle] == ["native_objects_opened", "history_native_objects_closed"] * 2,
        "两个原生运行时均须具有真实打开和正常关闭事件")
    closes = lifecycle[1::2]
    need([row.get("pid") for row in closes] == [first_pid, second_pid]
        and all(row.get("saver_context_closed") is True and row.get("tool_and_owned_model_http_clients_closed") is True for row in closes),
        "真实 saver/client 关闭事件不完整")
    source, adapter = read("/opt/materials/SOURCE_MANIFEST.json"), read("/opt/materials/LAB_ADAPTER_MANIFEST.json")
    save(output / "verdict.json", {"schema": "harness-lab-resident-native-probe/1", "passed": True,
        "source_export_sha256": source["export_sha256"], "adapter_export_sha256": adapter["export_sha256"],
        "probe_sha256": sha(Path(__file__).read_bytes()), "binding_sha256": sha(Path("/config/resident.json").read_bytes()),
        "tool_schema_sha256": sha(Path(history["tools_schema_file"]).read_bytes()),
        "pids": [first_pid, second_pid], "checkpoint_id": prepared["checkpoint_id"],
        "objects": first_objects["objects"], "store_after_prepare": first_inventory, "store_after_cold_close": final_inventory,
        "network_interfaces": ["lo"], "model_or_business_execution_commands_sent": 0,
        "claims": "真实对象已加载；应用历史已持久化；已冷读取同一检查点；两个进程均已关闭",
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
