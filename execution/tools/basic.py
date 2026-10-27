"""业务 HTTP 工具与文件操作，只能访问一个可信工作区。

所有模拟业务调用使用产品后端提供的凭证。
Agent 输入不能替换客户端凭证、业务地址或工作区根目录。
"""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


MAX_FILE_BYTES = 2 * 1024 * 1024


class ToolError(ValueError):
    """明确且不含秘密的工具错误，可安全写入事件。"""


class FixtureHTTPError(ToolError):
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"模拟业务返回 HTTP 状态码 {status_code}: {payload.get('error', '请求已被拒绝')}")


class FixtureTransportError(ToolError):
    """未收到响应；业务写入可能已经提交。"""


class FixtureClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10, *, strict_outcome: bool = False):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ToolError("模拟业务服务基础地址无效")
        if not isinstance(token, str) or not token or any(character.isspace() for character in token):
            raise ToolError("可信作用域凭证无效")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._strict_outcome = strict_outcome

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self._base_url + path, data=data, method=method, headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        if self._strict_outcome:
            return self._request_with_known_outcome(request, method, path)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, UnicodeDecodeError):
                payload = {"error": "请求已被拒绝"}
            raise FixtureHTTPError(exc.code, payload) from None
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise FixtureTransportError("模拟业务响应不可用；重试写入前须核对操作回执") from exc

    def _request_with_known_outcome(self, request: Request, method: str, path: str) -> dict:
        """仅供 I23 分类使用；不能从无效数据推断已知终态。

        上方默认路径保留早期实验语义；这里的明确拒绝
        与固定的本地模拟业务实现绑定。
        """
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("模拟业务响应必须为对象")
                return payload
        except HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, UnicodeDecodeError, OSError, http.client.HTTPException) as error:
                raise FixtureTransportError("无法解析模拟业务拒绝响应，写入结果未知") from error
            known_rejection = exc.code in {400, 403, 404, 409, 422} or (
                method == "POST" and path == "/writes" and exc.code == 503
                and payload == {"error": "已注入写入前工具故障", "retryable": True})
            if (not known_rejection or not isinstance(payload, dict)
                    or not isinstance(payload.get("error"), str)):
                raise FixtureTransportError("模拟业务响应无法证明请求已明确被拒绝") from None
            raise FixtureHTTPError(exc.code, payload) from None
        except (URLError, OSError, http.client.HTTPException, ValueError, UnicodeDecodeError) as exc:
            raise FixtureTransportError("模拟业务响应不可用；重试写入前须核对操作回执") from exc

    def list_items(self, page: int = 1, page_size: int = 5) -> dict:
        return self._request("GET", "/items?" + urlencode({"page": page, "page_size": page_size}))

    def write(self, operation_id: str, item_id: str, quantity: int) -> dict:
        return self._request("POST", "/writes", {"operation_id": operation_id, "item_id": item_id, "quantity": quantity})

    def sensitive_export(self, operation_id: str) -> dict:
        return self._request("POST", "/writes", {"operation_id": operation_id, "action": "sensitive_export"})

    def ledger(self) -> dict:
        return self._request("GET", "/ledger")

    def receipt(self, operation_id: str) -> dict:
        if not isinstance(operation_id, str) or not operation_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in operation_id):
            raise ToolError("operation_id 无效")
        return self._request("GET", "/receipts/" + operation_id)


def calculate_totals(items: list[dict]) -> dict:
    """以整数分计算指定数量的总额，不执行 Agent 代码。"""
    if not isinstance(items, list) or len(items) > 1000:
        raise ToolError("商品记录不能超过1000条")
    total_cents = 0
    quantity = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"unit_price_cents", "quantity"}:
            raise ToolError("每条记录必须包含 unit_price_cents 和 quantity")
        price, amount = item["unit_price_cents"], item["quantity"]
        if type(price) is not int or type(amount) is not int or not 0 <= price <= 10**9 or not 0 <= amount <= 10**6:
            raise ToolError("价格和数量必须为范围内的非负整数")
        total_cents += price * amount
        quantity += amount
    return {"total_cents": total_cents, "quantity": quantity, "row_count": len(items)}


def _no_symlink_ancestors(path: Path):
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ToolError("工作区不能包含软链接")
        if current.exists() and not current.is_dir():
            raise ToolError("工作区路径已被文件占用")


class WorkspaceFiles:
    def __init__(self, workspace: str | Path):
        self.root = Path(workspace)
        if not self.root.is_absolute() or self.root == Path(self.root.anchor) or ".." in self.root.parts or any(component.lower() == "old" for component in self.root.parts):
            raise ToolError("工作区必须使用归档之外的绝对目录")
        _no_symlink_ancestors(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _no_symlink_ancestors(self.root)

    def _parts(self, relative_path: str) -> list[str]:
        if not isinstance(relative_path, str) or "\\" in relative_path or any(ord(character) < 32 for character in relative_path):
            raise ToolError("工作区相对文件路径无效")
        components = relative_path.split("/")
        if len(components) > 20 or any(part in ("", ".", "..") or part.lower() == "old" or len(part) > 255 for part in components):
            raise ToolError("工作区相对文件路径无效")
        return components

    @contextmanager
    def _parent(self, relative_path: str, create: bool):
        components = self._parts(relative_path)
        descriptor = None
        try:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for component in components[:-1]:
                try:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, components[-1]
        except OSError as exc:
            raise ToolError("工作区文件不可用，或路径经过软链接") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def read_text(self, relative_path: str) -> str:
        with self._parent(relative_path, create=False) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ToolError("只允许读取不超过2 MiB的普通文件")
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = None
                    try:
                        return stream.read(MAX_FILE_BYTES + 1)
                    except UnicodeDecodeError as exc:
                        raise ToolError("工作区文件不是 UTF-8 文本") from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def write_text(self, relative_path: str, content: str) -> dict:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ToolError("文件内容必须是文本，且不超过2 MiB")
        with self._parent(relative_path, create=True) as (parent, name):
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ToolError("目标必须是普通文件，不能为链接或目录")
            temporary = ".write-" + uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {"path": relative_path, "bytes": len(content.encode("utf-8"))}
