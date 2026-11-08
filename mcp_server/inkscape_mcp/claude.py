"""Claude API client shared by the Inkscape extension, the drawing agent and the chat panel.

Uses the `anthropic` SDK when it is importable, otherwise a standard-library HTTPS request,
because Inkscape's bundled Python on Windows and macOS usually lacks extra packages.
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .usage import Limit

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
KEY_REJECTED = "The API key was rejected. Check the key you configured."
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BACKENDS = ("api", "claude-code")


class ClaudeError(RuntimeError):
    pass


class Config:
    """Settings shared by the extension and the chat: the API key (explicit value, then ANTHROPIC_API_KEY, then
    the config file), and the chat's model, effort and backend (an API key, or the `claude` command)."""

    @staticmethod
    def path() -> Path:
        if os.name == "nt" and os.environ.get("APPDATA"):
            return Path(os.environ["APPDATA"]) / "inkscape-claude" / "config.json"

        return Path.home() / ".config" / "inkscape-claude" / "config.json"

    @classmethod
    def load(cls) -> dict:
        path = cls.path()
        if not path.exists():
            return {}

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClaudeError(f"Could not read {path}: {exc}") from exc

    @classmethod
    def save(cls, **values: str) -> None:
        """Merges values into the config file, which only the current user can read."""
        path = cls.path()
        data = {**cls.load(), **values}
        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        path.chmod(0o600)

    @classmethod
    def chat_settings(cls) -> dict[str, str | None]:
        """Model, effort, backend (None when never chosen) and claude_path (None when not set)."""
        data = cls.load()
        effort = data.get("effort") if data.get("effort") in EFFORTS else "medium"
        backend = data.get("backend") if data.get("backend") in BACKENDS else None

        return {"model": data.get("model") or DEFAULT_MODEL, "effort": effort, "backend": backend,
                "claude_path": data.get("claude_path") or None}

    @classmethod
    def find_api_key(cls, explicit: str | None = None) -> str:
        key = (explicit or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()

        return key or cls.load().get("api_key", "")

    @classmethod
    def api_key(cls, explicit: str | None = None) -> str:
        key = cls.find_api_key(explicit)
        if not key:
            raise ClaudeError(
                "No Claude API key found. Set it in the chat's Settings, the extension's Settings tab, the "
                f"ANTHROPIC_API_KEY environment variable, or {cls.path()} as {{\"api_key\": \"sk-ant-...\"}}."
            )

        return key


class ClaudeClient:
    """Sends Messages API requests. Responses are plain dicts so both transports look the same."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, effort: str = "medium", max_tokens: int = 64000):
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    @classmethod
    def create(cls, *args, **kwargs) -> ClaudeClient:
        use_sdk = os.environ.get("INKSCAPE_CLAUDE_NO_SDK") != "1" and importlib.util.find_spec("anthropic")

        return SdkClaudeClient(*args, **kwargs) if use_sdk else HttpClaudeClient(*args, **kwargs)

    def send(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             on_text: Callable[[str], None] | None = None) -> dict:
        """Return the response as {"content": [...], "stop_reason": ..., "usage": {...}, "limits": [Limit]}.
        on_text gets the reply text as it arrives."""
        raise NotImplementedError

    def ask(self, system: str, content: list) -> str:
        response = self.send(system, [{"role": "user", "content": content}])

        return self.finish(response)

    def body(self, system: str, messages: list[dict], tools: list[dict] | None) -> dict:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
            "fallbacks": "default",
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

        return body

    @staticmethod
    def text(response: dict) -> str:
        return "".join(block.get("text", "") for block in response["content"] if block.get("type") == "text")

    @classmethod
    def finish(cls, response: dict) -> str:
        stop_reason = response.get("stop_reason")
        if stop_reason == "refusal":
            raise ClaudeError("Claude declined this request.")
        if stop_reason == "max_tokens":
            raise ClaudeError("Claude's answer was cut off (max output tokens reached). "
                              "Raise the output token limit or work on a smaller part of the drawing.")

        text = cls.text(response)
        if not text.strip():
            raise ClaudeError("Claude returned an empty answer.")

        return text


class SdkClaudeClient(ClaudeClient):
    def send(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             on_text: Callable[[str], None] | None = None) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)

        try:
            with client.beta.messages.stream(betas=[FALLBACK_BETA], **self.body(system, messages, tools)) as stream:
                for event in stream:
                    if on_text and event.type == "text":
                        on_text(event.text)
                message = stream.get_final_message()
                limits = Limit.from_headers(stream.response.headers)
        except anthropic.AuthenticationError as exc:
            raise ClaudeError(KEY_REJECTED) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError("Rate limited by the Claude API. Wait a moment and try again.") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(f"Could not reach the Claude API: {exc}") from exc

        data = message.to_dict(mode="json", exclude_none=True)

        return {"content": data["content"], "stop_reason": data.get("stop_reason"), "usage": data.get("usage"),
                "limits": limits}


class HttpClaudeClient(ClaudeClient):
    timeout = 900

    @property
    def url(self) -> str:
        return os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    def send(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             on_text: Callable[[str], None] | None = None) -> dict:
        body = self.body(system, messages, tools)
        if on_text:
            body["stream"] = True
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": FALLBACK_BETA,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                limits = Limit.from_headers(response.headers)
                if on_text:
                    return {**MessageStream(on_text).read(response), "limits": limits}
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ClaudeError(KEY_REJECTED) from exc
            raise ClaudeError(f"Claude API error {exc.code}: {self._detail(exc)}") from exc
        except urllib.error.URLError as exc:
            raise ClaudeError(f"Could not reach the Claude API: {exc.reason}") from exc

        return {"content": data.get("content", []), "stop_reason": data.get("stop_reason"), "usage": data.get("usage"),
                "limits": limits}

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        detail = exc.read().decode("utf-8", "replace")

        try:
            return json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return detail


class MessageStream:
    """Builds a Messages API response from its server-sent events, passing reply text on as it arrives."""

    def __init__(self, on_text: Callable[[str], None]):
        self.on_text = on_text
        self.blocks: dict[int, dict] = {}
        self.partial_json: dict[int, str] = {}
        self.stop_reason: str | None = None
        self.usage: dict = {}

    def read(self, lines) -> dict:
        data: list[str] = []
        for raw in lines:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                self.handle(json.loads("\n".join(data)))
                data = []
        if data:
            self.handle(json.loads("\n".join(data)))

        return {"content": [self.blocks[index] for index in sorted(self.blocks)], "stop_reason": self.stop_reason,
                "usage": self.usage}

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        index = event.get("index", 0)

        if kind == "error":
            raise ClaudeError(f"Claude API error: {event.get('error', {}).get('message', event)}")
        if kind == "content_block_start":
            self.blocks[index] = dict(event["content_block"])
            if self.blocks[index].get("type") == "tool_use":
                self.partial_json[index] = ""
        elif kind == "content_block_delta":
            self.apply(self.blocks[index], index, event["delta"])
        elif kind == "content_block_stop" and index in self.partial_json:
            self.blocks[index]["input"] = json.loads(self.partial_json.pop(index) or "{}")
        elif kind == "message_start":
            self.add_usage(event.get("message", {}).get("usage"))
        elif kind == "message_delta":
            self.stop_reason = event.get("delta", {}).get("stop_reason", self.stop_reason)
            self.add_usage(event.get("usage"))

    def add_usage(self, usage: dict | None) -> None:
        self.usage.update({name: value for name, value in (usage or {}).items() if value is not None})

    def apply(self, block: dict, index: int, delta: dict) -> None:
        kind = delta.get("type")
        if kind == "text_delta":
            block["text"] = block.get("text", "") + delta["text"]
            self.on_text(delta["text"])
        elif kind == "input_json_delta":
            self.partial_json[index] += delta.get("partial_json", "")
        elif kind == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta["thinking"]
        elif kind == "signature_delta":
            block["signature"] = delta["signature"]
        elif kind == "citations_delta":
            block.setdefault("citations", []).append(delta["citation"])
