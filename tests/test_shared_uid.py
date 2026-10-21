"""Opt-in real Linux subprocess calibration. Not executed by the author.

Run only in a NEW fixed Docker image as root, with --init and
--security-opt no-new-privileges and HARNESS_SHARED_UID_CALIBRATION=1.
Ordinary test collection skips every case. No candidate/model/business requests run.
Unlike the prior FakeChild tests these inspect actual UID, permissions and PIDs.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from runner.shared_backend import AuditJournal
from runner.shared_uid import RootTaskSupervisor, _status, uid_processes
from runner.shared_supervisor import BoundaryError


pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_SHARED_UID_CALIBRATION") != "1",
    reason="Explicit opt-in required for new Docker-only real UID calibration",
)


PROBE = r'''import argparse,json,os,signal,stat,subprocess,sys,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--request',required=True);a=p.parse_args()
r=json.loads(Path(a.request).read_text())
mask=os.umask(0o077);os.umask(mask)
result={'uid':os.getuid(),'euid':os.geteuid(),'gid':os.getgid(),'groups':os.getgroups(),
 'umask':oct(mask),'home':os.environ.get('HOME'),'tmp':os.environ.get('TMPDIR'),
 'ambient_secret_present':'OTHER_USER_SECRET' in os.environ,
 'candidate_output_initially_empty':not any(Path(r['output']).iterdir()),'reads':{},'writes':{}}
own=Path(r['workspace'])/'owned.txt';own.write_text('owned-by-this-task')
(Path(r['workspace'])/'owned-dir').mkdir()
result['file_mode']=oct(stat.S_IMODE(own.stat().st_mode))
result['dir_mode']=oct(stat.S_IMODE((Path(r['workspace'])/'owned-dir').stat().st_mode))
for name,path in r.get('foreign',{}).items():
 try:Path(path).read_bytes();result['reads'][name]='allowed'
 except PermissionError:result['reads'][name]='denied'
 try:
  with open(path,'ab') as f:f.write(b'FORBIDDEN')
  result['writes'][name]='allowed'
 except PermissionError:result['writes'][name]='denied'
if r.get('foreign_pid'):
 try:os.kill(r['foreign_pid'],signal.SIGUSR1);result['foreign_signal']='allowed'
 except PermissionError:result['foreign_signal']='denied'
if r.get('mode')=='orphan':
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
 result['descendant_pid']=child.pid
temporary=Path(r['output'],'probe.json.tmp');temporary.write_text(json.dumps(result))
os.replace(temporary,Path(r['output'],'probe.json'))
if r.get('mode') in {'orphan','exit'}:time.sleep(0.4)
else:time.sleep(60)
'''


def encoded(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def bounded_wait(callback, *, seconds=4):
    deadline = time.monotonic() + seconds
    while True:
        value = callback()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError("Expected actual Linux subprocess observation did not arrive")
        time.sleep(0.02)


@pytest.fixture
def shared_lab():
    assert sys.platform == "linux", "Docker Linux calibration only; do not run this on the host"
    assert Path("/.dockerenv").is_file(), "Docker-only calibration; no host subprocess probes"
    assert os.geteuid() == 0, "This calibration must use the root supervisor in Docker"
    assert _status(os.getpid()).get("NoNewPrivs") == "1", "Pass Docker --security-opt no-new-privileges"
    # Named root-owned /tmp dir is intentional: ordinary pytest tmp parents are
    # 0700, so a valid non-root child cannot traverse them to image-like material.
    with tempfile.TemporaryDirectory(prefix="i24-real-uid-", dir="/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o711)
        private = root / "private"
        private.mkdir(mode=0o700)
        materials = root / "materials"
        materials.mkdir(mode=0o755)
        materials.chmod(0o755)
        script = materials / "uid-probe.py"
        script.write_text(PROBE)
        script.chmod(0o444)
        journal = AuditJournal(private / "backend.jsonl")
        (private / "fixture-secret.json").write_text('"root-only-fixture"')
        (private / "fixture-secret.json").chmod(0o600)
        first_uid = 50000 + secrets.randbelow(500000)
        while uid_processes(first_uid) or uid_processes(first_uid + 1):
            first_uid = 50000 + secrets.randbelow(500000)
        tasks, identities = [], {}
        for offset, task_id in enumerate(("task-a", "task-b")):
            uid = first_uid + offset
            task_root = root / task_id
            task_root.mkdir(mode=0o711)
            task_root.chmod(0o711)
            for name in ("state", "workspace", "home", "tmp"):
                path = task_root / name
                path.mkdir(mode=0o700)
                os.chown(path, uid, uid)
            for name in ("config", "attempts"):
                (task_root / name).mkdir(mode=0o711)
                (task_root / name).chmod(0o711)
            scope = {"experiment_id":"scope-calibration", "variant":task_id,
                     "user_id":"user-a" if offset == 0 else "user-b", "session_id":"main"}
            attempts, entries = [], {}
            for attempt_id in ("worker-a", "worker-b"):
                config = task_root / "config" / attempt_id
                config.mkdir(mode=0o750)
                os.chown(config, 0, uid)
                config.chmod(0o750)
                output = task_root / "attempts" / attempt_id
                output.mkdir(mode=0o700)
                os.chown(output, uid, uid)
                binding = {"schema":"harness-lab-shared-spawn/1", "task_id":task_id,
                    "scope":scope, "attempt_id":attempt_id, "uid":uid, "gid":uid, "entry_kind":"calibration",
                    "paths":{"config":str(config), "request":str(config / "request.json"),
                        "state":str(task_root / "state"), "workspace":str(task_root / "workspace"),
                        "output":str(output), "home":str(task_root / "home"), "tmp":str(task_root / "tmp")},
                    "services":{"tools":f"http://tools-{task_id}:8081", "model":f"http://model-proxy-{task_id}:8082/v1"}}
                request = {"workspace":str(task_root / "workspace"), "output":str(output), "mode":"wait"}
                for name, data in {"shared-task.json":encoded(binding), "request.json":encoded(request),
                                   "run.token":b"calibration-model-token-only", "scope.token":b"calibration-tool-token-only"}.items():
                    path = config / name
                    path.write_bytes(data)
                    os.chown(path, 0, uid)
                    path.chmod(0o440)
                attempts.append({"attempt_id":attempt_id, "candidate":"deepagents_langgraph",
                    "argv":[sys.executable,str(script),"--request",str(config / "request.json")],
                    "environment":{"MODEL_API_BASE_URL":binding["services"]["model"], "MODEL_RUN_TOKEN_FILE":str(config / "run.token")},
                    "config_dir":str(config), "evidence_dir":str(output), "events_file":"raw-events.jsonl",
                    "readiness_file":"control-events.jsonl", "ready_match":{"phase":"pause_handler_installed","signal":"SIGUSR2"},
                    "native_ids":{"session_id":"main-calibration-not-native-state"}})
                entries[attempt_id] = {"kind":"calibration", "script":str(script),
                    "script_sha256":hashlib.sha256(PROBE.encode()).hexdigest(), "binding_sha256":hashlib.sha256(encoded(binding)).hexdigest()}
            tasks.append({"task_id":task_id, "owner":scope["user_id"], "scope":scope,
                "state_dir":str(task_root / "state"), "workspace":str(task_root / "workspace"),
                "artifacts":["owned.txt"], "attempts":attempts})
            identities[task_id] = {"uid":uid,"gid":uid,"root":str(task_root),"entries":entries}
        calls = []

        def real_spawn(argv, **kwargs):
            calls.append((argv, dict(kwargs)))
            return subprocess.Popen(argv, **kwargs)

        def create(*, selected_identities=None, allow_calibration=True):
            return RootTaskSupervisor(tasks, identities if selected_identities is None else selected_identities,
                journal, private_root=private, allow_calibration=allow_calibration, popen_factory=real_spawn)

        supervisor = create()

        def configure(task_id, **values):
            path = Path(identities[task_id]["root"]) / "config/worker-a/request.json"
            value = json.loads(path.read_bytes())
            value.update(values)
            path.chmod(0o640)
            path.write_bytes(encoded(value))
            path.chmod(0o440)

        def report(task_id):
            path = Path(identities[task_id]["root"]) / "attempts/worker-a/probe.json"
            return bounded_wait(lambda: json.loads(path.read_bytes()) if path.exists() else None)

        try:
            yield {"root":root,"private":private,"journal":journal,"tasks":tasks,"identities":identities,
                   "supervisor":supervisor,"create":create,"configure":configure,"report":report,"calls":calls}
        finally:
            try:
                for task_id in identities:
                    supervisor.revoke(task_id, timeout_seconds=4)
            finally:
                journal.close()


def test_actual_child_has_bound_credentials_private_environment_and_umask(shared_lab, monkeypatch):
    lab = shared_lab
    monkeypatch.setenv("OTHER_USER_SECRET", "must-not-inherit-this-ambient-secret")
    lab["supervisor"].start("task-a", "worker-a")
    value = lab["report"]("task-a")
    identity = lab["identities"]["task-a"]
    assert value["uid"] == value["euid"] == value["gid"] == identity["uid"]
    assert value["groups"] == [] and value["umask"] == "0o77"
    assert value["file_mode"] == "0o600" and value["dir_mode"] == "0o700"
    assert value["home"] == identity["root"] + "/home" and value["tmp"] == identity["root"] + "/tmp"
    assert value["candidate_output_initially_empty"] is True
    assert value["ambient_secret_present"] is False
    _, kwargs = lab["calls"][0]
    assert kwargs["user"] == identity["uid"] and kwargs["group"] == identity["gid"]
    assert kwargs["extra_groups"] == () and kwargs["umask"] == 0o077
    assert kwargs["close_fds"] is True and kwargs["shell"] is False
    events = [json.loads(line) for line in lab["journal"].path.read_bytes().splitlines()]
    proof = next(row for row in events if row["kind"] == "shared_child_identity_observed")
    assert proof["source"] == "linux_proc_status_after_explicit_Popen_credentials"
    assert proof["entry_kind"] == "calibration" and proof["native_control_verified"] is False


def test_actual_a_cannot_read_write_b_or_private_journal_or_signal_b(shared_lab):
    lab = shared_lab
    lab["supervisor"].start("task-b", "worker-a")
    lab["report"]("task-b")
    b = lab["supervisor"].owned[("task-b","worker-a")].process
    b_root = Path(lab["identities"]["task-b"]["root"])
    foreign = {"token":str(b_root / "config/worker-a/run.token"),
               "private_journal":str(lab["journal"].path), "private_fixture":str(lab["private"] / "fixture-secret.json")}
    for name in ("state", "workspace", "home", "tmp", "attempts/worker-a"):
        path = b_root / name / "foreign-sentinel.txt"
        path.write_text("belongs-only-to-b")
        os.chown(path, lab["identities"]["task-b"]["uid"], lab["identities"]["task-b"]["gid"])
        path.chmod(0o600)
        foreign[name] = str(path)
    lab["configure"]("task-a", foreign=foreign, foreign_pid=b.pid)
    lab["supervisor"].start("task-a", "worker-a")
    value = lab["report"]("task-a")
    assert set(value["reads"]) == set(foreign) and set(value["writes"]) == set(foreign)
    assert set(value["reads"].values()) == {"denied"} and set(value["writes"].values()) == {"denied"}
    assert value["foreign_signal"] == "denied" and b.poll() is None


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_duplicate_nonroot_identity_is_rejected_before_any_real_spawn(shared_lab, field):
    lab = shared_lab
    bad = deepcopy(lab["identities"])
    bad["task-b"][field] = bad["task-a"][field]
    with pytest.raises(BoundaryError, match="shared_unique_nonroot_identity_required"):
        lab["create"](selected_identities=bad)
    assert lab["calls"] == []


def test_calibration_and_default_candidate_entries_have_no_root_fallback(shared_lab):
    lab = shared_lab
    with pytest.raises(BoundaryError, match="shared_entry_not_enabled"):
        lab["create"](allow_calibration=False)
    bad = deepcopy(lab["identities"])
    for identity in bad.values():
        for entry in identity["entries"].values():
            entry["kind"] = "candidate"
    guarded = lab["create"](selected_identities=bad, allow_calibration=False)
    with pytest.raises(BoundaryError, match="shared_candidate_entry_required"):
        guarded.start("task-a", "worker-a")
    assert lab["calls"] == []


def test_missing_identity_or_world_readable_private_root_refuse_before_spawn(shared_lab):
    lab = shared_lab
    with pytest.raises(BoundaryError, match="shared_identity_missing"):
        lab["create"](selected_identities={})
    lab["private"].chmod(0o755)
    try:
        with pytest.raises(BoundaryError, match="shared_directory_permissions_invalid"):
            lab["create"]()
    finally:
        lab["private"].chmod(0o700)
    assert lab["calls"] == []


def test_parent_exit_with_live_uid_descendant_blocks_next_attempt_and_revocation_reaps_it(shared_lab):
    lab = shared_lab
    lab["configure"]("task-a", mode="orphan")
    lab["supervisor"].start("task-a", "worker-a")
    proof = lab["report"]("task-a")
    parent = lab["supervisor"].owned[("task-a","worker-a")].process
    assert parent.wait(timeout=3) == 0
    uid = lab["identities"]["task-a"]["uid"]
    assert any(row["pid"] == proof["descendant_pid"] and row["state"] != "Z" for row in uid_processes(uid))
    with pytest.raises(BoundaryError, match="shared_task_uid_still_active"):
        lab["supervisor"].start("task-a", "worker-b")
    result = lab["supervisor"].revoke("task-a", timeout_seconds=4)
    assert result["uid_processes_empty_observed"] is True and uid_processes(uid) == []
    assert result["tool_model_quiescence_verified"] is False and result["native_cancel_confirmed"] is False
    with pytest.raises(BoundaryError, match="shared_task_revoked"):
        lab["supervisor"].start("task-a", "worker-b")
    with pytest.raises(BoundaryError, match="shared_task_revoked"):
        lab["supervisor"].cancel("task-a", "control-after-revoke")


def test_revoke_is_uid_local_and_does_not_claim_native_cancel(shared_lab):
    lab = shared_lab
    for task_id in ("task-a","task-b"):
        lab["supervisor"].start(task_id, "worker-a")
        lab["report"](task_id)
    b = lab["supervisor"].owned[("task-b","worker-a")].process
    result = lab["supervisor"].revoke("task-a", timeout_seconds=4)
    assert result["native_cancel_confirmed"] is False and result["tool_model_quiescence_verified"] is False
    assert b.poll() is None
    events = [json.loads(line) for line in lab["journal"].path.read_bytes().splitlines()]
    assert any(row["kind"] == "shared_uid_teardown_started" and row["action"] == "application_SIGKILL_teardown" for row in events)
    assert not any(row.get("native_cancel_confirmed") is True for row in events)


def test_clean_parent_exit_still_does_not_invent_native_resume(shared_lab):
    lab = shared_lab
    lab["configure"]("task-a", mode="exit")
    lab["supervisor"].start("task-a", "worker-a")
    lab["report"]("task-a")
    assert lab["supervisor"].owned[("task-a","worker-a")].process.wait(timeout=3) == 0
    with pytest.raises(BoundaryError, match="shared_native_handoff_not_connected"):
        lab["supervisor"].start("task-a", "worker-b")
