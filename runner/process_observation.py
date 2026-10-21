"""Independent grading of a trusted host's retained Docker Worker observation.

This checks process evidence only, never native task completion or tool results.
Callers supply a trusted WorkerExpectation; the expected identity must not be
copied from an arbitrary candidate-controlled result. The caller must record
which fields were pinned before execution and which were observed afterward;
an end-of-run Docker ID is attribution, not an independent pre-start ID pin.
Existing detailed manifests and orchestration-status.json are supported without
rewriting them.
The CLI runs in the bench container and writes a new, supplemental audit only.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_DOCKER_ID = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
_NAME = re.compile(r"/?[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_MAX_JSON_BYTES = 8 * 1024 * 1024
_CANDIDATES = {
    "deepseek_harness": ("deepseek-harness", "dsh"), "openclaw": ("openclaw", "openclaw"),
    "deepagents_langgraph": ("deepagents-langgraph", "deepagents"),
    "pi_agent_core": ("pi-agent-core", "pi"), "google_adk": ("google-adk", "adk"),
    "openai_agents_sdk": ("openai-agents-sdk", "agents-sdk"),
    "openhands": ("openhands", "openhands"), "codex_sdk": ("codex-sdk", "codex"),
}


def expected_exit_code(candidate: str, case: str) -> int:
    """Current detailed policy: ordinary 0, cancel 1 except native graph drain 0."""
    return 1 if case == "cancel" and candidate != "deepagents_langgraph" else 0


def _matches(pattern: re.Pattern, value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _name(value: Any) -> str | None:
    return value.removeprefix("/") if _matches(_NAME, value) else None


@dataclass(frozen=True)
class WorkerExpectation:
    run_id: str
    candidate: str
    case: str
    container_name: str
    container_id: str
    image_id: str
    image_tag: str
    expected_exit_code: int = 0
    memory_limit: int | None = None
    cpu_nano: int | None = None

    def __post_init__(self):
        if not all(_matches(_ID, value) for value in (self.run_id, self.candidate, self.case)):
            raise ValueError("Expected run, candidate and case must be explicit valid identifiers")
        if not _matches(_NAME, self.container_name) or not _matches(_DOCKER_ID, self.container_id):
            raise ValueError("Expected Worker requires an exact name and full Docker ID")
        if not _matches(_IMAGE_ID, self.image_id):
            raise ValueError("Expected image must be an immutable sha256 Docker image ID")
        if (not isinstance(self.image_tag, str) or not self.image_tag
                or len(self.image_tag) > 512 or any(char.isspace() for char in self.image_tag)):
            raise ValueError("Expected image tag is required")
        if type(self.expected_exit_code) is not int or not 0 <= self.expected_exit_code <= 255:
            raise ValueError("Expected exit must be an integer Docker exit code")
        if self.expected_exit_code != expected_exit_code(self.candidate, self.case):
            raise ValueError("Expected exit does not match the current task policy; signals are not healthy exits")
        for value in (self.memory_limit, self.cpu_nano):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Expected resource limits must be positive integer limits")


def grade_process_observation(manifest: Any, host_observation: Any,
                              expected: WorkerExpectation) -> dict:
    """Fail closed on missing/ambiguous observations; do not grade service exits.

    The manifest and host observation must come from trusted host evidence,
    outside the candidate's writable output mount. This is not a signature or
    live Docker inspection. Services may be running or have cleanup exit 137;
    only the unique pinned candidate Worker is evaluated for process health.
    """
    if not isinstance(expected, WorkerExpectation):
        raise TypeError("A trusted WorkerExpectation is required")
    manifest = manifest if isinstance(manifest, dict) else {}
    host = host_observation if isinstance(host_observation, dict) else {}
    identity = {key: getattr(expected, key) for key in ("run_id", "candidate", "case")}
    inventory = manifest.get("containers")
    inventory_ok = isinstance(inventory, list) and bool(inventory)
    if inventory_ok:
        inventory_ok = all(isinstance(row, dict) and _matches(_DOCKER_ID, row.get("id"))
                           and _name(row.get("name")) is not None for row in inventory)
    if inventory_ok:
        inventory_ok = (len({row["id"] for row in inventory}) == len(inventory)
                        and len({_name(row["name"]) for row in inventory}) == len(inventory))
    rows = inventory if isinstance(inventory, list) else []
    workers = [row for row in rows if isinstance(row, dict) and (
        row.get("id") == expected.container_id
        or _name(row.get("name")) == _name(expected.container_name)
        or row.get("image_id") == expected.image_id
        or row.get("image_tag") == expected.image_tag)]
    unique = inventory_ok and len(workers) == 1
    worker = workers[0] if unique else {}
    state = worker.get("state") if isinstance(worker.get("state"), dict) else {}
    exit_codes = (state.get("ExitCode"), manifest.get("candidate_exit_code"), host.get("candidate_exit_code"))
    checks = [
        ("manifest_identity", all(manifest.get(key) == value for key, value in identity.items()),
         "Trusted manifest identifies the expected run, candidate and case"),
        ("host_identity", all(host.get(key) == value for key, value in identity.items()),
         "Trusted host process observation identifies this same execution"),
        ("valid_unique_inventory", bool(inventory_ok), "Retained Docker inventory has full, distinct IDs and names"),
        ("exactly_one_candidate_worker", bool(unique), "Name, ID, image ID or tag identifies exactly one Worker"),
        ("worker_identity", bool(unique) and worker.get("id") == expected.container_id
         and _name(worker.get("name")) == _name(expected.container_name)
         and worker.get("image_id") == expected.image_id and worker.get("image_tag") == expected.image_tag,
         "The unique observed Worker matches all trusted expected identity fields; pin timing is caller-recorded"),
        ("worker_exited", bool(unique) and state.get("Status") == "exited"
         and type(state.get("Pid")) is int and state["Pid"] == 0,
         "Docker reports an exited Worker with no running PID"),
        ("exit_codes_consistent", bool(unique) and all(type(code) is int
         and code == expected.expected_exit_code for code in exit_codes),
         "Docker, manifest and host exit codes exactly match the expected exit, including its integer type"),
        ("worker_no_oom", bool(unique) and state.get("OOMKilled") is False,
         "Docker explicitly reports OOMKilled=false; a successful task cannot override OOM evidence"),
        ("worker_inactive", bool(unique) and all(state.get(key) is False
         for key in ("Dead", "Running", "Paused", "Restarting")),
         "All inactive state flags are explicitly false"),
        ("worker_no_docker_error", bool(unique) and state.get("Error") == "",
         "Docker reports an explicit empty Error string"),
        ("worker_resource_limits", bool(unique) and all(type(worker.get(key)) is int
         and worker[key] > 0 and (pin is None or worker[key] == pin)
         for key, pin in (("memory_limit", expected.memory_limit), ("cpu_nano", expected.cpu_nano))),
         "Observed memory and CPU limits are positive integers and match any supplied pins"),
    ]
    return {"schema_version": 1, "audit_type": "candidate_docker_process_observation", **identity,
            "passed": all(passed for _, passed, _ in checks),
            "checks": [{"name": name, "passed": passed, "detail": detail} for name, passed, detail in checks],
            "expectation": asdict(expected),
            "observed_worker": {key: worker.get(key) for key in (
                "id", "name", "image_id", "image_tag", "state", "memory_limit", "cpu_nano")} if unique else None,
            "claim": "Retained trusted-host Worker process evidence only; no task, recovery, resource-efficiency or production-health conclusion"}


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def read_evidence(path: Path) -> tuple[dict, dict]:
    """Read an ordinary non-symlink JSON object and retain its exact byte hash."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Evidence paths must be absolute without traversal")
    if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
        raise ValueError("Evidence must be a regular file without symlink ancestors")
    if path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("Evidence JSON exceeds size limit")
    data = path.read_bytes()
    value = json.loads(data, object_pairs_hook=_object_pairs, parse_float=_finite_float,
                       parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    if not isinstance(value, dict):
        raise ValueError("Evidence JSON must be an object")
    return value, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def audit_files(manifest_path: Path, host_path: Path, expectation_path: Path,
                original_verdict_path: Path | None = None) -> dict:
    """Offline supplemental audit. Never mutate or reinterpret the old verdict."""
    manifest, manifest_source = read_evidence(manifest_path)
    host, host_source = read_evidence(host_path)
    expected, expected_source = read_evidence(expectation_path)
    result = grade_process_observation(manifest, host, WorkerExpectation(**expected))
    result["sources"] = {"manifest": manifest_source, "host_observation": host_source,
                         "expectation": expected_source}
    if original_verdict_path is not None:
        _, source = read_evidence(original_verdict_path)
        result["sources"]["original_verdict_unmodified"] = source
    return result


def _status(checks: list[dict]) -> str:
    if any(check["passed"] is False for check in checks):
        return "failed"
    return "unknown" if any(check["passed"] is None for check in checks) else "passed"


def snapshot_health(worker: dict, host_exit: Any, expected_exit: int | None) -> dict:
    """A missing snapshot field is unknown; explicit OOM/fault evidence is failed.

    The host exit may come from the same trusted manifest, which is explicitly
    weaker than independent identity pins. OOM cannot be hidden by unknown IDs.
    """
    state = worker.get("state") if isinstance(worker.get("state"), dict) else {}
    checks = []

    def check(name, keys, predicate):
        known = all(key in state for key in keys)
        checks.append({"name": name, "passed": bool(predicate()) if known else None})

    check("docker_exited", ["Status"], lambda: state["Status"] == "exited")
    check("docker_pid_zero", ["Pid"], lambda: type(state["Pid"]) is int and state["Pid"] == 0)
    check("docker_no_oom", ["OOMKilled"], lambda: state["OOMKilled"] is False)
    for key in ("Dead", "Running", "Paused", "Restarting"):
        check("docker_" + key.lower() + "_false", [key], lambda key=key: state[key] is False)
    check("docker_no_error", ["Error"], lambda: state["Error"] == "")
    known_exit = "ExitCode" in state and host_exit is not None and expected_exit is not None
    checks.append({"name": "docker_host_expected_exit", "passed": (
        type(state["ExitCode"]) is int and type(host_exit) is int
        and state["ExitCode"] == host_exit == expected_exit) if known_exit else None})
    return {"status": _status(checks), "checks": checks,
            "exit_source": "candidate_exit_code in the same trusted host manifest"}


def audit_manifest(manifest_path: Path, *, expectation_path: Path | None = None,
                   host_path: Path | None = None) -> dict:
    """Supplement old evidence without upgrading unpinned identities to healthy.

    Complete host snapshots can pass snapshot_health on their own. Independent
    validation remains unknown without separately supplied Worker identity pins.
    Original task verdicts, including a passed verdict with OOM, stay untouched.
    """
    manifest, source = read_evidence(manifest_path)
    candidate, run_id, case = (manifest.get(key) for key in ("candidate", "run_id", "case"))
    known_identity = (candidate in _CANDIDATES if isinstance(candidate, str) else False)
    known_identity = known_identity and _matches(_ID, run_id)
    service, prefix = _CANDIDATES[candidate] if known_identity else (None, None)
    names = {f"harness-lab-{prefix}-{run_id}", f"harness-lab-detailed-{prefix}-{run_id}"} if known_identity else set()
    tag = f"harness-lab-{service}:local" if known_identity else None
    inventory = manifest.get("containers")
    rows = inventory if isinstance(inventory, list) else []
    workers = [row for row in rows if isinstance(row, dict) and known_identity
               and (_name(row.get("name")) in names or row.get("image_tag") == tag)]
    expected_exit = expected_exit_code(candidate, case) if _matches(_ID, case) else None
    health = (snapshot_health(workers[0], manifest.get("candidate_exit_code"), expected_exit)
              if len(workers) == 1 else {"status": "failed" if len(workers) > 1 else "unknown", "checks": [
                  {"name": "unique_candidate_snapshot", "passed": False if len(workers) > 1 else None}]})
    result = {"schema_version": 1, "audit_type": "supplemental_candidate_process_audit",
              "run_id": run_id, "candidate": candidate, "case": case,
              "snapshot_health": health, "independent_identity_check": {"status": "unknown", "checks": []},
              "sources": {"manifest": source},
              "claim": "Offline audit of retained trusted-host snapshots; original task verdict is not replaced"}
    original_path = manifest_path.parent / "verdict.json"
    if original_path.exists() or original_path.is_symlink():
        _, original_source = read_evidence(original_path)
        result["sources"]["original_verdict_unmodified"] = original_source
    else:
        result["sources"]["original_verdict_unmodified"] = None
    if expectation_path is not None:
        expectation, pin_source = read_evidence(expectation_path)
        expected = WorkerExpectation(**expectation)
        if host_path is None:
            host = {key: manifest.get(key) for key in ("run_id", "candidate", "case", "candidate_exit_code")}
        else:
            host, host_source = read_evidence(host_path)
            result["sources"]["host_observation"] = host_source
        strict = grade_process_observation(manifest, host, expected)
        result["sources"]["expectation"] = pin_source
        result["independent_identity_check"] = {"status": "passed" if strict["passed"] else "failed",
                                                  "checks": strict["checks"], "expectation": asdict(expected),
                                                  "host_exit_source": "separate host observation" if host_path else "same host manifest"}
    # Explicit snapshot faults win even when independent identity is unknown.
    statuses = (health["status"], result["independent_identity_check"]["status"])
    result["status"] = "failed" if "failed" in statuses else "passed" if all(x == "passed" for x in statuses) else "unknown"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--run-dir", type=Path, action="append", default=[])
    parser.add_argument("--host-observation", type=Path)
    parser.add_argument("--expectation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from runner.bench import environment
    info = environment()  # Actual acceptance runs only inside Docker.
    if info["model_credentials_injected"]:
        raise RuntimeError("Offline process audit must not receive model credentials")
    manifests = args.manifest + [path / "manifest.json" for path in args.run_dir]
    if not manifests or len(set(manifests)) != len(manifests):
        raise ValueError("Supply distinct explicit manifest paths or run directories; no data-root scan")
    if len(manifests) != 1 and (args.expectation is not None or args.host_observation is not None):
        raise ValueError("Independent identity pins apply to exactly one run per invocation")
    if args.host_observation is not None and args.expectation is None:
        raise ValueError("Separate host observations require explicit Worker identity pins")
    if args.output.name != "process-audit.json" or not args.output.is_absolute():
        raise ValueError("Use a new supplemental process-audit.json output")
    if ".." in args.output.parts or any(parent.is_symlink() for parent in args.output.parents):
        raise ValueError("Audit output must not traverse symlink directories")
    runs = [audit_manifest(path, expectation_path=args.expectation, host_path=args.host_observation) for path in manifests]
    result = {"schema_version": 1, "audit_type": "supplemental_candidate_process_audit_batch",
              "runs": runs, "environment": info,
              "passed": all(run["status"] == "passed" for run in runs)}
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    for run in runs:
        print(json.dumps({"run_id": run["run_id"], "status": run["status"],
                          "snapshot_health": run["snapshot_health"]["status"],
                          "independent_identity_check": run["independent_identity_check"]["status"],
                          "failed_checks": sorted({check["name"] for part in ("snapshot_health", "independent_identity_check")
                                                   for check in run[part]["checks"] if check["passed"] is False})}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
