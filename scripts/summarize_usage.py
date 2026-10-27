"""仅读取明确且已结束的证据清单；不执行候选。

这是记账工具，不是评分器。仅导入 Python 标准库，不读取凭证、候选对话记录或模型响应。
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3


DATE = "2026-10-20"
FORMAL_BATCHES = (
    ("deepseek-original", "deepseek_harness/runtime/i17-dsh-formal-batch-001/batch.json"),
    ("deepseek-metadata", "deepseek_harness/runtime/i17-dsh-metadata-formal-batch-001/batch.json"),
    ("openclaw", "openclaw/runtime/i18-openclaw-formal-batch-001/batch.json"),
    ("deepagents", "deepagents_langgraph/runtime/i19-deepagents-extension-formal-batch-001/batch.json"),
    ("deepagents", "deepagents_langgraph/runtime/i19-deepagents-core-formal-batch-001/batch.json"),
    ("agents-sdk", "openai_agents_sdk/runtime/i20-agents-sdk-formal-batch-001/batch.json"),
)

# 不发现新目录：活动或新链不能静默计入总量。
# 每条链只有一份预算。离线复核不产生另一轮计费运行。
I22_CHAINS = (
    ("deepagents_langgraph", "i22-deepagents-baseline-seed-101-001", "baseline", None),
    ("deepagents_langgraph", "i22-deepagents-baseline-seed-202-001", "baseline", None),
    ("deepagents_langgraph", "i22-deepagents-baseline-seed-303-001", "baseline", None),
    ("deepseek_harness", "i22-deepseek-baseline-seed-101-001", "baseline", "durable-goal-001"),
    ("deepseek_harness", "i22-deepseek-baseline-seed-202-001", "baseline", "durable-goal-001"),
    ("deepseek_harness", "i22-deepseek-baseline-seed-303-001", "baseline", "durable-goal-001"),
    ("deepagents_langgraph", "i22-deepagents-pause-seed-101-001", "implementation-aborted", None),
    ("deepagents_langgraph", "i22-deepagents-pause-seed-101-002", "implementation-aborted", None),
    ("deepagents_langgraph", "i22-deepagents-pause-seed-101-003", "implementation-aborted", None),
    ("deepagents_langgraph", "i22-deepagents-pause-seed-101-004", "pause-formal", None),
    ("deepagents_langgraph", "i22-deepagents-pause-seed-202-001", "pause-formal", None),
    ("deepseek_harness", "i22-deepseek-pause-seed-101-001", "implementation-aborted", None),
    ("deepseek_harness", "i22-deepseek-pause-seed-101-002", "pause-formal", "restored-header-002"),
)
CALL_COLUMNS = ("id", "started_at", "path", "outcome", "upstream_status")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular(path, data_root):
    path = Path(path)
    relative = path.resolve(strict=True).relative_to(data_root)
    require("old" not in relative.parts and path.is_file(), '禁止使用归档证据或非文件证据')
    return path


def read_json(path, data_root):
    path = regular(path, data_root)
    data = path.read_bytes()
    return json.loads(data), {"path": str(path), "sha256": digest(data)}


def summarize_calls(rows):
    require(isinstance(rows, list), '调用元数据必须为列表')
    clean = []
    for row in rows:
        require(isinstance(row, dict) and set(row) == set(CALL_COLUMNS), '调用元数据列不符合预期')
        clean.append({column: row[column] for column in CALL_COLUMNS})
    require([row["id"] for row in clean] == list(range(1, len(clean) + 1)), '调用 ID 不连续')
    outcomes = Counter(row["outcome"] for row in clean)
    statuses = Counter(str(row["upstream_status"]) for row in clean)
    completed = sum(row["outcome"] == "forwarded" and type(row["upstream_status"]) is int
                    and 200 <= row["upstream_status"] < 300 for row in clean)
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()
    return {"reservations": len(clean), "completed_forwarded_2xx": completed,
            "outcomes": dict(sorted(outcomes.items())), "upstream_statuses": dict(sorted(statuses.items())),
            "selected_call_metadata_sha256": digest(encoded)}


def verdict_summary(document, reference):
    require(type(document.get("passed")) is bool, '判定缺少布尔结果')
    return {**reference, "passed": document["passed"],
            "failed_checks": [row["name"] for row in document["checks"] if row["passed"] is False]}


def formal_runs(data_root):
    runs, batches, seen = [], [], set()
    for variant, relative in FORMAL_BATCHES:
        batch, batch_ref = read_json(data_root / "runs" / DATE / relative, data_root)
        batches.append({"variant": variant, **batch_ref})
        for row in batch["runs"]:
            run_id = row["run_id"]
            require(run_id not in seen, '正式运行重复')
            seen.add(run_id)
            run_root = Path(row["manifest"]).parent
            require(run_root.name == run_id and run_root.parent.name == row["case"], '运行身份不匹配')
            verdict, verdict_ref = read_json(run_root / "verdict.json", data_root)
            calls, calls_ref = read_json(run_root / "model-calls.json", data_root)
            usage = summarize_calls(calls)
            require(row["passed"] == verdict["passed"] and row["metrics"]["model_requests"]
                    == verdict["metrics"]["model_requests"] == usage["reservations"], '现有记录不一致')
            runs.append({"variant": variant, "run_id": run_id, "case": row["case"], "seed": row["seed"],
                         "passed": row["passed"], "verdict": verdict_summary(verdict, verdict_ref),
                         "calls_source": calls_ref, "usage": usage})
    return batches, runs


def i22_runs(data_root):
    runs = []
    for candidate, chain_id, category, review in I22_CHAINS:
        run_root = data_root / "runs" / DATE / candidate / "tools" / chain_id
        database = regular(data_root / "budgets/pause-recovery" / chain_id / chain_id / "calls.sqlite", data_root)
        # 明确不对 budget.token_hash 执行 SELECT，也不读取 run.token。
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows = [dict(zip(CALL_COLUMNS, row)) for row in connection.execute(
                "SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id").fetchall()]
        usage = summarize_calls(rows)
        original = effective = None
        verdicts = []
        if category != "implementation-aborted":
            document, reference = read_json(run_root / "verdict.json", data_root)
            original_hash = reference["sha256"]
            original = document["passed"]
            require(document["chain_id"] == chain_id and document["candidate"] == candidate
                    and document["metrics"]["reserved_model_calls"] == usage["reservations"], 'I22 原始结果不匹配')
            verdicts.append(verdict_summary(document, reference))
            if review:
                document, reference = read_json(run_root / "reviews" / review / "verdict.json", data_root)
                review_calls = document["metrics"].get("reserved_model_calls", document["metrics"].get("model_calls"))
                require(document["chain_id"] == chain_id and document["candidate"] == candidate
                        and review_calls == usage["reservations"], 'I22 复核结果不匹配')
                require(document.get("original_verdict_sha256", original_hash) == original_hash,
                        '复核引用了不同的原始判定')
                verdicts.append(verdict_summary(document, reference))
            effective = document["passed"]
        runs.append({"candidate": candidate, "chain_id": chain_id, "category": category,
                     "original_passed": original, "effective_passed": effective, "verdicts": verdicts,
                     "calls_source": {"path": str(database), "mode": '只读 SQLite 快照',
                                      "selected_columns": list(CALL_COLUMNS)}, "usage": usage})
    return runs


def aggregate(rows, keys, outcome):
    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(row[key] for key in keys)].append(row)
    summaries = []
    for group, members in sorted(grouped.items()):
        successes = sum(row[outcome] is True for row in members)
        total = sum(row["usage"]["reservations"] for row in members)
        failed = sum(row["usage"]["reservations"] for row in members if row[outcome] is not True)
        summaries.append({**dict(zip(keys, group)), "attempts": len(members), "successful_attempts": successes,
                          "reservations": total, "unsuccessful_attempt_reservations": failed,
                          "completed_forwarded_2xx": sum(row["usage"]["completed_forwarded_2xx"] for row in members)})
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("/Users/hanbowen01/harness-lab-data"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    batches, formal = formal_runs(root)
    paused = i22_runs(root)
    summary = {
        "schema": "harness-lab-read-only-usage-inventory/1", "date": DATE,
        "scope": '明确选定的六个正式批次和十三条已结束 I22 链；调研台账不完整，不排名',
        "excluded": ['未列入 I22_CHAINS 的活动或新链', "old/", "screening/smoke/cancel/cold-read/calibration",
                     "I23-I25", '原始响应或推理文本', 'token、密钥和预算绑定行'],
        "unknown": ['统一的输入、输出、缓存与推理 token 总量', '已核验的模型单价和账单金额',
                    '人工开发与调试工时', '候选升级成本', '完整基础设施费用'],
        "formal_batches": batches, "formal_runs": formal,
        "formal_groups": aggregate(formal, ("variant", "case"), "passed"),
        "i22_runs": paused, "i22_groups": aggregate(paused, ("candidate", "category"), "effective_passed"),
        "subset_totals": {"formal_attempts": len(formal), "formal_reservations": sum(row["usage"]["reservations"] for row in formal),
                          "i22_chains": len(paused), "i22_reservations": sum(row["usage"]["reservations"] for row in paused)},
    }
    encoded = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = args.output.resolve()
        require(not destination.exists(), '拒绝覆盖现有快照')
        require(root != destination and root not in destination.parents, '不能写入证据根目录')
        with destination.open("x") as target:
            target.write(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
