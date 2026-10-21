"""Host Docker orchestration for one detailed case; no host Agent or grading."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.bench import identifier
from runner.detailed import CANCEL_EXIT_CODES
from runner.metrics import summarize
from scripts.lab import CANDIDATES, Lab

BROWSER_CANDIDATES = frozenset(("deepseek_harness", "openclaw", "deepagents_langgraph", "openai_agents_sdk"))
CASE_OPTIONS_CANDIDATES = BROWSER_CANDIDATES
CANCEL_CANDIDATES = frozenset(CANCEL_EXIT_CODES)


def worker_resource_pins(spec):
    """Extract only non-secret resolved candidate fields before execution."""
    image = spec.get("image") if isinstance(spec, dict) else None
    if not isinstance(image, str) or not image or any(char.isspace() for char in image):
        raise RuntimeError("Candidate image tag is missing or malformed")
    memory, cpus = spec.get("mem_limit"), spec.get("cpus")
    if type(memory) is int:
        memory_limit = memory
    elif isinstance(memory, str):
        match = re.fullmatch(r"([0-9]+)(b|k(?:b)?|m(?:b)?|g(?:b)?)?", memory, flags=re.IGNORECASE)
        if match is None:
            raise RuntimeError("Candidate memory cap is missing or malformed")
        unit = (match[2] or "").lower()[:1]
        memory_limit = int(match[1]) * 1024 ** {"": 0, "b": 0, "k": 1, "m": 2, "g": 3}[unit]
    else:
        raise RuntimeError("Candidate memory cap is missing or malformed")
    if isinstance(cpus, bool) or not isinstance(cpus, (str, int, float)):
        raise RuntimeError("Candidate CPU cap is missing or malformed")
    try:
        cpu_nano = Decimal(str(cpus)) * 10 ** 9
        valid_cpu = cpu_nano.is_finite() and cpu_nano > 0 and cpu_nano == cpu_nano.to_integral_value()
    except DecimalException:
        valid_cpu = False
    if memory_limit <= 0 or not valid_cpu:
        raise RuntimeError("Candidate requires positive finite memory and CPU caps")
    return {"image_tag": image, "memory_limit": memory_limit, "cpu_nano": int(cpu_nano)}


class DetailedLab(Lab):
    def __init__(self):
        super().__init__()
        self.detailed_override = ROOT / "compose/detailed.override.yml"
        if self.detailed_override.is_file():
            self.prefix += ["-f", str(self.detailed_override)]

    def wait_browser(self):
        # Only readiness, not a browser action or task step. The probe itself
        # executes in the already-built bench image, not on the host network.
        code = ("import socket,time\n"
                "for i in range(30):\n"
                " try:\n"
                "  s=socket.create_connection(('browser',8931),timeout=1); s.close(); break\n"
                " except OSError: time.sleep(0.3)\n"
                "else: raise SystemExit('Browser MCP readiness timed out')\n")
        self.compose("run", "--rm", "--entrypoint", "python", "bench", "-c", code)

    def cancel_on_live_turn(self, name, run, stop, candidate="deepseek_harness", run_id=None, scope=None):
        """Trusted test trigger only; the candidate's native API owns cancel."""
        if candidate == "openai_agents_sdk":
            return self.cancel_on_sdk_activity(name, run, stop, run_id, scope)
        if candidate == "deepagents_langgraph":
            from scripts.native_triggers import cancel_on_deepagents_activity
            return cancel_on_deepagents_activity(self, name, run, stop, run_id, scope)
        if candidate == "openclaw":
            from scripts.openclaw_trigger import cancel_on_openclaw_activity
            return cancel_on_openclaw_activity(self.call, name, run, stop, run_id, scope)
        raw = run / "candidate/raw-events.jsonl"
        while not stop.wait(0.1):
            try:
                lines = raw.read_text().splitlines()
            except OSError:
                continue
            active = None
            for number, line in enumerate(lines, 1):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                event = row.get("params", {}).get("event", {})
                if event.get("type") == "turn/start":
                    active = number
                elif event.get("type") == "turn/end":
                    active = None
            if active is not None:
                record = {"requested_at": datetime.now(timezone.utc).isoformat(), "container_name": name,
                          "signal": "SIGUSR1", "trigger": {"path": "candidate/raw-events.jsonl", "line": active}}
                try:
                    sent = self.call(["docker", "kill", "--signal", "SIGUSR1", name], capture=True, check=False, timeout=10)
                    record["signal_exit_code"] = sent.returncode
                except Exception as error:
                    record["error_type"] = type(error).__name__
                (run / "control-request.json").write_text(json.dumps(record) + "\n")
                return

    def cancel_on_sdk_activity(self, name, run, stop, run_id, scope):
        """Observe actual official hooks; never equate is_complete with success."""
        raw = run / "candidate/raw-events.jsonl"
        while not stop.wait(0.1):
            try:
                content = raw.read_text()
                lines = content.splitlines()
                if content and not content.endswith("\n"):
                    lines = lines[:-1]
            except OSError:
                continue
            active = {}
            for number, line in enumerate(lines, 1):
                try:
                    row = json.loads(line)
                    if (row.get("run_id") != run_id or row.get("session_id") != scope["session_id"]
                            or row.get("scope") != scope):
                        continue
                    kind, event = row.get("type"), row.get("event", {})
                    if kind in {"agent_end", "public_stream_observation"} or row.get("native_is_complete") is True:
                        active.clear()
                    elif kind == "llm_end":
                        active.pop(("llm", None), None)
                    elif kind == "tool_end":
                        active.pop(("tool", event.get("call_id") or event.get("tool_name")), None)
                    elif (kind in {"llm_start", "tool_start"} and row.get("native_is_complete") is False
                          and type(row.get("native_current_turn")) is int and event.get("agent_name") == "harness-lab-agent"):
                        key = ("llm", None) if kind == "llm_start" else ("tool", event.get("call_id") or event.get("tool_name"))
                        active[key] = (number, line)
                except (ValueError, TypeError, AttributeError):
                    continue
            if active:
                number, line = max(active.values())
                record = {"requested_at": datetime.now(timezone.utc).isoformat(), "container_name": name,
                          "candidate": "openai_agents_sdk", "run_id": run_id, "session_id": scope["session_id"],
                          "scope": scope, "signal": "SIGUSR1",
                          "trigger": {"path": "candidate/raw-events.jsonl", "line": number},
                          "trigger_line_sha256": hashlib.sha256((line + "\n").encode()).hexdigest(),
                          "observed_through": {"path": "raw-events.jsonl", "line": len(lines),
                              "line_sha256": hashlib.sha256((lines[-1] + "\n").encode()).hexdigest()}}
                try:
                    sent = self.call(["docker", "kill", "--signal", "SIGUSR1", name], capture=True, check=False, timeout=10)
                    record["signal_exit_code"] = sent.returncode
                except Exception as error:
                    record["error_type"] = type(error).__name__
                (run / "control-request.json").write_text(json.dumps(record) + "\n")
                return

    def run_detailed(self, candidate, case, seed, run_id, experiment_id="detailed"):
        if candidate not in CANDIDATES or case not in {"tools", "browser", "sensitive", "extension", "extension-denied", "diagnostic", "diagnostic-timeout", "cancel"}:
            raise ValueError("Unknown candidate or detailed case")
        if case == "cancel" and candidate not in CANCEL_CANDIDATES:
            raise ValueError(f"Native cancel trigger is not connected for {candidate}")
        if case.startswith("extension") and candidate not in CASE_OPTIONS_CANDIDATES:
            raise ValueError(f"Business extension is not connected for {candidate}")
        if case == "browser" and candidate not in BROWSER_CANDIDATES:
            raise ValueError(f"Browser case is not connected for {candidate}; no tools substitute was run")
        if case != "tools" and candidate in CASE_OPTIONS_CANDIDATES and not self.detailed_override.is_file():
            raise RuntimeError("Detailed case mounts are not connected: compose/detailed.override.yml is missing")
        if any(container["state"]["Running"] for container in self.containers()):
            raise RuntimeError("Another harness-lab experiment is still running")
        self.config.initialize_layout()
        paths = self.config.paths_for(experiment_id, run_id, candidate, "alice", "main", run_id, case=case)
        if any(path.exists() for path in (paths.run, paths.fixture, paths.state, paths.workspace, paths.budget)):
            raise RuntimeError("Run ID already exists; use new state and evidence")
        if any(self.config.data_root.joinpath("runs").glob(f"*/{candidate}/*/{run_id}")):
            raise RuntimeError("Run ID already exists on another date or case; evidence cannot be reused")
        # The detailed override requires the run mounts even while resolving
        # config. Set paths first, without creating directories or reading keys.
        self.env.update({"LAB_EXPERIMENT_ID": experiment_id, "LAB_VARIANT": run_id,
                         "LAB_EVIDENCE_HOST": str(paths.run), "LAB_WORKSPACE_HOST": str(paths.workspace),
                         "LAB_STATE_HOST": str(paths.state), "LAB_BUDGET_HOST": str(paths.budget),
                         "LAB_FIXTURE_HOST": str(paths.fixture),
                         "LAB_MODEL_TIMEOUT_AT": "1" if case == "diagnostic-timeout" else "",
                         "LAB_MAX_MODEL_CALLS": str(self.config.budget.max_model_calls)})
        # Compose resolves .env quoting; it may contain a real key. Keep the
        # object in memory only, extract the non-secret model name, then drop it.
        service, prefix, profile, _ = CANDIDATES[candidate]
        name = "harness-lab-detailed-" + prefix + "-" + run_id
        resolved = json.loads(self.compose("--profile", "model", "--profile", service,
                                           "config", "--format", "json", capture=True).stdout)
        model = resolved["services"]["model-proxy"]["environment"].get("MODEL_NAME")
        worker_pins = worker_resource_pins(resolved["services"][service])
        del resolved
        if not isinstance(model, str) or not model:
            raise RuntimeError("The selected MODEL_NAME is missing")
        self.env["LAB_MODEL_NAME"] = model
        # Retain only the immutable image ID, never image Config/Env or the
        # expanded Compose document. A changed tag later fails image matching.
        image_id = self.call(["docker", "image", "inspect", "--format", "{{.Id}}",
                              worker_pins["image_tag"]], capture=True, timeout=10).stdout.strip()
        if re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None:
            raise RuntimeError("Candidate immutable Docker image ID is missing or malformed")
        worker_pins.update({"container_name": name, "image_id": image_id})
        worker_pins_captured_at = datetime.now(timezone.utc).isoformat()
        paths.fixture.mkdir(parents=True, exist_ok=False)
        day = paths.run.parent.parent.parent.name
        common = ["--run-id", run_id, "--candidate", candidate, "--case", case, "--seed", str(seed),
                  "--experiment-id", experiment_id, "--date", day]
        services = ("fixtures", "site", "browser", "model-proxy") if case == "browser" else ("fixtures", "tools", "model-proxy")
        cleanup_services = tuple(reversed(services))
        samples, records, errors = [], [], []
        stop = threading.Event()
        sampler = None
        control = None
        candidate_exit = grade_exit = None
        try:
            self.compose("up", "-d", "--no-build", "fixtures")
            self.wait_health("fixtures", 8080)
            self.compose("run", "--rm", "--entrypoint", "python", "bench", "-m", "runner.detailed", "prepare", *common)
            self.compose("up", "-d", "--no-build", *services[1:])
            self.wait_health("site" if case == "browser" else "tools", 8081)
            self.wait_health("model-proxy", 8082)
            if case == "browser":
                self.wait_browser()
            self.sampling_phase = "services_idle"
            sampler = threading.Thread(target=self.sample, args=(stop, samples, paths.run), daemon=True)
            sampler.start()
            time.sleep(self.config.sampling.interval_seconds)
            self.sampling_phase = "candidate_active"
            if case == "cancel":
                control_scope = {"experiment_id": experiment_id, "variant": run_id, "user_id": "alice", "session_id": "main"}
                control = threading.Thread(target=self.cancel_on_live_turn,
                    args=(name, paths.run, stop, candidate, run_id, control_scope), daemon=True)
                control.start()
            try:
                result = self.compose("run", "--no-deps", "--name", name, service,
                                      check=False, timeout=self.config.budget.active_timeout_seconds + 30)
                candidate_exit = result.returncode
            except subprocess.TimeoutExpired:
                candidate_exit = 124
                errors.append("candidate_timeout")
            # Paid execution stops before snapshots, grading, logs or any other
            # evidence I/O. Tools/site remain alive so the bench can fetch ledger.
            self.halt(names=(name,), services=("model-proxy",))
            # The signal can finish the candidate before the sending thread
            # persists its request. Complete that evidence before grading it.
            if control is not None:
                stop.set()
                control.join(timeout=12)
                if control.is_alive():
                    raise RuntimeError("Cancellation trigger did not finish before grading")
            self.sampling_phase = "grading"
            records = self.containers()
            worker_matches = [row for row in records if isinstance(row, dict)
                              and row.get("name") in {name, "/" + name}]
            # The full ID is trusted end-of-run attribution. It is explicitly
            # not a pre-start independent container-ID pin.
            worker_id = worker_matches[0].get("id") if len(worker_matches) == 1 else None
            observation = {"schema_version": 1, "run_id": run_id, "candidate": candidate,
                           "case": case, "candidate_exit_code": candidate_exit,
                           "observed_at": datetime.now(timezone.utc).isoformat(),
                           "source": "trusted host Docker inspect after paid execution halted",
                           "containers": records}
            (paths.run / "worker-observation.json").write_text(json.dumps(observation, indent=2) + "\n")
            (paths.run / "orchestration-status.json").write_text(json.dumps({
                "run_id": run_id, "candidate": candidate, "case": case,
                "candidate_exit_code": candidate_exit,
                "worker_expectation": {**worker_pins, "container_id": worker_id},
                "worker_snapshot_matches": len(worker_matches),
                "worker_pins_captured_at": worker_pins_captured_at,
                "identity_pin_provenance": {
                    "before_execution": ["container_name", "image_tag", "image_id", "memory_limit", "cpu_nano"],
                    "container_id": "end-of-run trusted Docker snapshot with unique exact name; attribution only, not a pre-start ID pin"}}) + "\n")
            grade_exit = self.compose("run", "--rm", "--entrypoint", "python", "bench", "-m", "runner.detailed", "grade", *common, check=False).returncode
        except Exception as exc:
            errors.append(type(exc).__name__ + ": detailed command failed; inspect saved service logs")
            raise
        finally:
            self.halt(names=(name,), services=("model-proxy",))
            stop.set()
            if sampler is not None:
                sampler.join(timeout=10)
            if control is not None:
                control.join(timeout=10)
            try:
                if paths.run.exists():
                    for item in services:
                        log = self.compose("logs", "--no-color", item, capture=True, check=False, timeout=10)
                        (paths.run / (item + ".log")).write_text(log.stdout + log.stderr)
                    log = self.call(["docker", "logs", name], capture=True, check=False, timeout=10)
                    (paths.run / "candidate.log").write_text(log.stdout + log.stderr)
                    manifest = {"run_id": run_id, "experiment_id": experiment_id, "candidate": candidate,
                                "case": case, "seed": seed, "model_name": model,
                                "candidate_exit_code": candidate_exit, "grade_exit_code": grade_exit,
                                "containers": records or self.containers(), "execution_profile": profile,
                                "real_model_credential_owner": "model-proxy only", "errors": errors,
                                "process_evidence": ["orchestration-status.json", "worker-observation.json", "process-validation.json"],
                                "candidate_output_mount": "candidate subdirectory only; trusted service audits stay outside",
                                "business_http_tools_exposed": case != "browser",
                                "scope": "one detailed case; no pause/resume or tenant-isolation conclusion"}
                    (paths.run / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
                    metrics = summarize(samples)
                    metrics["phases"] = {phase: summarize([sample for sample in samples if sample["phase"] == phase])
                                         for phase in sorted({sample["phase"] for sample in samples})}
                    (paths.run / "resource-metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
            finally:
                self.cleanup(cleanup_services, names=(name,))
        expected_exit = CANCEL_EXIT_CODES[candidate] if case == "cancel" else 0
        return 0 if grade_exit == 0 and candidate_exit == expected_exit else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=tuple(CANDIDATES), required=True)
    parser.add_argument("--case", choices=("tools", "browser", "sensitive", "extension", "extension-denied", "diagnostic", "diagnostic-timeout", "cancel"), required=True)
    parser.add_argument("--seed", type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument("--run-id", type=identifier, required=True)
    parser.add_argument("--experiment-id", type=identifier, default="detailed")
    args = parser.parse_args()
    return DetailedLab().run_detailed(args.candidate, args.case, args.seed, args.run_id, args.experiment_id)


if __name__ == "__main__":
    raise SystemExit(main())
