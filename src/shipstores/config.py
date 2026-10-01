"""Credentials and account settings for both stores.

Everything comes from environment variables, with an optional TOML file for the
values that are awkward to pass as env (like the Play Console app id map).
Environment variables always win over the file.
Tool groups can be selected with SHIPSTORES_TOOLSETS or [server].toolsets.

Config file: ~/.config/shipstores/config.toml (override with
SHIPSTORES_CONFIG). See config.example.toml in the repository.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "shipstores"
TOOLSETS = frozenset({"apple", "core", "eas", "play"})


class ConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _file() -> dict:
    path = Path(os.environ.get("SHIPSTORES_CONFIG", CONFIG_DIR / "config.toml")).expanduser()
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _setting(env: str, section: str, key: str, default: str | None = None) -> str | None:
    value = os.environ.get(env)
    if value:
        return value
    value = _file().get(section, {}).get(key)
    return str(value) if value not in (None, "") else default


def load_toolsets() -> frozenset[str]:
    """Return enabled toolsets, always including core diagnostics."""
    configured = os.environ.get("SHIPSTORES_TOOLSETS")
    if configured:
        toolsets = {item.strip().lower() for item in configured.split(",") if item.strip()}
    else:
        server_config = _file().get("server", {})
        if not isinstance(server_config, dict):
            raise ConfigError("[server] must be a table in config.toml.")
        configured = server_config.get("toolsets")
        if configured is None:
            return TOOLSETS
        if not isinstance(configured, list) or any(
            not isinstance(item, str) for item in configured
        ):
            raise ConfigError("[server] toolsets must be an array of names in config.toml.")
        toolsets = {item.strip().lower() for item in configured if item.strip()}

    unknown = toolsets - TOOLSETS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(f"Unknown toolset(s): {names}. Choose from: apple, core, eas, play.")
    return frozenset(toolsets | {"core"})


@dataclass(frozen=True)
class AppleConfig:
    key_id: str
    issuer_id: str
    private_key_path: Path
    team_id: str

    @property
    def private_key(self) -> str:
        return self.private_key_path.read_text()


@dataclass(frozen=True)
class PlayConfig:
    service_account_path: Path


def load_apple() -> AppleConfig:
    key_id = _setting("ASC_KEY_ID", "apple", "key_id")
    issuer_id = _setting("ASC_ISSUER_ID", "apple", "issuer_id")
    if not key_id or not issuer_id:
        raise ConfigError(
            "App Store Connect API key not configured: set ASC_KEY_ID and ASC_ISSUER_ID "
            "(or [apple] key_id / issuer_id in config.toml). "
            "Create the key in App Store Connect > Users and Access > Integrations."
        )
    default_key = CONFIG_DIR / f"AuthKey_{key_id}.p8"
    return AppleConfig(
        key_id=key_id,
        issuer_id=issuer_id,
        private_key_path=Path(_setting("ASC_PRIVATE_KEY_PATH", "apple", "private_key_path", str(default_key))).expanduser(),
        # Optional: store_doctor detects it from any registered bundle ID
        team_id=_setting("APPLE_TEAM_ID", "apple", "team_id", "") or "",
    )


def load_play() -> PlayConfig:
    default_sa = CONFIG_DIR / "play-service-account.json"
    return PlayConfig(
        service_account_path=Path(
            _setting("PLAY_SERVICE_ACCOUNT_PATH", "play", "service_account_path", str(default_sa))
        ).expanduser()
    )


def play_developer_id() -> str:
    """Play Console developer id. Only shows up in the console URL; no API resolves it."""
    value = _setting("PLAY_DEVELOPER_ID", "play", "developer_id")
    if not value:
        raise ConfigError(
            "Play Console developer id not configured: set PLAY_DEVELOPER_ID "
            "(the number after /developers/ in the Play Console URL)."
        )
    return value


def play_console_app_ids() -> dict[str, str]:
    """packageName -> numeric Play Console app id (the number after /app/ in the console URL)."""
    return {str(k): str(v) for k, v in _file().get("play", {}).get("console_app_ids", {}).items()}
