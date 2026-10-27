"""初始草稿：固定 LangGraph 的真实 drain、SQLite 及 A→B 校准，不调用模型。

仅在固定 Deep Agents 候选镜像内运行。以只读方式挂载本脚本，
并挂载一个全新空证据目录为可写。使用镜像原有 LangGraph 与来源
校验器，不替换实现。这是运行时校准，不是业务任务通过证据。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import operator
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Annotated, TypedDict


THREAD = "i22-real-runtime-calibration"
REASON = "calibration_pause"
CONFIG = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}, "recursion_limit": 16}


class State(TypedDict):
    done: Annotated[list[str], operator.add]


def now():
    return datetime.now(timezone.utc).isoformat()


def native(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "_asdict"):
        return value._asdict()
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    if isinstance(value, (datetime, Path)):
        return str(value)
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__, "message": str(value)}
    raise TypeError(type(value).__name__)


def encoded(value):
    return json.dumps(value, default=native, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def create(path, value):
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def append(path, value):
    with path.open("ab") as stream:
        stream.write(encoded({"at": now(), "pid": os.getpid(), **value}) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def read(path):
    return json.loads(path.read_bytes())


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def inventory(root, *, sync=False):
    value = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        require(stat.S_ISREG(mode), "原生清单包含链接或非普通文件")
        data = path.read_bytes()
        if sync:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        value[path.relative_to(root).as_posix()] = {"bytes": len(data),
            "mode": stat.S_IMODE(mode), "sha256": hashlib.sha256(data).hexdigest()}
    require("checkpoints.sqlite" in value, "缺少原生 SQLite 检查点数据库")
    return value


def snapshot_value(snapshot):
    return json.loads(encoded(snapshot._asdict()))


def validate_boundary(snapshot, expected=None):
    config = snapshot.config.get("configurable", {})
    checkpoint = config.get("checkpoint_id")
    require(config.get("thread_id") == THREAD and config.get("checkpoint_ns", "") == "",
        "原生检查点线程或命名空间不正确")
    require(isinstance(checkpoint, str) and checkpoint, "缺少真实检查点 ID")
    require(expected is None or checkpoint == expected, "原始检查点已不再是头检查点")
    require(snapshot.values == {"done": ["first", "active"]}, "暂停业务状态不正确")
    require(tuple(snapshot.next) == ("finish",) and len(snapshot.tasks) == 1,
        "暂停后未留下真实最终节点待执行")
    require(not snapshot.interrupts and all(not task.error and not task.interrupts for task in snapshot.tasks),
        "暂停状态包含错误或中断")
    return checkpoint


def installed_provenance():
    # 本目录与校验器均打包在不可变候选镜像中。
    # 校准挂载不包含候选包或源码替换材料。
    sys.path.insert(0, "/opt/materials/harness-lab")
    from verify_source import verify_source
    import langgraph.runtime
    import langgraph.pregel.main
    import langgraph.checkpoint.sqlite.aio
    return {"source": verify_source(), "distributions": {name: importlib.metadata.version(name)
        for name in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "deepagents")},
        "module_paths": {"runtime": langgraph.runtime.__file__, "pregel": langgraph.pregel.main.__file__,
            "sqlite_saver": langgraph.checkpoint.sqlite.aio.__file__}}


def make_graph(saver, root, attempt, entered=None, release=None):
    from langgraph.graph import END, START, StateGraph
    audit = root / "node-audit.jsonl"

    async def first(state: State):
        append(audit, {"attempt": attempt, "node": "first", "phase": "start", "input": state})
        require(attempt == "worker-a", "B 重放了已完成的首节点")
        append(audit, {"attempt": attempt, "node": "first", "phase": "finish"})
        return {"done": ["first"]}

    async def active(state: State):
        append(audit, {"attempt": attempt, "node": "active", "phase": "start", "input": state})
        require(attempt == "worker-a", "B 重放了已完成的活跃节点")
        require(state["done"] == ["first"], "首节点未在活跃节点前提交")
        entered.set()
        # 允许真实在途节点在 drain 请求后完成。
        # 不模拟其调用方、图执行器或 checkpoint 保存器。
        await release.wait()
        append(audit, {"attempt": attempt, "node": "active", "phase": "finish"})
        return {"done": ["active"]}

    async def finish(state: State):
        append(audit, {"attempt": attempt, "node": "finish", "phase": "start", "input": state})
        require(attempt == "worker-b", "A 在请求原生 drain 后执行了下一节点")
        require(state["done"] == ["first", "active"], "B 未收到原始原生状态")
        append(audit, {"attempt": attempt, "node": "finish", "phase": "finish"})
        return {"done": ["finish"]}

    builder = StateGraph(State)
    builder.add_node("first", first)
    builder.add_node("active", active)
    builder.add_node("finish", finish)
    builder.add_edge(START, "first")
    builder.add_edge("first", "active")
    builder.add_edge("active", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=saver)


async def stream(graph, initial, control, root, attempt, observed):
    require(set(CONFIG["configurable"]) == {"thread_id", "checkpoint_ns"},
        "执行配置意外包含 checkpoint_id")
    async for mode, event in graph.astream(initial, config=CONFIG,
            stream_mode=["updates", "tasks", "checkpoints"], durability="sync", control=control):
        append(root / (attempt + "-raw.jsonl"), {"mode": mode, "data": event})
        if mode == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
            if "input" in event and "result" not in event:
                observed[event["id"]] = event.get("name")
            elif any(field in event for field in ("result", "error", "interrupts")):
                observed.pop(event["id"], None)


async def worker_a(root):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.errors import GraphDrained
    from langgraph.runtime import RunControl
    provenance = installed_provenance()
    entered, release = asyncio.Event(), asyncio.Event()
    control, observed = RunControl(), {}
    require(not control.drain_requested, "RunControl 不是新实例")
    create(root / "worker-a-start.json", {"pid": os.getpid(), "provenance": provenance,
        "execution_config": CONFIG, "durability": "sync", "initial": {"done": []}, "model_used": False})
    paused = None
    async with AsyncSqliteSaver.from_conn_string(str(root / "state/checkpoints.sqlite")) as saver:
        graph = make_graph(saver, root, "worker-a", entered, release)

        async def requester():
            await entered.wait()
            require(list(observed.values()) == ["active"], "请求未发生在真实活跃任务期间")
            append(root / "control.jsonl", {"attempt": "worker-a", "phase": "before_public_request",
                "observed_active_tasks": observed, "first_completed": True})
            control.request_drain(REASON)
            append(root / "control.jsonl", {"attempt": "worker-a", "phase": "public_request_returned",
                "drain_requested": control.drain_requested, "reason": control.drain_reason})
            release.set()

        async with asyncio.timeout(15):
            request_task = asyncio.create_task(requester())
            try:
                await stream(graph, {"done": []}, control, root, "worker-a", observed)
            except GraphDrained as error:
                require(type(error) is GraphDrained and error.reason == REASON and control.drain_reason == REASON,
                    "当前请求缺少真实原生 GraphDrained")
                require(not observed, "原生流仍有当前任务在途执行")
                head = await graph.aget_state(CONFIG)
                checkpoint = validate_boundary(head)
                exact_config = {**CONFIG, "configurable": {**CONFIG["configurable"], "checkpoint_id": checkpoint}}
                exact = await graph.aget_state(exact_config)
                validate_boundary(exact, checkpoint)
                paused = {"pid": os.getpid(), "actual_exception_type": type(error).__module__ + "." + type(error).__qualname__,
                    "reason": error.reason, "checkpoint_id": checkpoint,
                    "head": snapshot_value(head), "exact": snapshot_value(exact),
                    "head_sha256": digest(snapshot_value(head)), "exact_sha256": digest(snapshot_value(exact)),
                    "observed_active_tasks": observed, "model_used": False}
            else:
                raise AssertionError("A 未出现真实原生 GraphDrained 就完成了")
            await request_task
    # 先完整退出原生保存器上下文，再盘点已关闭文件。
    require(paused is not None, "A 没有真实原生暂停")
    paused.update({"saver_context_exited": True, "closed_state_files": inventory(root / "state", sync=True)})
    create(root / "worker-a-paused.json", paused)
    print(json.dumps({"attempt": "worker-a", "paused": True, "checkpoint_id": paused["checkpoint_id"]}))


async def worker_b(root):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.runtime import RunControl
    provenance = installed_provenance()
    paused = read(root / "worker-a-paused.json")
    require(paused["saver_context_exited"] is True, "未观测到 A saver 关闭")
    require(inventory(root / "state") == paused["closed_state_files"], "B 打开 saver 前，已关闭 SQLite/WAL 发生变化")
    require(provenance == read(root / "worker-a-start.json")["provenance"], "已安装材料发生变化")
    control, observed = RunControl(), {}
    require(not control.drain_requested, "B 复用了 A 已排空的 RunControl")
    create(root / "worker-b-start.json", {"pid": os.getpid(), "provenance": provenance,
        "execution_config": CONFIG, "durability": "sync", "initial": None, "new_prompt": False,
        "execution_checkpoint_id_present": False, "model_used": False})
    async with AsyncSqliteSaver.from_conn_string(str(root / "state/checkpoints.sqlite")) as saver:
        graph = make_graph(saver, root, "worker-b")
        async with asyncio.timeout(15):
            checkpoint = paused["checkpoint_id"]
            exact = await graph.aget_state({**CONFIG, "configurable": {**CONFIG["configurable"], "checkpoint_id": checkpoint}})
            head = await graph.aget_state(CONFIG)
            validate_boundary(exact, checkpoint)
            validate_boundary(head, checkpoint)
            require(digest(snapshot_value(exact)) == paused["exact_sha256"], "精确快照跨进程后发生变化")
            require(digest(snapshot_value(head)) == paused["head_sha256"], "头检查点快照跨进程后发生变化")
            create(root / "worker-b-binding.json", {"checkpoint_id": checkpoint,
                "exact_and_current_head_verified": True, "execution_input": None,
                "execution_config": CONFIG, "execution_checkpoint_id_present": False})
            await stream(graph, None, control, root, "worker-b", observed)
            require(not observed and not control.drain_requested, "B 结束时仍有活跃任务或旧 drain 请求")
            final = await graph.aget_state(CONFIG)
            require(final.values == {"done": ["first", "active", "finish"]}, "B 未继续原始进度")
            require(not final.next and not final.tasks and not final.interrupts, "B 留下未完成原生任务")
            create(root / "worker-b-final-native.json", snapshot_value(final))
    create(root / "worker-b-complete.json", {"pid": os.getpid(), "completed": True,
        "saver_context_exited": True, "closed_state_files": inventory(root / "state", sync=True), "model_used": False})
    print(json.dumps({"attempt": "worker-b", "completed": True, "old_nodes_replayed": False}))


def coordinate(root, image_id):
    require(image_id.startswith("sha256:") and len(image_id) == 71, "请传入真实不可变 Docker 镜像 ID")
    require(not root.is_symlink() and root.is_absolute(), "输出根路径必须是明确的真实目录")
    root.mkdir(exist_ok=True)
    require(not list(root.iterdir()), "校准需要全新空输出根目录")
    (root / "state").mkdir(mode=0o700)
    create(root / "calibration.json", {"schema": "harness-lab-native-runtime-calibration/1",
        "declared_image_id": image_id, "started_at": now(), "coordinator_pid": os.getpid(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model_used": False, "scope": "仅覆盖真实 LangGraph Runtime/SQLite，不声称 Deep Agents 业务任务能力"})
    results = []
    for attempt in ("worker-a", "worker-b"):
        tick = time.monotonic()
        with (root / (attempt + ".log")).open("xb") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), attempt,
                "--root", str(root)], stdout=log, stderr=subprocess.STDOUT)
            try:
                exit_code = child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise AssertionError("校准子进程超过固定截止时间") from None
        results.append({"attempt": attempt, "pid": child.pid, "exit_code": exit_code,
            "elapsed_seconds": time.monotonic() - tick})
        create(root / (attempt + "-process.json"), results[-1])
        require(exit_code == 0, attempt + ' 失败；请检查其保留日志')
    require(results[0]["pid"] != results[1]["pid"], "A 和 B 必须是不同 Python 进程")
    require(read(root / "worker-a-paused.json")["pid"] == results[0]["pid"]
        and read(root / "worker-b-complete.json")["pid"] == results[1]["pid"], "Worker 证据 PID 不匹配")
    audit = rows(root / "node-audit.jsonl")
    actual = [(row["attempt"], row["node"], row["phase"]) for row in audit]
    expected = [(attempt, node, phase) for attempt, node in
        (("worker-a", "first"), ("worker-a", "active"), ("worker-b", "finish")) for phase in ("start", "finish")]
    require(actual == expected, "A 已完成节点被重复执行，或 B 未仅执行待处理最终节点")
    create(root / "verdict.json", {"schema": "harness-lab-native-runtime-calibration-verdict/1",
        "passed": True, "finished_at": now(), "actual_graph_drained": True,
        "saver_closed_before_B": True, "distinct_processes": results,
        "same_exact_checkpoint_and_current_head": True, "resume_input": None,
        "resume_execution_checkpoint_id_present": False, "completed_nodes_replayed": False,
        "model_used": False, "deepagents_business_task_validated": False})
    print(json.dumps({"passed": True, "model_used": False, "evidence": str(root)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("coordinator", "worker-a", "worker-b"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image-id")
    args = parser.parse_args()
    if args.mode == "coordinator":
        coordinate(args.root, args.image_id or "")
    else:
        require(Path("/opt/venv/bin/python").samefile(sys.executable), "使用镜像内固定的候选 Python 运行时")
        asyncio.run((worker_a if args.mode == "worker-a" else worker_b)(args.root))


if __name__ == "__main__":
    main()
