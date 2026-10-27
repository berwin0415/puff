"""将共享 HTTP 结构绑定到 OpenHands 的原生工具执行机制。"""

from copy import deepcopy
import json
import re
import time

import httpx
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from openhands.sdk.tool.client_tool import ClientTool, ClientToolObservation, ClientToolSpec


class HttpTool(ClientTool):
    """保留实验结构，不添加 SDK 摘要或风险参数。"""

    @property
    def name(self):
        # 否则 ToolDefinition.__init_subclass__ 会将继承的
        # ClientTool 属性替换为由类名推导的 'http'。
        return self.client_tool_name

    def _get_tool_schema(self, add_security_risk_prediction=False, action_type=None):
        # 官方 Agent 始终请求此提示，包括 NeverConfirm 模式。
        # 保留冻结的业务结构，不增加模型风险字段；
        # 真实敏感操作规则仍由限定作用域的工具服务执行。
        if action_type is not None:
            raise ValueError("此入口必须使用普通自动执行方式和原始工具结构")
        return deepcopy(self.input_schema)


class HttpExecutor(ToolExecutor):
    def __init__(self, client, name, properties, deadline):
        self.client = client
        self.name = name
        self.properties = frozenset(properties)
        self.deadline = deadline

    def __call__(self, action, conversation=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return ClientToolObservation.from_text("分发工具前已达到活跃任务执行时限", is_error=True)
        arguments = action.model_dump(mode="json", exclude={"kind"})
        if set(arguments) - self.properties:
            return ClientToolObservation.from_text("工具参数不符合预期；不能替换作用域", is_error=True)
        try:
            # 不重试 HTTP：传输失败可能掩盖已经提交的写入。
            response = self.client.post("/call", json={"name": self.name, "arguments": arguments}, timeout=remaining)
            payload = response.json()
        except httpx.HTTPError:
            return ClientToolObservation.from_text("HTTP 工具传输失败；副作用状态未知", is_error=True)
        except ValueError:
            return ClientToolObservation.from_text("HTTP 工具返回了无效 JSON；副作用状态未知", is_error=True)
        return ClientToolObservation.from_text(
            json.dumps({"status": response.status_code, "body": payload}, ensure_ascii=False),
            is_error=response.is_error,
        )


class HttpTools:
    def __init__(self, url, token, deadline):
        self.deadline = deadline
        self.client = httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {token}"},
            trust_env=False, transport=httpx.HTTPTransport(retries=0), timeout=30,
        )
        self.schemas = []

    def load(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("发现工具前已达到活跃任务执行时限")
        response = self.client.get("/tools", timeout=min(remaining, 30))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list) or not payload["tools"]:
            raise ValueError("可信工具清单不能为空")
        specifications = []
        seen = set()
        for entry in payload["tools"]:
            if not isinstance(entry, dict):
                raise ValueError("可信工具定义无效")
            name, description, schema = entry.get("name"), entry.get("description"), entry.get("input_schema")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in seen or name in {"finish", "think"}:
                raise ValueError("可信工具名称无效、重复或属于保留名称")
            if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
                raise ValueError("可信工具结构无效")
            seen.add(name)
            specification = ClientToolSpec(name=name, description=description, parameters=schema)
            tool = HttpTool.from_spec(specification).model_copy(update={
                "executor": HttpExecutor(self.client, name, schema["properties"], self.deadline),
            })
            exported = tool.to_openai_tool(add_security_risk_prediction=True)["function"]
            if tool.name != name or exported["name"] != name or exported["parameters"] != schema:
                raise ValueError("OpenHands 更改了共享工具结构")
            register_tool(name, tool)
            specifications.append(Tool(name=name))
            self.schemas.append(deepcopy(entry))
        return specifications

    def close(self):
        self.client.close()
