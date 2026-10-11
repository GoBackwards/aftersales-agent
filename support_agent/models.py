"""Environment-configured Chat Completions adapter using Python's standard library."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Any


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw_message: dict[str, Any] | None = None
    model_id: str | None = None


# Validated Chat Completions tool choice carried alongside list-compatible schemas.

def _validated_choice(choice, definitions):
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        if choice == "required" and not definitions:
            raise ValueError("Required tool choice needs at least one tool.")
        return choice
    if (isinstance(choice, dict) and set(choice) == {"type", "function"}
            and choice["type"] == "function" and isinstance(choice["function"], dict)
            and set(choice["function"]) == {"name"}):
        name = choice["function"]["name"]
        if isinstance(name, str) and name.strip():
            matches = sum(isinstance(tool, dict) and tool.get("type") == "function"
                          and isinstance(tool.get("function"), dict)
                          and tool["function"].get("name") == name for tool in definitions)
            if matches == 1:
                return deepcopy(choice)
        raise ValueError("Named tool choice must identify one available function.")
    raise ValueError("Tool choice must be auto, none, required, or a named function.")


class ToolDefinitions(list[dict]):
    """Keep existing model/wrapper signatures while opting into tool selection.

    JSON remains the ordinary schema array; request/audit writers must record
    ``requested_tool_choice`` separately. Deepcopy preserves both the schemas
    and this metadata. Revalidate on use because list contents remain mutable.
    ``strict_functions`` names functions whose schema may be sent in a
    provider's strict (schema-constrained) mode when the adapter enables it;
    the caller still validates every result itself.
    """

    def __init__(self, definitions=(), *, tool_choice="auto", strict_functions=()):
        super().__init__(definitions)
        self._tool_choice = _validated_choice(tool_choice, self)
        names = {tool["function"]["name"] for tool in self
                 if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
        if not set(strict_functions) <= names:
            raise ValueError("Strict functions must be defined in this tool list.")
        self.strict_functions = frozenset(strict_functions)

    @property
    def tool_choice(self):
        return _validated_choice(self._tool_choice, self)


# Keywords a strict (constrained-decoding) tool schema does not accept. Length
# and size limits stay enforced by the caller's own validation of the result.
_STRICT_UNSUPPORTED = frozenset({"minLength", "maxLength", "minItems", "maxItems", "default", "title"})


def strict_schema(schema):
    """Return a copy of a JSON schema in the strict tool-calling form.

    Every object lists all of its properties as required and forbids extra
    ones; unsupported keywords are dropped. Types, enums, anyOf, patterns,
    numeric bounds and descriptions are kept. Optional fields therefore have to
    be written out explicitly by the model (for example an empty list).
    """
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return deepcopy(schema)
    result = {key: strict_schema(value) for key, value in schema.items()
              if key not in _STRICT_UNSUPPORTED and key != "properties"}
    if isinstance(schema.get("properties"), dict):
        # Property names are data, not keywords: keep every one of them.
        result["properties"] = {name: strict_schema(value) for name, value in schema["properties"].items()}
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties", {})
        result["required"] = list(properties)
        result["additionalProperties"] = False
    return result


def strict_tool_definitions(definitions):
    """The request's tools with the opted-in functions in strict form (others unchanged)."""
    names = getattr(definitions, "strict_functions", frozenset())
    tools = []
    for tool in definitions:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name") in names:
            function = {**deepcopy(function), "strict": True,
                        "parameters": strict_schema(function.get("parameters", {}))}
            tool = {**deepcopy(tool), "function": function}
        tools.append(deepcopy(tool))
    return tools


def requested_tool_choice(definitions: list[dict]) -> str | dict | None:
    """Return the adapter's actual field value; None means it omits the field."""
    choice = definitions.tool_choice if isinstance(definitions, ToolDefinitions) else "auto"
    return choice if definitions else None


class ModelError(RuntimeError):
    def __init__(self, public_message: str, *, retryable: bool = False,
                 status_code: int | None = None, retry_after: float | None = None):
        self.public_message = public_message
        self.retryable = retryable is True
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.retry_after = (float(retry_after) if type(retry_after) in (int, float)
                            and math.isfinite(retry_after) and retry_after >= 0 else None)
        super().__init__(public_message)


def _retry_after(headers) -> float | None:
    """Parse only a duration/date; never expose arbitrary header contents."""
    try:
        value = headers.get("Retry-After") if headers is not None else None
        if not isinstance(value, str):
            return None
        try:
            seconds = float(value)
        except ValueError:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def load_dotenv(path: Path) -> None:
    """Load only the explicit project file; never search parents or print secrets."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f".env 第 {number} 行应使用 KEY=VALUE 格式。")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key.startswith("LLM_"):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class APIConfig:
    base_url: str
    model: str
    api_key: str
    provider: str = "compatible"
    max_tokens: int = 1024
    timeout: float = 45
    temperature: float | None = None
    # Send opted-in functions in the provider's strict tool mode (DeepSeek:
    # schema-constrained decoding on the /beta endpoint). Results are still
    # validated by the caller; this only stops malformed argument JSON.
    strict_tools: bool = False

    @classmethod
    def from_env(cls) -> "APIConfig":
        required = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError("缺少配置：" + ", ".join(missing) + "。请填写项目 .env。")
        temperature = os.environ.get("LLM_TEMPERATURE", "").strip()
        strict = os.environ.get("LLM_STRICT_TOOLS", "").strip().lower()
        if strict not in {"", "0", "false", "no", "off", "1", "true", "yes", "on"}:
            raise ValueError("LLM_STRICT_TOOLS 必须为 1/true/yes 或 0/false/no，或不设置。")
        config = cls(
            base_url=os.environ["LLM_BASE_URL"].strip().rstrip("/"),
            model=os.environ["LLM_MODEL"].strip(),
            api_key=os.environ["LLM_API_KEY"].strip(),
            provider=os.environ.get("LLM_PROVIDER", "compatible").strip().lower(),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "1024")),
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "45")),
            temperature=float(temperature) if temperature else None,
            strict_tools=strict in {"1", "true", "yes", "on"},
        )
        config.validate()
        return config

    def validate(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM_BASE_URL 应为不含密钥、查询参数或片段的 API 基础地址。")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("远程 API 请使用 HTTPS；本地模型可使用 localhost HTTP。")
        if self.provider not in {"deepseek", "qwen", "compatible"}:
            raise ValueError("LLM_PROVIDER 必须为 deepseek、qwen 或 compatible。")
        if not 1 <= self.max_tokens <= 32768 or not 0 < self.timeout <= 300:
            raise ValueError("请检查 LLM_MAX_TOKENS（1–32768）和超时（0–300秒）。")
        if self.temperature is not None and (
            type(self.temperature) not in (int, float)
            or not 0 <= self.temperature <= 2 or not math.isfinite(self.temperature)
        ):
            raise ValueError("LLM_TEMPERATURE 必须为 0–2 之间的有限数值，或不设置。")
        if type(self.strict_tools) is not bool:
            raise ValueError("LLM_STRICT_TOOLS 必须是布尔值。")
        if self.strict_tools and self.provider == "deepseek" and not self.base_url.rstrip("/").endswith("/beta"):
            raise ValueError("DeepSeek 严格工具模式需要 beta 地址，例如 https://api.deepseek.com/beta。")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A mistaken endpoint must not forward the Authorization header elsewhere.
        return None


class ChatCompletionsModel:
    def __init__(self, config: APIConfig, *, json_output: bool = False):
        config.validate()
        if type(json_output) is not bool:
            raise ValueError("json_output 必须是布尔值。")
        self.config = config
        self.json_output = json_output
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.last_request_metadata = None

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        return self.complete_with_timeout(messages, tools, timeout=self.config.timeout)

    def complete_with_timeout(self, messages: list[dict], tools: list[dict], *, timeout: float) -> ModelResponse:
        # Never attribute a previous request to an attempt rejected before send.
        self.last_request_metadata = None
        config = self.config
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ModelError("模型请求时限必须为正数。")
        request_timeout = min(float(timeout), config.timeout)
        body = {"model": config.model, "messages": messages, "max_tokens": config.max_tokens}
        if config.temperature is not None:
            body["temperature"] = config.temperature
        tool_choice = requested_tool_choice(tools)
        strict = sorted(getattr(tools, "strict_functions", ())) if config.strict_tools else []
        if tools:
            body.update(tools=strict_tool_definitions(tools) if strict else tools, tool_choice=tool_choice)
        if self.json_output:
            body["response_format"] = {"type": "json_object"}
        if config.provider == "deepseek":
            body["thinking"] = {"type": "disabled"}
        elif config.provider == "qwen":
            body["enable_thinking"] = False
            body["parallel_tool_calls"] = False
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.last_request_metadata = {
            "body_sha256": hashlib.sha256(encoded_body).hexdigest(),
            "body_canonical_sha256": hashlib.sha256(json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
            "parameters": {name: deepcopy(body[name]) for name in (
                "model", "max_tokens", "temperature", "tool_choice", "thinking",
                "response_format", "enable_thinking", "parallel_tool_calls") if name in body},
        }
        if strict:
            self.last_request_metadata["parameters"]["strict_functions"] = strict
        request = urllib.request.Request(
            config.base_url.rstrip("/") + "/chat/completions",
            data=encoded_body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + config.api_key},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=request_timeout) as response:
                payload = json.loads(response.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise ModelError(f"模型 API 返回 HTTP {exc.code}；请检查地址、模型权限、额度和请求兼容性。",
                             retryable=exc.code in {429, 500, 502, 503, 504},
                             status_code=exc.code, retry_after=_retry_after(exc.headers)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError("无法连接模型 API 或请求超时；请检查网络和 API 地址。", retryable=True) from None
        except (ValueError, UnicodeError):
            raise ModelError("模型 API 没有返回有效 JSON。") from None
        try:
            choice = payload["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ModelError("模型输出被截断或过滤，本轮未执行其中的工具；请检查任务或输出预算。")
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("unexpected role")
            content = message.get("content")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise ValueError("content must be text")
            raw_calls = message.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ValueError("tool_calls must be an array")
            usage = payload.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise ValueError("usage must be an object")
            calls = []
            identifiers = set()
            for raw_call in raw_calls:
                call_id = raw_call["id"]
                if not isinstance(call_id, str) or not call_id or call_id in identifiers:
                    raise ValueError("invalid tool id")
                identifiers.add(call_id)
                function = raw_call["function"]
                name = function["name"]
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("invalid function name")
                arguments = function.get("arguments", "")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        pass  # Domain validation returns an error the model can correct.
                calls.append(ToolCall(call_id, name, arguments))
            if not calls and not content.strip():
                raise ValueError("empty response")
            response_model = payload.get("model")
            if response_model is not None and not isinstance(response_model, str):
                raise ValueError("invalid model identifier")
            return ModelResponse(content, calls, usage, dict(message), response_model)
        except ModelError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelError("模型响应格式不兼容：需要 Chat Completions messages/tool_calls。") from None


class LazyModel:
    """Do not construct a provider for cached/rejected requests."""
    def __init__(self, factory):
        self.factory, self.model = factory, None

    @property
    def mode(self):
        return getattr(self.model, "mode", getattr(self.factory, "mode", "model"))

    def complete(self, messages, tools):
        if self.model is None:
            self.model = self.factory()
        return self.model.complete(messages, tools)

    def complete_with_timeout(self, messages, tools, *, timeout):
        if self.model is None:
            self.model = self.factory()
        bounded = getattr(self.model, "complete_with_timeout", None)
        if callable(bounded):
            return bounded(messages, tools, timeout=timeout)
        return self.model.complete(messages, tools)
