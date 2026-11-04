"""Configuration, read once from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

VISIBILITIES = ("private", "internal", "public")


class ConfigError(ValueError):
    pass


def parse_bool(value: str | None, default: bool, name: str = "value") -> bool:
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: expected true/false, got {value!r}")


_DURATION_PART = re.compile(r"(\d+)\s*([smhd])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | None, default: int, name: str = "value") -> int:
    """Parse '3600', '90s', '30m', '1h', '1h30m' into seconds."""
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v.isdigit():
        seconds = int(v)
    else:
        compact = v.replace(" ", "")
        parts = _DURATION_PART.findall(compact)
        if not parts or "".join(n + u for n, u in parts) != compact:
            raise ConfigError(f"{name}: cannot parse duration {value!r} (use e.g. 3600, 30m, 1h, 1h30m)")
        seconds = sum(int(n) * _UNITS[u] for n, u in parts)
    if seconds < 60:
        raise ConfigError(f"{name}: must be at least 60 seconds, got {seconds}")
    return seconds


def parse_list(value: str | None) -> list[str]:
    if not value:
        return []
    items = []
    for raw in re.split(r"[,\n]", value):
        item = raw.strip()
        if not item:
            continue
        # Accept "owner/name" as well as "name".
        item = item.rsplit("/", 1)[-1]
        items.append(item.lower())
    return items


@dataclass
class Config:
    github_token: str
    gitlab_token: str
    gitlab_namespace: str
    gitlab_url: str = "https://gitlab.com"
    github_api_url: str = "https://api.github.com"
    sync_interval: int = 3600
    include_forks: bool = False
    include_archived: bool = True
    exclude_repos: list[str] = field(default_factory=list)
    gitlab_visibility: str = "private"
    enable_lfs: bool = False
    dry_run: bool = False
    adopt_existing: bool = False
    data_dir: Path = Path("/data")
    git_timeout: int = 3600

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / ".lock"

    def describe(self) -> str:
        """One-line summary of the non-secret settings, for the startup log."""
        return (
            f"gitlab={self.gitlab_url} namespace={self.gitlab_namespace} interval={self.sync_interval}s "
            f"forks={self.include_forks} archived={self.include_archived} "
            f"exclude={','.join(self.exclude_repos) or '-'} visibility={self.gitlab_visibility} "
            f"lfs={self.enable_lfs} dry_run={self.dry_run} adopt_existing={self.adopt_existing} "
            f"data_dir={self.data_dir}"
        )


def interval_from_env(env: Mapping[str, str] | None = None) -> int:
    """Used by the healthcheck, which must not require the tokens."""
    env = os.environ if env is None else env
    return parse_duration(env.get("SYNC_INTERVAL"), 3600, "SYNC_INTERVAL")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    missing = [k for k in ("GITHUB_TOKEN", "GITLAB_TOKEN", "GITLAB_NAMESPACE") if not env.get(k, "").strip()]
    if missing:
        raise ConfigError(f"required environment variable(s) not set: {', '.join(missing)}")

    visibility = env.get("GITLAB_VISIBILITY", "").strip().lower() or "private"
    if visibility not in VISIBILITIES:
        raise ConfigError(f"GITLAB_VISIBILITY must be one of {VISIBILITIES}, got {visibility!r}")

    return Config(
        github_token=env["GITHUB_TOKEN"].strip(),
        gitlab_token=env["GITLAB_TOKEN"].strip(),
        gitlab_namespace=env["GITLAB_NAMESPACE"].strip().strip("/"),
        gitlab_url=(env.get("GITLAB_URL", "").strip() or "https://gitlab.com").rstrip("/"),
        github_api_url=(env.get("GITHUB_API_URL", "").strip() or "https://api.github.com").rstrip("/"),
        sync_interval=parse_duration(env.get("SYNC_INTERVAL"), 3600, "SYNC_INTERVAL"),
        include_forks=parse_bool(env.get("INCLUDE_FORKS"), False, "INCLUDE_FORKS"),
        include_archived=parse_bool(env.get("INCLUDE_ARCHIVED"), True, "INCLUDE_ARCHIVED"),
        exclude_repos=parse_list(env.get("EXCLUDE_REPOS")),
        gitlab_visibility=visibility,
        enable_lfs=parse_bool(env.get("ENABLE_LFS"), False, "ENABLE_LFS"),
        dry_run=parse_bool(env.get("DRY_RUN"), False, "DRY_RUN"),
        adopt_existing=parse_bool(env.get("ADOPT_EXISTING"), False, "ADOPT_EXISTING"),
        data_dir=Path(env.get("DATA_DIR", "").strip() or "/data"),
        git_timeout=parse_duration(env.get("GIT_TIMEOUT"), 3600, "GIT_TIMEOUT"),
    )
