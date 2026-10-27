"""Configuration, read only from environment variables. Secrets are never printed or logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-4-5"

# The PayPal layer refuses to talk to anything except the sandbox (or localhost for tests).
_ALLOWED_PAYPAL_HOSTS = {"api-m.sandbox.paypal.com", "localhost", "127.0.0.1"}
_ALLOWED_ANTHROPIC_HOSTS = {"api.anthropic.com", "localhost", "127.0.0.1"}


class ConfigError(Exception):
    pass


def _check_host(url: str, allowed: set[str], what: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in allowed:
        raise ConfigError(f"{what} host '{host}' is not allowed (allowed: {', '.join(sorted(allowed))})")
    return url.rstrip("/")


@dataclass(frozen=True)
class Config:
    paypal_client_id: str | None
    paypal_client_secret: str | None
    paypal_base_url: str
    anthropic_api_key: str | None
    anthropic_base_url: str
    model: str
    data_dir: Path
    force_mock: bool
    port: int

    @property
    def paypal_mode(self) -> str:
        """'sandbox' when both keys are present, otherwise 'mock' (local testing only)."""
        if self.force_mock:
            return "mock"
        return "sandbox" if (self.paypal_client_id and self.paypal_client_secret) else "mock"

    @property
    def ai_mode(self) -> str:
        return "ai" if self.anthropic_api_key else "rules"

    def public_status(self) -> dict:
        """Safe to show in the UI or CLI: booleans and names only, never secret values."""
        return {
            "paypal_mode": self.paypal_mode,
            "paypal_base_url": self.paypal_base_url if self.paypal_mode == "sandbox" else None,
            "checklist_mode": self.ai_mode,
            "model": self.model if self.ai_mode == "ai" else None,
        }


def load_config(env: dict | None = None) -> Config:
    e = os.environ if env is None else env
    base = _check_host(e.get("PAYPAL_BASE_URL") or SANDBOX_BASE_URL, _ALLOWED_PAYPAL_HOSTS, "PayPal")
    abase = _check_host(e.get("ANTHROPIC_BASE_URL") or ANTHROPIC_BASE_URL, _ALLOWED_ANTHROPIC_HOSTS, "Anthropic")
    return Config(
        paypal_client_id=e.get("PAYPAL_CLIENT_ID") or None,
        paypal_client_secret=e.get("PAYPAL_CLIENT_SECRET") or None,
        paypal_base_url=base,
        anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
        anthropic_base_url=abase,
        model=e.get("RELEASEGATE_MODEL") or DEFAULT_MODEL,
        data_dir=Path(e.get("RELEASEGATE_DATA") or "data").resolve(),
        force_mock=(e.get("RELEASEGATE_FORCE_MOCK", "").lower() in {"1", "true", "yes"}),
        port=int(e.get("RELEASEGATE_PORT") or 8765),
    )
