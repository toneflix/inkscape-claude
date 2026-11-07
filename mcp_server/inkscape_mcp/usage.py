"""How full the conversation's context is, and how much of each usage limit is used and when it resets."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

LIMIT_NAMES = {"five_hour": "5-hour limit", "seven_day": "Weekly limit", "seven_day_opus": "Weekly Opus limit",
               "seven_day_sonnet": "Weekly Sonnet limit", "overage": "Extra usage"}
API_LIMITS = {"requests": "requests", "input-tokens": "input tokens", "output-tokens": "output tokens",
              "tokens": "tokens"}


def context_window(model: str) -> int | None:
    """Tokens the model can take in: 1M from the 4.6 models on, 200K before. None for names it can't read."""
    match = re.match(r"claude-(?:[a-z]+-)?(\d+)(?:-(\d))?(?!\d)", model or "")
    if not match:
        return None

    version = (int(match.group(1)), int(match.group(2) or 0))

    return 1_000_000 if version >= (4, 6) else 200_000


def short_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1000:
        return f"{round(count / 1000)}K"

    return str(count)


def timestamp(value) -> float | None:
    """Epoch seconds from a number (seconds or milliseconds) or an RFC 3339 time."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else float(value)

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Limit:
    """One usage limit: the share used (0 to 1) and when it resets, in epoch seconds."""

    name: str
    used: float
    resets_at: float | None = None
    status: str | None = None

    def describe(self, now: float | None = None) -> str:
        text = f"{self.name}: {round(self.used * 100)}% used"
        if self.status == "rejected":
            text = f"{self.name}: reached"
        if self.resets_at:
            text += ", " + self.reset_text(now)

        return text

    def reset_text(self, now: float | None = None) -> str:
        left = max(0, int(self.resets_at - (time.time() if now is None else now)))
        when = time.localtime(self.resets_at)
        clock = time.strftime("%H:%M", when) if left < 86400 else time.strftime("%a %H:%M", when)
        hours, minutes = divmod(left // 60, 60)
        if left < 60:
            wait = "less than a minute"
        elif hours >= 24:
            wait = f"{hours // 24} d {hours % 24} h"
        else:
            wait = f"{hours} h {minutes} min" if hours else f"{minutes} min"

        return f"resets in {wait} ({clock})"

    @staticmethod
    def share(value) -> float:
        """Claude Code reports a fraction; values above 1.5 are taken as percentages."""
        number = float(value or 0)

        return min(1.0, max(0.0, number / 100 if number > 1.5 else number))

    @classmethod
    def from_claude_code(cls, info: dict) -> list[Limit]:
        """The windows in a rate_limit_event's rate_limit_info, such as the 5-hour and weekly limits."""
        windows = info.get("unifiedWindows") or {}
        limits = [cls(LIMIT_NAMES.get(key, key.replace("_", " ").capitalize()), cls.share(window.get("utilization")),
                      timestamp(window.get("resetsAt")), window.get("status"))
                  for key, window in windows.items() if isinstance(window, dict) and "utilization" in window]
        kind = info.get("rateLimitType")
        if not limits and kind and info.get("utilization") is not None:
            limits = [cls(LIMIT_NAMES.get(kind, kind), cls.share(info["utilization"]), timestamp(info.get("resetsAt")),
                          info.get("status"))]

        return limits

    @classmethod
    def from_headers(cls, headers) -> list[Limit]:
        """The API key's tightest per-minute limit, from the anthropic-ratelimit-* response headers."""
        limits = []
        for key, label in API_LIMITS.items():
            prefix = f"anthropic-ratelimit-{key}-"
            try:
                total = float(headers.get(prefix + "limit"))
                remaining = float(headers.get(prefix + "remaining"))
            except (TypeError, ValueError):
                continue
            if total > 0:
                limits.append(cls(f"Rate limit ({label} per minute)", 1 - remaining / total,
                                  timestamp(headers.get(prefix + "reset"))))

        return [max(limits, key=lambda limit: limit.used)] if limits else []


@dataclass
class Usage:
    """What the chat shows under its header: context used of the model's window, and the usage limits."""

    context_tokens: int = 0
    context_window: int | None = None
    limits: list[Limit] = field(default_factory=list)

    @staticmethod
    def tokens(usage: dict | None) -> int:
        """Everything in the context after one response: its input, cached or not, and its output."""
        usage = usage or {}
        names = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")

        return sum(int(usage.get(name) or 0) for name in names)

    def record(self, usage: dict | None = None, limits: list[Limit] | None = None,
               window: int | None = None) -> None:
        if usage:
            self.context_tokens = self.tokens(usage)
        if limits:
            self.limits = limits
        if window:
            self.context_window = window

    @property
    def empty(self) -> bool:
        return not self.context_tokens and not self.limits

    @property
    def context_share(self) -> float | None:
        if not self.context_window:
            return None

        return min(1.0, self.context_tokens / self.context_window)

    def context_text(self) -> str:
        if not self.context_window:
            return f"Context: {short_tokens(self.context_tokens)} tokens"

        return (f"Context: {short_tokens(self.context_tokens)} of {short_tokens(self.context_window)} "
                f"({round(self.context_share * 100)}%)")

    def to_dict(self) -> dict:
        return {**asdict(self), "context_share": self.context_share, "context_text": self.context_text()}
