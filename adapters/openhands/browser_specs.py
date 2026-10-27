"""把 MCP 原生浏览器工具转换为 OpenHands 原生 ClientTool。

工具名、描述与输入 schema 都直接取自 MCP 服务，不做改名、不改写结构；
执行仍走到官方 MCP 客户端发起的真实 call_tool。
"""

from copy import deepcopy
import json

from openhands.sdk.tool import Tool, register_tool
from openhands.sdk.tool.client_tool import ClientTool, ClientToolObservation, ClientToolSpec


class BrowserMcpTool(ClientTool):
    @property
    def name(self):
        return self.client_tool_name

    def _get_tool_schema(self, add_security_risk_prediction=False, action_type=None):
        if action_type is not None:
            raise ValueError("浏览器用例必须使用原始 MCP 工具结构")
        return deepcopy(self.input_schema)


class BrowserMcpExecutor:
    """同步执行器：把调用投递给后台 MCP 事件循环。"""

    def __init__(self, bridge, name, properties):
        self.bridge = bridge
        self.name = name
        self.properties = frozenset(properties)

    def __call__(self, action, conversation=None):
        arguments = action.model_dump(mode="json", exclude={"kind"})
        # OpenHands 会为未提供的参数显式填入 null；MCP 服务按 JSON Schema
        # 校验时不允许 null。这里只移除“模型没有提供”的 null 值，
        # 不改变任何真实取值，也不补齐默认值。
        arguments = {key: value for key, value in arguments.items() if value is not None}
        if set(arguments) - self.properties:
            return ClientToolObservation.from_text("浏览器工具参数不符合原始结构", is_error=True)
        try:
            payload, is_error = self.bridge.call(self.name, arguments)
        except Exception as error:
            return ClientToolObservation.from_text(f"浏览器 MCP 传输或协议错误：{error}", is_error=True)
        return ClientToolObservation.from_text(json.dumps(payload, ensure_ascii=False), is_error=is_error)


def browser_specifications(bridge, definitions):
    specifications = []
    for name in sorted(definitions):
        definition = definitions[name]
        schema = deepcopy(definition.inputSchema or {"type": "object", "properties": {}})
        specification = ClientToolSpec(name=name, description=definition.description or name, parameters=schema)
        tool = BrowserMcpTool.from_spec(specification).model_copy(update={
            "executor": BrowserMcpExecutor(bridge, name, schema.get("properties", {})),
        })
        register_tool(name, tool)
        specifications.append(Tool(name=name))
    return specifications
