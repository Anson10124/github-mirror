"""GitLab side: projects, ownership marker, default branch and protected branches."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .config import Config
from .http import HttpClient, HttpError
from .logs import log

MARKER_TOPIC = "github-mirror"
MARKER_DESC = "[github-mirror]"


def enc(value: str) -> str:
    return urllib.parse.quote(value, safe="")


@dataclass
class Project:
    id: int
    path_with_namespace: str
    http_url_to_repo: str
    default_branch: str | None
    description: str = ""
    topics: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> "Project":
        topics = d.get("topics")
        if topics is None:
            topics = d.get("tag_list") or []
        return cls(
            id=d["id"],
            path_with_namespace=d["path_with_namespace"],
            http_url_to_repo=d["http_url_to_repo"],
            default_branch=d.get("default_branch"),
            description=d.get("description") or "",
            topics=list(topics),
        )

    @property
    def has_topic(self) -> bool:
        return MARKER_TOPIC in self.topics

    @property
    def has_desc_marker(self) -> bool:
        return MARKER_DESC in self.description

    @property
    def is_ours(self) -> bool:
        """A project is ours if it carries the topic OR the description marker."""
        return self.has_topic or self.has_desc_marker


def mirror_description(html_url: str) -> str:
    return f"{MARKER_DESC} Read-only mirror of {html_url}. Managed automatically; changes made here are overwritten."


class GitLabClient:
    # '-o ci.skip' makes sure no pipeline runs for mirrored pushes.
    push_options: list[str] = ["ci.skip"]

    def __init__(self, cfg: Config, http: HttpClient | None = None):
        self.http = http or HttpClient(cfg.gitlab_url + "/api/v4", {"PRIVATE-TOKEN": cfg.gitlab_token}, name="gitlab")

    def namespace_id(self, namespace: str) -> int:
        resp = self.http.get(f"/namespaces/{enc(namespace)}", allow_404=True)
        if resp is None:
            raise RuntimeError(f"GitLab namespace {namespace!r} not found (check GITLAB_NAMESPACE and token scope)")
        return int(resp.data["id"])

    def get_project(self, full_path: str) -> Project | None:
        resp = self.http.get(f"/projects/{enc(full_path)}", allow_404=True)
        if resp is None:
            return None
        project = Project.from_api(resp.data)
        if project.path_with_namespace.lower() != full_path.lower():
            # GitLab answers old paths of renamed projects via redirects; that is not our project path.
            log.info("GitLab path %s redirects to %s; treating %s as missing",
                     full_path, project.path_with_namespace, full_path)
            return None
        return project

    def create_project(self, path: str, namespace_id: int, visibility: str, description: str) -> Project:
        resp = self.http.request("POST", "/projects", json_body={
            "name": path,
            "path": path,
            "namespace_id": namespace_id,
            "visibility": visibility,
            "description": description,
            "topics": [MARKER_TOPIC],
            "initialize_with_readme": False,
            "builds_access_level": "disabled",
            "auto_devops_enabled": False,
        })
        assert resp is not None
        return Project.from_api(resp.data)

    def add_marker_topic(self, project: Project) -> Project:
        topics = sorted(set(project.topics) | {MARKER_TOPIC})
        resp = self.http.request("PUT", f"/projects/{project.id}", json_body={"topics": topics})
        assert resp is not None
        return Project.from_api(resp.data)

    def refresh(self, project: Project) -> Project:
        resp = self.http.get(f"/projects/{project.id}")
        assert resp is not None
        return Project.from_api(resp.data)

    def set_default_branch(self, project: Project, branch: str) -> None:
        self.http.request("PUT", f"/projects/{project.id}", json_body={"default_branch": branch})

    def get_protected_branch(self, project: Project, branch: str) -> dict | None:
        resp = self.http.get(f"/projects/{project.id}/protected_branches/{enc(branch)}", allow_404=True)
        return None if resp is None else resp.data

    def allow_force_push(self, project: Project, branch: str) -> bool:
        """Allow force pushes on `branch` if it is protected. Returns True if something changed."""
        pb = self.get_protected_branch(project, branch)
        if pb is None or pb.get("allow_force_push"):
            return False
        path = f"/projects/{project.id}/protected_branches/{enc(branch)}"
        try:
            self.http.request("PATCH", path, json_body={"allow_force_push": True})
        except HttpError as e:
            if e.status not in (404, 405):
                raise
            # Older GitLab without PATCH: re-create the protection with force push allowed.
            self.http.request("DELETE", path)
            self.http.request("POST", f"/projects/{project.id}/protected_branches", json_body={
                "name": branch, "allow_force_push": True,
                "push_access_level": 40, "merge_access_level": 40,
            })
        return True

    def unprotect_branch(self, project: Project, branch: str) -> bool:
        """Remove protection from `branch` (needed before a mirrored deletion). True if it was protected."""
        if self.get_protected_branch(project, branch) is None:
            return False
        self.http.request("DELETE", f"/projects/{project.id}/protected_branches/{enc(branch)}")
        return True
