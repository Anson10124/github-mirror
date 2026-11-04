"""Git operations on the local bare mirror clones."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logs import redact

ASKPASS = str(Path(__file__).with_name("askpass.sh"))
REFSPECS = ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]

# Messages GitLab prints when a push hits branch protection or the default branch.
_PROTECTED_RE = re.compile(r"protected branch|default branch|not allowed to (force )?push|pre-receive hook declined",
                           re.IGNORECASE)


@dataclass(frozen=True)
class Creds:
    username: str
    password: str

    def env(self) -> dict[str, str]:
        return {"MIRROR_GIT_USERNAME": self.username, "MIRROR_GIT_PASSWORD": self.password}


class GitError(Exception):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        self.args_ = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        tail = redact((stderr or stdout).strip())[-800:]
        super().__init__(f"git {' '.join(args)} failed (exit {returncode}): {tail}")

    @property
    def is_protected_rejection(self) -> bool:
        return bool(_PROTECTED_RE.search(self.stderr + "\n" + self.stdout))

    def rejected_branches(self) -> list[str]:
        """Branch names rejected in `git push --porcelain` output (lines starting with '!')."""
        out = []
        for line in self.stdout.splitlines():
            if line.startswith("!"):
                parts = line.split("\t")
                if len(parts) >= 2:
                    dst = parts[1].split(":")[-1]
                    if dst.startswith("refs/heads/"):
                        out.append(dst[len("refs/heads/"):])
        return out


def _base_env(creds: Creds | None) -> dict[str, str]:
    env = dict(os.environ)
    # Make sure nothing in the environment can leak a token into a child process
    # other than through the askpass variables below.
    for k in ("GITHUB_TOKEN", "GITLAB_TOKEN", "MIRROR_GIT_USERNAME", "MIRROR_GIT_PASSWORD"):
        env.pop(k, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ASKPASS
    env["GCM_INTERACTIVE"] = "never"
    if creds:
        env.update(creds.env())
    return env


class Git:
    def __init__(self, timeout: int = 3600):
        self.timeout = timeout

    def run(self, args: list[str], *, cwd: Path | None = None, creds: Creds | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        # -c credential.helper= : never consult or store credentials in a helper.
        full = ["git", "-c", "credential.helper=", *args]
        try:
            proc = subprocess.run(full, cwd=cwd, env=_base_env(creds), capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise GitError(args, -1, "", f"timed out after {self.timeout}s") from e
        if check and proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stdout, proc.stderr)
        return proc

    # ---- local mirror -------------------------------------------------

    def ensure_clone(self, path: Path, url: str) -> bool:
        """Create the bare repo if needed and (re)write its remote config. Returns True if created."""
        created = False
        if not (path / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["init", "--bare", "--quiet", str(path)])
            created = True
        # Re-applied every run so the config self-heals. The URL never contains credentials.
        self.run(["config", "remote.origin.url", url], cwd=path)
        self.run(["config", "--unset-all", "remote.origin.fetch"], cwd=path, check=False)
        for spec in REFSPECS:
            self.run(["config", "--add", "remote.origin.fetch", spec], cwd=path)
        self.run(["config", "remote.origin.tagOpt", "--no-tags"], cwd=path)
        self.run(["config", "gc.autoDetach", "false"], cwd=path)
        return created

    def fetch(self, path: Path, creds: Creds | None) -> None:
        # Only refs/heads/* and refs/tags/* (from remote.origin.fetch); refs/pull/* is never fetched.
        self.run(["fetch", "--prune", "--quiet", "origin"], cwd=path, creds=creds)

    def local_refs(self, path: Path) -> dict[str, str]:
        out = self.run(["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"], cwd=path)
        return _parse_ref_lines(out.stdout)

    def set_head(self, path: Path, branch: str) -> None:
        self.run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=path)

    # ---- remotes ------------------------------------------------------

    def ls_remote(self, url: str, creds: Creds | None) -> dict[str, str]:
        out = self.run(["ls-remote", "--heads", "--tags", "--refs", url], creds=creds)
        refs = {}
        for line in out.stdout.splitlines():
            if "\t" in line:
                sha, ref = line.split("\t", 1)
                refs[ref.strip()] = sha.strip()
        return refs

    def push(self, path: Path, url: str, creds: Creds | None, *, prune: bool,
             push_options: list[str] | None = None) -> str:
        args = ["push", "--porcelain"]
        if prune:
            args.append("--prune")
        for opt in push_options or []:
            args += ["-o", opt]
        args += [url, *REFSPECS]
        return self.run(args, cwd=path, creds=creds).stdout

    # ---- LFS ----------------------------------------------------------

    def lfs_fetch(self, path: Path, creds: Creds | None) -> None:
        self.run(["lfs", "fetch", "--all", "origin"], cwd=path, creds=creds)

    def lfs_push(self, path: Path, url: str, creds: Creds | None) -> None:
        self.run(["lfs", "push", "--all", url], cwd=path, creds=creds)


def _parse_ref_lines(text: str) -> dict[str, str]:
    refs = {}
    for line in text.splitlines():
        if " " in line:
            sha, ref = line.split(" ", 1)
            refs[ref.strip()] = sha.strip()
    return refs
