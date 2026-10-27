"""小型网页站点，复用工具使用的同一作用域业务 API。"""

from __future__ import annotations

import argparse
from datetime import datetime
import html
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import csv
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

import httpx


class SiteServer(ThreadingHTTPServer):
    def __init__(self, address, users_file: Path, actions_file: Path, upstream: str):
        super().__init__(address, SiteHandler)
        self.users_file = users_file
        self.actions_file = actions_file
        self.upstream = upstream.rstrip("/")
        self.sessions = {}
        self.lock = threading.Lock()

    def record(self, user, method, path, status):
        entry = {"at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                 "user": user, "method": method, "path": path, "status": status}
        with self.lock:
            self.actions_file.parent.mkdir(parents=True, exist_ok=True)
            with self.actions_file.open("a") as stream:
                stream.write(json.dumps(entry) + "\n")


class SiteHandler(BaseHTTPRequestHandler):
    server: SiteServer

    def log_message(self, *args):
        pass

    def respond(self, status, body, *, content_type="text/html; charset=utf-8", headers=None, user=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.server.record(user, self.command, self.path, status)

    def page(self, title, body, *, status=200, headers=None, user=None):
        self.respond(status, f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
                     f'<title>{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1>'
                     f'{body}</body></html>', headers=headers, user=user)

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            key = cookie["lab_session"].value if "lab_session" in cookie else None
        except Exception:
            return None
        return self.server.sessions.get(key)

    def api(self, session, method, path, **kwargs):
        with httpx.Client(timeout=10, trust_env=False) as client:
            return client.request(method, self.server.upstream + path,
                                  headers={"Authorization": "Bearer " + session["token"]}, **kwargs)

    def form(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 8192:
            raise ValueError("表单过大")
        return {k: values[0] for k, values in parse_qs(self.rfile.read(size).decode()).items()}

    def do_GET(self):
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        if url.path == "/health":
            return self.respond(200, '{"ok":true}', content_type="application/json")
        if url.path in ("/", "/login"):
            return self.page("登录测试业务", '<form method="post" action="/login">'
                             '<label>用户名<input name="username" required></label>'
                             '<label>密码<input name="password" type="password" required></label>'
                             '<button type="submit">登录</button></form>')
        session = self.session()
        if session is None:
            return self.respond(303, b"", headers={"Location": "/login"})
        user = session["username"]
        if url.path == "/items":
            try:
                page = int(params.get("page", ["1"])[0])
                if page < 1:
                    raise ValueError()
            except ValueError:
                return self.page("参数错误", "页码须为正数", status=400, user=user)
            response = self.api(session, "GET", f"/items?page={page}&page_size=3")
            if response.status_code != 200:
                return self.page("查询失败", "业务查询失败", status=502, user=user)
            payload = response.json()
            items = payload["items"]
            next_link = (f'<a href="/items?page={payload["next_page"]}">下一页</a> '
                         if payload["next_page"] else '<span>已到末页</span> ')
            rows = ''.join('<tr>' + ''.join(f'<td>{html.escape(str(item[k]))}</td>'
                                           for k in ("item_id", "name", "unit_price_cents", "available_quantity")) + '</tr>' for item in items)
            return self.page("业务数据", f'<p>当前用户：{html.escape(user)}</p><p>第 {page} 页，共 {payload["total"]} 条</p>'
                             '<table><thead><tr><th>编号</th><th>名称</th><th>单价（分）</th><th>可用数量</th></tr></thead>'
                             f'<tbody>{rows}</tbody></table>{next_link}'
                             '<a href="/order">填写业务记录</a>', user=user)
        if url.path == "/order":
            return self.order_form(user)
        if url.path in ("/receipt", "/download"):
            operation = params.get("operation_id", [""])[0]
            response = self.api(session, "GET", "/receipts/" + quote(operation, safe=""))
            if response.status_code != 200:
                return self.page("记录不存在", "未找到当前用户的操作回执", status=404, user=user)
            receipt = response.json()
            if url.path == "/receipt":
                return self.page("提交结果", '<pre id="receipt">' + html.escape(json.dumps(receipt)) + '</pre>'
                                 f'<a href="/download?operation_id={quote(operation, safe="")}">下载结果</a>'
                                 '<a href="/items">返回数据页</a>', user=user)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["operation_id", "receipt_json"])
            writer.writerow([operation, json.dumps(receipt, sort_keys=True)])
            return self.respond(200, output.getvalue(), content_type="text/csv; charset=utf-8",
                                headers={"Content-Disposition": 'attachment; filename="result.csv"'}, user=user)
        self.page("未找到", "页面不存在", status=404, user=user)

    def order_form(self, user, error="", status=200):
        self.page("填写业务记录", f'<p role="alert">{html.escape(error)}</p>'
                  '<form method="post" action="/order">'
                  '<label>操作编号<input name="operation_id" required></label>'
                  '<label>数据编号<input name="item_id" required></label>'
                  '<label>数量<input name="quantity" type="number" min="1" max="100" required></label>'
                  '<button type="submit">提交</button></form><a href="/items">返回数据页</a>',
                  status=status, user=user)

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            body = self.form()
        except (ValueError, UnicodeDecodeError):
            return self.page("参数错误", "无法读取表单", status=400)
        if path == "/login":
            username = body.get("username", "")
            users = json.loads(self.server.users_file.read_text())
            account = users.get(username)
            if account is None or not secrets.compare_digest(account["password"], body.get("password", "")):
                return self.page("登录失败", '<a href="/login">重试</a>', status=401)
            key = secrets.token_urlsafe(32)
            self.server.sessions[key] = {"username": username, "token": account["token"]}
            return self.respond(303, b"", headers={"Location": "/items", "Set-Cookie":
                                f"lab_session={key}; Path=/; HttpOnly; SameSite=Strict"}, user=username)
        session = self.session()
        if session is None:
            return self.respond(401, "需要登录")
        user = session["username"]
        if path != "/order":
            return self.page("未找到", "页面不存在", status=404, user=user)
        try:
            quantity = int(body.get("quantity", "0"))
            if not 1 <= quantity <= 100 or not body.get("operation_id") or not body.get("item_id"):
                raise ValueError()
        except ValueError:
            return self.order_form(user, "数量须为 1 到 100 的整数，编号必填", 400)
        response = self.api(session, "POST", "/writes", json={
            "operation_id": body["operation_id"], "item_id": body["item_id"], "quantity": quantity,
        })
        if response.status_code not in (200, 201):
            return self.order_form(user, "业务校验未通过，请核对编号与填写内容", 400)
        self.respond(303, b"", headers={"Location": "/receipt?operation_id=" + quote(body["operation_id"], safe="")}, user=user)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--users-file", type=Path, required=True)
    parser.add_argument("--actions-file", type=Path, required=True)
    parser.add_argument("--upstream", default="http://fixtures:8080")
    args = parser.parse_args()
    server = SiteServer((args.host, args.port), args.users_file, args.actions_file, args.upstream)
    server.serve_forever()


if __name__ == "__main__":
    main()
