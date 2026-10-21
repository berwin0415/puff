"""Short-lived infrastructure checks, executed inside the bench image."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
from zoneinfo import ZoneInfo


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", value):
        raise ValueError("Invalid infrastructure run identifier")
    return value


def environment() -> dict:
    if sys.platform != "linux" or not Path("/.dockerenv").exists():
        raise RuntimeError("Infrastructure acceptance must run in Docker")
    for forbidden in (Path("/app/old"), Path("/app/.env"), Path("/data/old")):
        if forbidden.exists():
            raise RuntimeError(f"Unexpected archive or credential path: {forbidden}")
    sources = {}
    for folder in ("runner", "tests", "configs", "fixtures", "execution", "scripts", "compose"):
        for file in sorted(Path("/app", folder).rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                sources[str(file.relative_to("/app"))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return {
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "container_hostname": socket.gethostname(),
        "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "archive_present": False,
        "credential_file_present": False,
        "model_credentials_injected": any(os.environ.get(k) for k in
            ("MODEL_API_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")),
        "source_sha256": sources,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "write-probe", "read-probe"))
    parser.add_argument("--run-id", required=True, type=identifier)
    parser.add_argument("--probe", type=identifier, default="persistence")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    output = Path("/data/runs") / now.date().isoformat() / "infrastructure" / "bootstrap" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    info = environment()
    probe = Path("/data/state/infrastructure") / f"{args.probe}.json"
    passed = True
    if args.action == "check":
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd="/app", text=True, capture_output=True, check=False,
        )
        (output / "test-output.txt").write_text(result.stdout + result.stderr)
        passed = result.returncode == 0
        info["pytest_exit_code"] = result.returncode
        info["pytest_summary"] = result.stdout.strip().splitlines()[-1:]
    elif args.action == "write-probe":
        probe.parent.mkdir(parents=True, exist_ok=True)
        with probe.open("x") as stream:
            json.dump({"probe": args.probe, "writer": info["container_hostname"]}, stream)
        info["persistence"] = {"action": "write", "probe": args.probe}
    else:
        previous = json.loads(probe.read_text())
        passed = previous["probe"] == args.probe and previous["writer"] != info["container_hostname"]
        info["persistence"] = {"action": "read", "probe": args.probe,
                               "writer_container": previous["writer"], "new_container": passed}
    info.update({"run_id": args.run_id, "recorded_at": now.isoformat(), "passed": passed})
    (output / "environment.json").write_text(json.dumps(info, indent=2) + "\n")
    print(json.dumps({k: v for k, v in info.items() if k != "source_sha256"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
