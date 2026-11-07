"""Claude API client for the Ask Claude extension.

Uses the `anthropic` SDK when it is importable, otherwise a standard-library HTTPS request,
because Inkscape's bundled Python on Windows and macOS usually lacks extra packages.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
KEY_REJECTED = "The API key was rejected. Check it in Settings."


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
    """Sends one message to Claude and returns the reply text."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, effort: str = "medium", max_tokens: int = 64000):
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    @classmethod
    def create(cls, *args, **kwargs) -> ClaudeClient:
        if os.environ.get("INKSCAPE_CLAUDE_NO_SDK") != "1":
            try:
                import anthropic  # noqa: F401

                return SdkClaudeClient(*args, **kwargs)
            except ImportError:
                pass

        return HttpClaudeClient(*args, **kwargs)

    def ask(self, system: str, content: list) -> str:
        raise NotImplementedError

    def body(self, system: str, content: list) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "fallbacks": "default",
            "messages": [{"role": "user", "content": content}],
        }

    @staticmethod
    def finish(stop_reason: str | None, text: str) -> str:
        if stop_reason == "refusal":
            raise ClaudeError("Claude declined this request.")
        if stop_reason == "max_tokens":
            raise ClaudeError("Claude's answer was cut off (max output tokens reached). "
                              "Raise 'Max output tokens' in Settings or work on a smaller selection.")
        if not text.strip():
            raise ClaudeError("Claude returned an empty answer.")

        return text


class SdkClaudeClient(ClaudeClient):
    def ask(self, system: str, content: list) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)

        try:
            with client.beta.messages.stream(betas=[FALLBACK_BETA], **self.body(system, content)) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ClaudeError(KEY_REJECTED) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError("Rate limited by the Claude API. Wait a moment and try again.") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(f"Could not reach the Claude API: {exc}") from exc

        text = "".join(block.text for block in message.content if block.type == "text")

        return self.finish(message.stop_reason, text)


class HttpClaudeClient(ClaudeClient):
    timeout = 900

    @property
    def url(self) -> str:
        return os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    def ask(self, system: str, content: list) -> str:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.body(system, content)).encode("utf-8"),
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

        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")

        return self.finish(data.get("stop_reason"), text)

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        detail = exc.read().decode("utf-8", "replace")

        try:
            return json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return detail
