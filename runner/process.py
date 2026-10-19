"""Small subprocess lifecycle helper for candidate adapter services.

The helper speaks newline-delimited JSON on stdin/stdout.  It is intentionally
transport-agnostic enough for the first local adapter experiments; Compose can
replace it with HTTP/SSE without changing the task or event contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
from typing import Any, Mapping, Sequence


class AdapterProcessError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdapterProcessConfig:
    command: Sequence[str]
    cwd: str | Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 5.0


class AdapterProcess:
    """Own one adapter process and exchange JSON objects over JSONL."""

    def __init__(self, config: AdapterProcessConfig) -> None:
        if not config.command:
            raise ValueError("adapter command cannot be empty")
        self.config = config
        self.process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> int:
        if self.running:
            return int(self.process.pid)  # type: ignore[union-attr]
        environment = os.environ.copy()
        environment.update(self.config.env)
        self.process = subprocess.Popen(
            list(self.config.command),
            cwd=str(self.config.cwd) if self.config.cwd is not None else None,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.running:
                return int(self.process.pid)
            time.sleep(0.01)
        code = self.process.poll()
        self.stop()
        raise AdapterProcessError(f"adapter exited during startup (returncode={code})")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=self.config.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=self.config.shutdown_timeout_seconds)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.process = None

    def restart(self) -> int:
        self.stop()
        return self.start()

    def request(self, payload: Mapping[str, Any], *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        if not self.running or self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise AdapterProcessError("adapter process is not running")
        self.process.stdin.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout_seconds)
        finally:
            selector.close()
        if not ready:
            raise TimeoutError("timed out waiting for adapter response")
        line = self.process.stdout.readline()
        if not line:
            raise AdapterProcessError(f"adapter closed stdout (returncode={self.process.poll()})")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise AdapterProcessError(f"adapter returned invalid JSON: {line!r}") from error
        if not isinstance(response, dict):
            raise AdapterProcessError("adapter response must be a JSON object")
        return response

    def __enter__(self) -> "AdapterProcess":
        self.start()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.stop()
