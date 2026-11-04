"""GitHub side: list the user's own repositories and decide which to mirror."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Iterable

from .config import Config
from .http import HttpClient


@dataclass
class Repo:
    name: str
    full_name: str
    clone_url: str
    html_url: str
    default_branch: str | None
    private: bool = True
    fork: bool = False
    archived: bool = False
    disabled: bool = False

    @classmethod
    def from_api(cls, d: dict) -> "Repo":
        return cls(
            name=d["name"],
            full_name=d["full_name"],
            clone_url=d["clone_url"],
            html_url=d["html_url"],
            default_branch=d.get("default_branch"),
            private=bool(d.get("private")),
            fork=bool(d.get("fork")),
            archived=bool(d.get("archived")),
            disabled=bool(d.get("disabled")),
        )


class GitHubClient:
    def __init__(self, cfg: Config, http: HttpClient | None = None):
        self.http = http or HttpClient(
            cfg.github_api_url,
            {
                "Authorization": f"Bearer {cfg.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            name="github",
        )

    def list_repos(self) -> list[Repo]:
        items = self.http.paginate(
            "/user/repos",
            {"affiliation": "owner", "visibility": "all", "per_page": 100, "sort": "full_name"},
        )
        return [Repo.from_api(d) for d in items]


def is_excluded(name: str, patterns: Iterable[str]) -> bool:
    lname = name.lower()
    return any(fnmatch.fnmatchcase(lname, p) for p in patterns)


def filter_repos(repos: list[Repo], cfg: Config) -> tuple[list[Repo], list[tuple[Repo, str]]]:
    """Split repos into (included, [(skipped, reason)])."""
    included: list[Repo] = []
    skipped: list[tuple[Repo, str]] = []
    for r in repos:
        if is_excluded(r.name, cfg.exclude_repos):
            skipped.append((r, "excluded by EXCLUDE_REPOS"))
        elif r.fork and not cfg.include_forks:
            skipped.append((r, "fork (INCLUDE_FORKS=false)"))
        elif r.archived and not cfg.include_archived:
            skipped.append((r, "archived (INCLUDE_ARCHIVED=false)"))
        elif r.disabled:
            skipped.append((r, "disabled by GitHub"))
        else:
            included.append(r)
    return included, skipped
