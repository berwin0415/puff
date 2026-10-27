"""通过客户端公开传输 API 观测未经修改的模型服务原始字节。

解析、模型调用、重试和 Agent 循环由 SDK 负责。观测器仅记录传输响应及
真实 finish_reason，不重试、恢复、改写实时分块或伪造结束事件。
详细浏览器证据仅在写入保存字节时，对跨分块的可信敏感信息进行脱敏。
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
                raise ValueError("模型服务 SSE 帧超过大小上限")
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
            # 观测器不修复或替换 SDK 的原始流。
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
            raise ValueError("候选尝试访问固定模型代理以外的接口")
        payload = json.loads(request.content)
        if payload.get("model") != self.model or payload.get("stream") is not True:
            raise ValueError("候选更改了选定模型或流式协议")
        record = {"attempt": len(self.records) + 1, "status": None, "sse": False, "bytes": 0, "finish_reason": None, "saw_done": False}
        if self.secrets:
            record["stored_wire_projection"] = "跨分块边界脱敏；SDK 接收的字节保持不变"
        self.records.append(record)
        response = await self.delegate.handle_async_request(request)
        record.update({"status": response.status_code, "sse": "text/event-stream" in response.headers.get("content-type", "")})
        response.stream = ObservedStream(response.stream, record, self.output, self.emit, self.secrets)
        self.emit("provider_http_response", {"attempt": record["attempt"], "status": response.status_code, "sse": record["sse"]})
        return response

    async def aclose(self) -> None:
        await self.delegate.aclose()
