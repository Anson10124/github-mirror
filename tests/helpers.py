"""Test fixtures: local 'GitHub' repos and a fake GitLab backed by real bare repos.

The fake GitLab's server-side hooks emulate the GitLab behaviours the mirror
has to cope with:
  * the default branch (HEAD) cannot be deleted,
  * protected branches cannot be deleted by push,
  * protected branches reject non-fast-forward pushes unless allow_force_push,
  * the first branch pushed to a new project becomes the default and is protected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from mirror.config import Config
from mirror.github import Repo
from mirror.gitlab import MARKER_DESC, MARKER_TOPIC, Project

# Hermetic git for tests: no user/system config, fixed identity.
os.environ.update({
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
})

ZERO = "0" * 40


def git(*args: str, cwd: Path | str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_config(tmp: Path, **kw) -> Config:
    base = dict(github_token="fake-github-TESTTOKEN123", gitlab_token="fake-gitlab-TESTTOKEN456",
                gitlab_namespace="Anson10124", data_dir=tmp / "data")
    base.update(kw)
    return Config(**base)


# ---------------------------------------------------------------- GitHub

class FakeGitHubRepo:
    """A bare 'GitHub' repo plus a work tree used to create commits."""

    def __init__(self, root: Path, name: str, *, fork=False, archived=False):
        self.name = name
        self.bare = root / "github" / f"{name}.git"
        self.work = root / "github-work" / name
        self.fork = fork
        self.archived = archived
        self.bare.parent.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "--quiet", str(self.bare))
        self.work.mkdir(parents=True)
        git("init", "--quiet", str(self.work))
        git("remote", "add", "origin", str(self.bare), cwd=self.work)
        self._n = 0

    def commit(self, branch: str, msg: str | None = None) -> str:
        self._n += 1
        if self._has_branch(branch):
            git("checkout", "--quiet", branch, cwd=self.work)
        else:
            self._new_branch(branch)
        (self.work / "f.txt").write_text(f"{branch} {self._n}\n")
        git("add", "f.txt", cwd=self.work)
        git("commit", "--quiet", "-m", msg or f"{branch} commit {self._n}", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def _has_branch(self, branch: str) -> bool:
        return subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                              cwd=self.work, capture_output=True).returncode == 0

    def _new_branch(self, branch: str) -> None:
        has_commits = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=self.work,
                                     capture_output=True).returncode == 0
        if has_commits:
            git("checkout", "--quiet", "-b", branch, cwd=self.work)
        else:
            git("checkout", "--quiet", "--orphan", branch, cwd=self.work)

    def push(self, *refspecs: str, force: bool = False) -> None:
        git("push", "--quiet", *(["--force"] if force else []), "origin", *refspecs, cwd=self.work)

    def tag(self, name: str, branch: str, annotated: bool = False) -> None:
        if annotated:
            git("tag", "-a", name, "-m", f"tag {name}", branch, cwd=self.work)
        else:
            git("tag", name, branch, cwd=self.work)
        git("push", "--quiet", "origin", f"refs/tags/{name}", cwd=self.work)

    def set_default(self, branch: str) -> None:
        git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=self.bare)

    @property
    def default_branch(self) -> str | None:
        ref = git("symbolic-ref", "HEAD", cwd=self.bare)
        return ref[len("refs/heads/"):]

    def add_pull_ref(self, n: int, branch: str) -> None:
        sha = git("rev-parse", f"refs/heads/{branch}", cwd=self.bare)
        git("update-ref", f"refs/pull/{n}/head", sha, cwd=self.bare)

    def refs(self) -> dict[str, str]:
        return refs_of(self.bare)

    def to_repo(self) -> Repo:
        return Repo(name=self.name, full_name=f"Anson10124/{self.name}", clone_url=str(self.bare),
                    html_url=f"https://github.com/Anson10124/{self.name}", default_branch=self.default_branch,
                    fork=self.fork, archived=self.archived)


class FakeGitHub:
    def __init__(self):
        self.repos: dict[str, FakeGitHubRepo] = {}
        self.extra: list[Repo] = []  # repos whose clone URL is broken etc.

    def add(self, r: FakeGitHubRepo) -> FakeGitHubRepo:
        self.repos[r.name] = r
        return r

    def list_repos(self) -> list[Repo]:
        return [r.to_repo() for r in self.repos.values()] + list(self.extra)


def refs_of(bare: Path) -> dict[str, str]:
    out = git("for-each-ref", "--format=%(objectname) %(refname)", cwd=bare)
    return {ref: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines() if line)}


# ---------------------------------------------------------------- GitLab

PRE_RECEIVE = textwrap.dedent("""\
    #!{python}
    import json, subprocess, sys
    Z = "0" * 40
    meta = json.load(open("meta.json"))
    head = subprocess.run(["git", "symbolic-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    errors = []
    for line in sys.stdin:
        old, new, ref = line.split()
        if not ref.startswith("refs/heads/"):
            continue
        b = ref[len("refs/heads/"):]
        prot = meta["protected"].get(b)
        if new == Z:
            if ref == head:
                errors.append("GitLab: The default branch of a project cannot be deleted.")
            elif prot:
                errors.append("GitLab: You can only delete protected branches using the web interface.")
        elif old != Z and prot and not prot["allow_force_push"]:
            if subprocess.run(["git", "merge-base", "--is-ancestor", old, new]).returncode != 0:
                errors.append("GitLab: You are not allowed to force push code to a protected branch on this project.")
    meta["pushes"] = meta.get("pushes", 0) + 1
    json.dump(meta, open("meta.json", "w"))
    if errors:
        print("\\n".join(errors), file=sys.stderr)
        sys.exit(1)
""")

POST_RECEIVE = textwrap.dedent("""\
    #!{python}
    import json, subprocess, sys
    Z = "0" * 40
    meta = json.load(open("meta.json"))
    heads = [ref for old, new, ref in (l.split() for l in sys.stdin) if new != Z and ref.startswith("refs/heads/")]
    if not meta["initialized"] and heads:
        branch = "refs/heads/main" if "refs/heads/main" in heads else heads[0]
        subprocess.run(["git", "symbolic-ref", "HEAD", branch], check=True)
        meta["protected"][branch[len("refs/heads/"):]] = {{"allow_force_push": False}}
        meta["initialized"] = True
        json.dump(meta, open("meta.json", "w"))
""")


class FakeGitLab:
    push_options = ["ci.skip"]

    def __init__(self, root: Path):
        self.root = root / "gitlab"
        self.root.mkdir(parents=True)
        self.projects: dict[str, dict] = {}   # lower-case full path -> record
        self.calls: list[tuple] = []          # write calls only
        self._next_id = 1

    # -- helpers for tests
    def bare(self, full_path: str) -> Path:
        return Path(self.projects[full_path.lower()]["bare"])

    def meta(self, full_path: str) -> dict:
        return json.loads((self.bare(full_path) / "meta.json").read_text())

    def _write_meta(self, rec: dict, meta: dict) -> None:
        (Path(rec["bare"]) / "meta.json").write_text(json.dumps(meta))

    def refs(self, full_path: str) -> dict[str, str]:
        return refs_of(self.bare(full_path))

    def _new(self, full_path: str, description: str, topics: list[str]) -> dict:
        bare = self.root / f"{full_path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "--quiet", "--initial-branch=main", str(bare))
        git("config", "receive.advertisePushOptions", "true", cwd=bare)
        for name, body in (("pre-receive", PRE_RECEIVE), ("post-receive", POST_RECEIVE)):
            hook = bare / "hooks" / name
            hook.write_text(body.format(python=sys.executable))
            hook.chmod(0o755)
        (bare / "meta.json").write_text(json.dumps({"protected": {}, "initialized": False}))
        rec = {"id": self._next_id, "path_with_namespace": full_path, "bare": str(bare),
               "description": description, "topics": list(topics)}
        self._next_id += 1
        self.projects[full_path.lower()] = rec
        return rec

    def create_foreign(self, full_path: str, description: str = "my own project") -> Path:
        """A pre-existing project NOT created by the mirror, with one commit on main."""
        rec = self._new(full_path, description, [])
        work = Path(tempfile.mkdtemp(dir=self.root))
        git("init", "--quiet", "--initial-branch=main", str(work))
        (work / "own.txt").write_text("hand-made\n")
        git("add", ".", cwd=work)
        git("commit", "--quiet", "-m", "own work", cwd=work)
        git("push", "--quiet", rec["bare"], "main", cwd=work)
        return Path(rec["bare"])

    def _project(self, rec: dict) -> Project:
        bare = Path(rec["bare"])
        head = git("symbolic-ref", "HEAD", cwd=bare)[len("refs/heads/"):]
        has_head = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{head}"],
                                  cwd=bare, capture_output=True).returncode == 0
        return Project(id=rec["id"], path_with_namespace=rec["path_with_namespace"], http_url_to_repo=str(bare),
                       default_branch=head if has_head else None, description=rec["description"],
                       topics=list(rec["topics"]))

    def _rec(self, project: Project) -> dict:
        return self.projects[project.path_with_namespace.lower()]

    # -- GitLabClient interface
    def namespace_id(self, namespace: str) -> int:
        return 42

    def get_project(self, full_path: str) -> Project | None:
        rec = self.projects.get(full_path.lower())
        return None if rec is None else self._project(rec)

    def create_project(self, path, namespace_id, visibility, description) -> Project:
        self.calls.append(("create_project", path, visibility))
        return self._project(self._new(f"Anson10124/{path}", description, [MARKER_TOPIC]))

    def add_marker_topic(self, project: Project) -> Project:
        self.calls.append(("add_marker_topic", project.path_with_namespace))
        rec = self._rec(project)
        rec["topics"] = sorted(set(rec["topics"]) | {MARKER_TOPIC})
        return self._project(rec)

    def refresh(self, project: Project) -> Project:
        return self._project(self._rec(project))

    def set_default_branch(self, project: Project, branch: str) -> None:
        self.calls.append(("set_default_branch", project.path_with_namespace, branch))
        git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=self._rec(project)["bare"])

    def get_protected_branch(self, project: Project, branch: str):
        return json.loads((Path(self._rec(project)["bare"]) / "meta.json").read_text())["protected"].get(branch)

    def allow_force_push(self, project: Project, branch: str) -> bool:
        rec = self._rec(project)
        meta = json.loads((Path(rec["bare"]) / "meta.json").read_text())
        pb = meta["protected"].get(branch)
        if pb is None or pb["allow_force_push"]:
            return False
        self.calls.append(("allow_force_push", project.path_with_namespace, branch))
        pb["allow_force_push"] = True
        self._write_meta(rec, meta)
        return True

    def unprotect_branch(self, project: Project, branch: str) -> bool:
        rec = self._rec(project)
        meta = json.loads((Path(rec["bare"]) / "meta.json").read_text())
        if branch not in meta["protected"]:
            return False
        self.calls.append(("unprotect_branch", project.path_with_namespace, branch))
        del meta["protected"][branch]
        self._write_meta(rec, meta)
        return True

    # Test-only knobs
    def protect(self, full_path: str, branch: str, allow_force_push: bool = False) -> None:
        rec = self.projects[full_path.lower()]
        meta = json.loads((Path(rec["bare"]) / "meta.json").read_text())
        meta["protected"][branch] = {"allow_force_push": allow_force_push}
        self._write_meta(rec, meta)

    def set_description(self, full_path: str, description: str, topics: list[str]) -> None:
        rec = self.projects[full_path.lower()]
        rec["description"] = description
        rec["topics"] = topics


__all__ = ["FakeGitHub", "FakeGitHubRepo", "FakeGitLab", "make_config", "git", "refs_of", "ZERO", "MARKER_DESC"]
