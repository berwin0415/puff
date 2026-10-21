"""Observe unmodified provider bytes through the client's public transport API.

The SDK owns parsing, model calls, retries and the Agent loop. This observer only
records the wire response and its real finish_reason; it never retries, resumes,
rewrites live chunks or fabricates a terminal event. Detailed browser evidence
redacts trusted secret forms only while writing saved bytes, across chunks.
"""

import codecs
import json
from pathlib import Path

import httpx2

from detailed_support import StreamingRedactor


class ObservedStream(httpx2.AsyncByteStream):
    def __init__(self, delegate, record: dict, directory: Path, emit, secrets=()):
        self.delegate, self.record, self.emit = delegate, record, emit
        self.path = directory / (f"provider-response-{record['attempt']:03d}" + (".sse" if record["sse"] else ".body"))
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.pending = ""
        self.parse_failed = False
        self.redactor = StreamingRedactor(secrets)

    def observe(self, chunk: bytes) -> None:
        with self.path.open("ab") as output:
            output.write(self.redactor.feed(chunk))
        self.record["bytes"] += len(chunk)
        if not self.record["sse"] or self.parse_failed:
            return
        try:
            self.pending += self.decoder.decode(chunk)
            self.pending = self.pending.replace("\r\n", "\n")
            if len(self.pending) > 8 * 1024 * 1024:
                raise ValueError("Unbounded provider SSE frame")
            while "\n\n" in self.pending:
                frame, self.pending = self.pending.split("\n\n", 1)
                data = "\n".join(line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:"))
                if not data:
                    continue
                if data == "[DONE]":
                    self.record["saw_done"] = True
                    continue
                event = json.loads(data)
                self.emit("provider_wire_event", {"attempt": self.record["attempt"], "data": event})
                if event.get("error"):
                    self.record["provider_error"] = event["error"]
                for choice in event.get("choices", []):
                    if choice.get("index") == 0 and choice.get("finish_reason") is not None:
                        self.record["finish_reason"] = choice["finish_reason"]
        except (UnicodeError, ValueError, TypeError) as error:
            # Observation never repairs or replaces the SDK's original stream.
            self.parse_failed = True
            self.record["observation_error"] = f"{type(error).__name__}: {error}"

    async def __aiter__(self):
        try:
            async for chunk in self.delegate:
                self.observe(chunk)
                yield chunk
            self.record["body_exhausted"] = True
        finally:
            with self.path.open("ab") as output:
                output.write(self.redactor.feed(b"", final=True))
            self.record["partial_frame"] = bool(self.pending.strip())

    async def aclose(self) -> None:
        try:
            await self.delegate.aclose()
        finally:
            with self.path.open("ab") as output:
                output.write(self.redactor.feed(b"", final=True))
            self.record["closed"] = True


class ObservedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, output: Path, model: str, emit, secrets=()):
        self.output, self.model, self.emit = output, model, emit
        self.secrets = tuple(secrets)
        self.delegate = httpx2.AsyncHTTPTransport(retries=0)
        self.records: list[dict] = []

    async def handle_async_request(self, request):
        if request.method != "POST" or request.url.scheme != "http" or request.url.host != "model-proxy" or request.url.port != 8082 or request.url.path != "/v1/chat/completions":
            raise ValueError("Candidate attempted an endpoint outside the fixed model proxy")
        payload = json.loads(request.content)
        if payload.get("model") != self.model or payload.get("stream") is not True:
            raise ValueError("Candidate changed the selected model or streaming protocol")
        record = {"attempt": len(self.records) + 1, "status": None, "sse": False, "bytes": 0, "finish_reason": None, "saw_done": False}
        if self.secrets:
            record["stored_wire_projection"] = "secrets redacted across chunk boundaries; SDK bytes unchanged"
        self.records.append(record)
        response = await self.delegate.handle_async_request(request)
        record.update({"status": response.status_code, "sse": "text/event-stream" in response.headers.get("content-type", "")})
        response.stream = ObservedStream(response.stream, record, self.output, self.emit, self.secrets)
        self.emit("provider_http_response", {"attempt": record["attempt"], "status": response.status_code, "sse": record["sse"]})
        return response

    async def aclose(self) -> None:
        await self.delegate.aclose()
