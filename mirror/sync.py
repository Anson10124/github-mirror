"""One sync run: GitHub -> GitLab, one repository at a time."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .github import GitHubClient, Repo, filter_repos
from .gitlab import GitLabClient, Project, mirror_description
from .gitops import Creds, Git, GitError
from .logs import log
from .names import gitlab_path

HEADS = "refs/heads/"

# Result statuses
CREATED = "created"            # project created and pushed
SYNCED = "synced"              # existing project updated
UNCHANGED = "unchanged"        # nothing to do
EMPTY = "empty"                # GitHub repo has no branches; nothing to push
SKIPPED_FOREIGN = "skipped_foreign"
FAILED = "failed"
WOULD_CREATE = "would_create"  # dry run
WOULD_SYNC = "would_sync"      # dry run


@dataclass
class RepoResult:
    repo: str
    gitlab_path: str
    status: str
    detail: str = ""


@dataclass
class Summary:
    results: list[RepoResult] = field(default_factory=list)
    filtered: int = 0
    disappeared: list[str] = field(default_factory=list)
    duration: float = 0.0
    dry_run: bool = False

    @property
    def counts(self) -> Counter:
        return Counter(r.status for r in self.results)

    @property
    def failed(self) -> int:
        return self.counts[FAILED]

    def line(self) -> str:
        c = self.counts
        keys = [WOULD_CREATE, WOULD_SYNC] if self.dry_run else [CREATED, SYNCED]
        keys += [UNCHANGED, EMPTY]
        parts = [f"{k}={c[k]}" for k in keys]
        parts += [f"skipped_filtered={self.filtered}", f"skipped_foreign={c[SKIPPED_FOREIGN]}",
                  f"failed={c[FAILED]}", f"disappeared={len(self.disappeared)}"]
        prefix = "DRY RUN finished" if self.dry_run else "Run finished"
        return f"{prefix} in {self.duration:.1f}s: " + " ".join(parts)


def _branch(ref: str) -> str:
    return ref[len(HEADS):]


class Syncer:
    def __init__(self, cfg: Config, github: GitHubClient, gitlab: GitLabClient, git: Git | None = None):
        self.cfg = cfg
        self.github = github
        self.gitlab = gitlab
        self.git = git or Git(cfg.git_timeout)
        self.gh_creds = Creds("x-access-token", cfg.github_token)
        self.gl_creds = Creds("oauth2", cfg.gitlab_token)
        self._namespace_id: int | None = None

    # ------------------------------------------------------------------

    def run(self) -> Summary:
        start = time.monotonic()
        summary = Summary(dry_run=self.cfg.dry_run)
        if self.cfg.dry_run:
            log.info("DRY RUN: no GitLab projects will be created or changed and nothing will be pushed")

        all_repos = self.github.list_repos()
        repos, skipped = filter_repos(all_repos, self.cfg)
        summary.filtered = len(skipped)
        log.info("GitHub: %d repositories owned, %d selected, %d filtered out", len(all_repos), len(repos), len(skipped))
        for r, reason in skipped:
            log.info("skip %s: %s", r.name, reason)

        self._namespace_id = self.gitlab.namespace_id(self.cfg.gitlab_namespace)

        # Detect name collisions after sanitising (GitLab paths are case-insensitive).
        seen: dict[str, str] = {}
        for repo in repos:
            path = gitlab_path(repo.name)
            if path != repo.name:
                log.info("name mapping: %s -> %s", repo.name, path)
            key = path.lower()
            if key in seen:
                msg = f"GitLab path {path!r} collides with repo {seen[key]!r}; add one of them to EXCLUDE_REPOS"
                log.error("%s: %s", repo.name, msg)
                summary.results.append(RepoResult(repo.name, path, FAILED, msg))
                continue
            seen[key] = repo.name
            summary.results.append(self._sync_one(repo, path))

        summary.disappeared = self._disappeared({r.name for r in all_repos})
        for name in summary.disappeared:
            log.warning("%s: no longer on GitHub (local copy %s kept; GitLab project NOT deleted)",
                        name, self.cfg.repos_dir / f"{name}.git")

        summary.duration = time.monotonic() - start
        for r in summary.results:
            if r.status == SKIPPED_FOREIGN:
                log.warning("skipped_foreign %s -> %s: %s", r.repo, r.gitlab_path, r.detail)
            elif r.status == FAILED:
                log.error("failed %s -> %s: %s", r.repo, r.gitlab_path, r.detail)
        (log.warning if summary.failed else log.info)("%s", summary.line())
        return summary

    def _disappeared(self, github_names: set[str]) -> list[str]:
        d = self.cfg.repos_dir
        if not d.is_dir():
            return []
        return sorted(p.name[:-4] for p in d.iterdir() if p.name.endswith(".git") and p.name[:-4] not in github_names)

    def _sync_one(self, repo: Repo, path: str) -> RepoResult:
        try:
            status, detail = self._sync_repo(repo, path)
            return RepoResult(repo.name, path, status, detail)
        except Exception as e:  # one failing repo must not stop the others
            log.error("%s: %s", repo.name, e)
            return RepoResult(repo.name, path, FAILED, str(e))

    # ------------------------------------------------------------------

    def _sync_repo(self, repo: Repo, path: str) -> tuple[str, str]:
        cfg = self.cfg
        full_path = f"{cfg.gitlab_namespace}/{path}"
        clone = cfg.repos_dir / f"{repo.name}.git"

        # 1. Source refs: fetch into the local bare mirror (dry run: just ask GitHub, write nothing).
        if cfg.dry_run:
            local = self.git.ls_remote(repo.clone_url, self.gh_creds)
        else:
            if self.git.ensure_clone(clone, repo.clone_url):
                log.info("%s: initial clone into %s", repo.name, clone)
            self.git.fetch(clone, self.gh_creds)
            if cfg.enable_lfs:
                self.git.lfs_fetch(clone, self.gh_creds)
            local = self.git.local_refs(clone)
            if repo.default_branch and HEADS + repo.default_branch in local:
                self.git.set_head(clone, repo.default_branch)
        has_heads = any(r.startswith(HEADS) for r in local)
        desired_default = repo.default_branch if repo.default_branch and HEADS + repo.default_branch in local else None

        # 2. Target project: find, create, or refuse if it is not ours.
        project = self.gitlab.get_project(full_path)
        created = False
        if project is None:
            if cfg.dry_run:
                n_heads = sum(r.startswith(HEADS) for r in local)
                log.info("[dry-run] %s: would create GitLab project %s (%s) and push %d branches, %d tags%s",
                         repo.name, full_path, cfg.gitlab_visibility, n_heads, len(local) - n_heads,
                         f", default branch {desired_default}" if desired_default else "")
                return WOULD_CREATE, ""
            assert self._namespace_id is not None
            project = self.gitlab.create_project(path, self._namespace_id, cfg.gitlab_visibility,
                                                 mirror_description(repo.html_url))
            created = True
            log.info("%s: created GitLab project %s (%s)", repo.name, project.path_with_namespace, cfg.gitlab_visibility)
        elif not project.is_ours:
            if not cfg.adopt_existing:
                return SKIPPED_FOREIGN, (f"{full_path} exists but has no 'github-mirror' topic or description "
                                         "marker; not touching it (set ADOPT_EXISTING=true to take it over)")
            if cfg.dry_run:
                log.info("[dry-run] %s: would adopt existing project %s (add 'github-mirror' topic)", repo.name, full_path)
            else:
                project = self.gitlab.add_marker_topic(project)
                log.warning("%s: adopted existing project %s (ADOPT_EXISTING=true); added 'github-mirror' topic",
                            repo.name, full_path)
        elif not project.has_topic and not cfg.dry_run:
            project = self.gitlab.add_marker_topic(project)
            log.info("%s: re-added 'github-mirror' topic on %s", repo.name, full_path)

        if not has_heads:
            log.info("%s: GitHub repository is empty; nothing to push", repo.name)
            return EMPTY, ("project created" if created else "")

        # 3. Work out what differs.
        remote = {} if created else self.git.ls_remote(project.http_url_to_repo, self.gl_creds)
        to_update = sorted(r for r, sha in local.items() if remote.get(r) != sha)
        to_delete = sorted(r for r in remote if r not in local)
        default_ok = desired_default is None or project.default_branch == desired_default

        if not to_update and not to_delete and default_ok:
            log.info("%s: up to date", repo.name)
            return UNCHANGED, ""

        if cfg.dry_run:
            log.info("[dry-run] %s: would push %d ref(s) %s, delete %d ref(s) %s%s", repo.name,
                     len(to_update), _preview(to_update), len(to_delete), _preview(to_delete),
                     f", set default branch {project.default_branch} -> {desired_default}" if not default_ok else "")
            return WOULD_SYNC, ""

        url = project.http_url_to_repo
        if cfg.enable_lfs:
            self.git.lfs_push(clone, url, self.gl_creds)

        # 4a. Push new/updated refs WITHOUT prune (so a renamed default branch exists before we switch to it).
        if to_update:
            self._push(repo, project, clone, prune=False)

        # 4b. Make GitLab's default branch match GitHub's and allow force push on it.
        if created or not default_ok:
            project = self.gitlab.refresh(project)
            previous = project.default_branch
            if desired_default and previous != desired_default:
                log.info("%s: default branch %s -> %s", repo.name, previous, desired_default)
                self.gitlab.set_default_branch(project, desired_default)
                project.default_branch = desired_default
            # GitLab protects the branch that was default at first push; allow force push on it and the new default.
            for b in dict.fromkeys(x for x in (desired_default, previous) if x):
                if HEADS + b in local and self.gitlab.allow_force_push(project, b):
                    log.info("%s: allowed force push on protected branch %s", repo.name, b)

        # 4c. Push again WITH prune to remove branches/tags deleted on GitHub.
        if to_delete:
            for ref in to_delete:
                if ref.startswith(HEADS) and self.gitlab.unprotect_branch(project, _branch(ref)):
                    log.info("%s: removed protection from %s so its deletion can be mirrored", repo.name, _branch(ref))
            self._push(repo, project, clone, prune=True)

        detail = f"{len(to_update)} updated, {len(to_delete)} deleted"
        log.info("%s: %s %s (%s)", repo.name, "created and pushed" if created else "synced", project.path_with_namespace, detail)
        return (CREATED if created else SYNCED), detail

    def _push(self, repo: Repo, project: Project, clone: Path, *, prune: bool) -> None:
        url = project.http_url_to_repo
        try:
            self.git.push(clone, url, self.gl_creds, prune=prune, push_options=self.gitlab.push_options)
        except GitError as e:
            if not e.is_protected_rejection:
                raise
            branches = set(e.rejected_branches())
            if project.default_branch:
                branches.add(project.default_branch)
            changed = [b for b in sorted(branches) if self.gitlab.allow_force_push(project, b)]
            if not changed:
                raise
            log.info("%s: push rejected by branch protection; allowed force push on %s and retrying",
                     repo.name, ", ".join(changed))
            self.git.push(clone, url, self.gl_creds, prune=prune, push_options=self.gitlab.push_options)


def _preview(refs: list[str], n: int = 5) -> str:
    if not refs:
        return ""
    shown = ", ".join(refs[:n])
    return f"[{shown}{', ...' if len(refs) > n else ''}]"
