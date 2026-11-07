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

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
KEY_REJECTED = "The API key was rejected. Check the key you configured."


class ClaudeError(RuntimeError):
    pass


class Config:
    """API key lookup: explicit value, then ANTHROPIC_API_KEY, then the config file."""

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
    def api_key(cls, explicit: str | None = None) -> str:
        key = (explicit or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        key = key or cls.load().get("api_key", "")
        if not key:
            raise ClaudeError(
                "No Claude API key found. Set it in the extension's Settings tab, in the ANTHROPIC_API_KEY "
                f"environment variable, or in {cls.path()} as {{\"api_key\": \"sk-ant-...\"}}."
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

    def send(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Return the response as {"content": [...], "stop_reason": ...}."""
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
    def send(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)

        try:
            with client.beta.messages.stream(betas=[FALLBACK_BETA], **self.body(system, messages, tools)) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ClaudeError(KEY_REJECTED) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError("Rate limited by the Claude API. Wait a moment and try again.") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(f"Could not reach the Claude API: {exc}") from exc

        data = message.to_dict(mode="json", exclude_none=True)

        return {"content": data["content"], "stop_reason": data.get("stop_reason")}


class HttpClaudeClient(ClaudeClient):
    timeout = 900

    @property
    def url(self) -> str:
        return os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    def send(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.body(system, messages, tools)).encode("utf-8"),
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
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ClaudeError(KEY_REJECTED) from exc
            raise ClaudeError(f"Claude API error {exc.code}: {self._detail(exc)}") from exc
        except urllib.error.URLError as exc:
            raise ClaudeError(f"Could not reach the Claude API: {exc.reason}") from exc

        return {"content": data.get("content", []), "stop_reason": data.get("stop_reason")}

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        detail = exc.read().decode("utf-8", "replace")

        try:
            return json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return detail
