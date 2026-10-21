"""Bind the shared HTTP schemas to OpenHands' native tool execution."""

from copy import deepcopy
import json
import re
import time

import httpx
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from openhands.sdk.tool.client_tool import ClientTool, ClientToolObservation, ClientToolSpec


class HttpTool(ClientTool):
    """Retain the experiment schema without SDK summary/risk parameters."""

    @property
    def name(self):
        # ToolDefinition.__init_subclass__ otherwise replaces the inherited
        # ClientTool property with the class-derived name 'http'.
        return self.client_tool_name

    def _get_tool_schema(self, add_security_risk_prediction=False, action_type=None):
        # The official Agent always requests this hint, including NeverConfirm.
        # Keep the frozen business schema instead of adding model risk fields;
        # actual sensitive rules remain enforced by the scoped tool service.
        if action_type is not None:
            raise ValueError("This entry requires ordinary automatic execution and the original tool schema")
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
            return ClientToolObservation.from_text("Active task deadline reached before tool dispatch", is_error=True)
        arguments = action.model_dump(mode="json", exclude={"kind"})
        if set(arguments) - self.properties:
            return ClientToolObservation.from_text("Unexpected tool argument; scope cannot be replaced", is_error=True)
        try:
            # No HTTP retry: a transport failure can conceal a committed write.
            response = self.client.post("/call", json={"name": self.name, "arguments": arguments}, timeout=remaining)
            payload = response.json()
        except httpx.HTTPError:
            return ClientToolObservation.from_text("HTTP tool transport failure; side-effect state is unknown", is_error=True)
        except ValueError:
            return ClientToolObservation.from_text("HTTP tool returned invalid JSON; side-effect state is unknown", is_error=True)
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
            raise TimeoutError("Active task deadline reached before tool discovery")
        response = self.client.get("/tools", timeout=min(remaining, 30))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list) or not payload["tools"]:
            raise ValueError("Expected a nonempty trusted tools listing")
        specifications = []
        seen = set()
        for entry in payload["tools"]:
            if not isinstance(entry, dict):
                raise ValueError("Invalid trusted tool definition")
            name, description, schema = entry.get("name"), entry.get("description"), entry.get("input_schema")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in seen or name in {"finish", "think"}:
                raise ValueError("Invalid, duplicate or reserved trusted tool name")
            if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
                raise ValueError("Invalid trusted tool schema")
            seen.add(name)
            specification = ClientToolSpec(name=name, description=description, parameters=schema)
            tool = HttpTool.from_spec(specification).model_copy(update={
                "executor": HttpExecutor(self.client, name, schema["properties"], self.deadline),
            })
            exported = tool.to_openai_tool(add_security_risk_prediction=True)["function"]
            if tool.name != name or exported["name"] != name or exported["parameters"] != schema:
                raise ValueError("OpenHands changed the shared tool schema")
            register_tool(name, tool)
            specifications.append(Tool(name=name))
            self.schemas.append(deepcopy(entry))
        return specifications

    def close(self):
        self.client.close()
