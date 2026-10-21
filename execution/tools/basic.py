"""Business HTTP tools and file operations restricted to one trusted workspace.

All fixture calls use the token supplied by the simulated product backend.
Agent input cannot replace the client token, fixture URL or workspace root.
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
    """Explicit, non-secret tool failure suitable for recording in events."""


class FixtureHTTPError(ToolError):
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Fixture returned HTTP {status_code}: {payload.get('error', 'request rejected')}")


class FixtureTransportError(ToolError):
    """The response was not received; a write may already have committed."""


class FixtureClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10, *, strict_outcome: bool = False):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ToolError("Invalid fixture service base URL")
        if not isinstance(token, str) or not token or any(character.isspace() for character in token):
            raise ToolError("Invalid trusted scope token")
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
                payload = {"error": "request rejected"}
            raise FixtureHTTPError(exc.code, payload) from None
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise FixtureTransportError("Fixture response unavailable; check operation receipt before retrying a write") from exc

    def _request_with_known_outcome(self, request: Request, method: str, path: str) -> dict:
        """I23-only classification; never infer a known terminal from bad data.

        The unchanged default path above preserves prior experiments. These
        known rejections are tied to the locked local fixture implementation.
        """
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("Fixture response must be an object")
                return payload
        except HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, UnicodeDecodeError, OSError, http.client.HTTPException) as error:
                raise FixtureTransportError("Fixture rejection could not be decoded; write outcome is unknown") from error
            known_rejection = exc.code in {400, 403, 404, 409, 422} or (
                method == "POST" and path == "/writes" and exc.code == 503
                and payload == {"error": "Injected tool failure before write", "retryable": True})
            if (not known_rejection or not isinstance(payload, dict)
                    or not isinstance(payload.get("error"), str)):
                raise FixtureTransportError("Fixture response does not establish a known rejection") from None
            raise FixtureHTTPError(exc.code, payload) from None
        except (URLError, OSError, http.client.HTTPException, ValueError, UnicodeDecodeError) as exc:
            raise FixtureTransportError("Fixture response unavailable; check operation receipt before retrying a write") from exc

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
            raise ToolError("Invalid operation_id")
        return self._request("GET", "/receipts/" + operation_id)


def calculate_totals(items: list[dict]) -> dict:
    """Sum requested quantities in integer cents, without evaluating Agent code."""
    if not isinstance(items, list) or len(items) > 1000:
        raise ToolError("Expected at most 1000 item rows")
    total_cents = 0
    quantity = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"unit_price_cents", "quantity"}:
            raise ToolError("Each row requires unit_price_cents and quantity")
        price, amount = item["unit_price_cents"], item["quantity"]
        if type(price) is not int or type(amount) is not int or not 0 <= price <= 10**9 or not 0 <= amount <= 10**6:
            raise ToolError("Prices and quantities must be bounded nonnegative integers")
        total_cents += price * amount
        quantity += amount
    return {"total_cents": total_cents, "quantity": quantity, "row_count": len(items)}


def _no_symlink_ancestors(path: Path):
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ToolError("Workspace must not contain symbolic links")
        if current.exists() and not current.is_dir():
            raise ToolError("Workspace path is occupied by a file")


class WorkspaceFiles:
    def __init__(self, workspace: str | Path):
        self.root = Path(workspace)
        if not self.root.is_absolute() or self.root == Path(self.root.anchor) or ".." in self.root.parts or any(component.lower() == "old" for component in self.root.parts):
            raise ToolError("Workspace must be an absolute non-archive directory")
        _no_symlink_ancestors(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _no_symlink_ancestors(self.root)

    def _parts(self, relative_path: str) -> list[str]:
        if not isinstance(relative_path, str) or "\\" in relative_path or any(ord(character) < 32 for character in relative_path):
            raise ToolError("Invalid workspace-relative file path")
        components = relative_path.split("/")
        if len(components) > 20 or any(part in ("", ".", "..") or part.lower() == "old" or len(part) > 255 for part in components):
            raise ToolError("Invalid workspace-relative file path")
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
            raise ToolError("Workspace file unavailable or path crosses a symbolic link") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def read_text(self, relative_path: str) -> str:
        with self._parent(relative_path, create=False) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ToolError("Only regular files of at most 2 MiB may be read")
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = None
                    try:
                        return stream.read(MAX_FILE_BYTES + 1)
                    except UnicodeDecodeError as exc:
                        raise ToolError("Workspace file is not UTF-8 text") from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def write_text(self, relative_path: str, content: str) -> dict:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ToolError("File content must be text of at most 2 MiB")
        with self._parent(relative_path, create=True) as (parent, name):
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ToolError("Destination must be a regular file, not a link or directory")
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
