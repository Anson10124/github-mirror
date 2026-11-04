"""End-to-end sync tests with real git, local 'GitHub' repos and a fake GitLab."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import MARKER_DESC, FakeGitHub, FakeGitHubRepo, FakeGitLab, git, make_config

from mirror import sync as S
from mirror.github import Repo
from mirror.gitops import REFSPECS
from mirror.logs import clear_secrets, register_secret, setup_logging

NS = "Anson10124"


def heads_and_tags(refs: dict[str, str]) -> dict[str, str]:
    return {r: s for r, s in refs.items() if r.startswith(("refs/heads/", "refs/tags/"))}


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mirror-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.gh = FakeGitHub()
        self.gl = FakeGitLab(self.tmp)
        self.cfg = make_config(self.tmp)
        setup_logging(logging.CRITICAL)  # keep test output quiet

    def repo(self, name: str, **kw) -> FakeGitHubRepo:
        return self.gh.add(FakeGitHubRepo(self.tmp, name, **kw))

    def run_sync(self, **cfg_overrides) -> S.Summary:
        for k, v in cfg_overrides.items():
            setattr(self.cfg, k, v)
        return S.Syncer(self.cfg, self.gh, self.gl).run()

    def status(self, summary: S.Summary, name: str) -> str:
        return next(r.status for r in summary.results if r.repo == name)

    def assertMirrored(self, r: FakeGitHubRepo, gitlab_path: str | None = None):
        path = f"{NS}/{gitlab_path or r.name}"
        self.assertEqual(heads_and_tags(r.refs()), self.gl.refs(path))
        self.assertEqual(self.gl.get_project(path).default_branch, r.default_branch)


class TestInitialAndNoop(SyncTestBase):
    def setUp(self):
        super().setUp()
        self.a = self.repo("alpha")
        self.a.commit("main")
        self.a.commit("develop")
        self.a.push("main", "develop")
        self.a.tag("v1.0", "main", annotated=True)
        self.a.tag("light", "develop")
        self.a.set_default("develop")          # non-'main' default must be carried over
        self.a.add_pull_ref(1, "develop")      # must NOT be mirrored

        self.b = self.repo("beta")
        self.b.commit("master")
        self.b.push("master")
        self.b.set_default("master")

    def test_initial_sync_creates_and_pushes(self):
        s = self.run_sync()
        self.assertEqual(self.status(s, "alpha"), S.CREATED)
        self.assertEqual(self.status(s, "beta"), S.CREATED)
        self.assertMirrored(self.a)
        self.assertMirrored(self.b)
        # refs/pull/* exists on GitHub but not on GitLab, nor in the local mirror.
        self.assertIn("refs/pull/1/head", self.a.refs())
        self.assertNotIn("refs/pull/1/head", self.gl.refs(f"{NS}/alpha"))
        local = git("for-each-ref", "--format=%(refname)", cwd=self.cfg.repos_dir / "alpha.git")
        self.assertNotIn("refs/pull", local)
        # GitLab protected the first pushed branch (main); after switching the default to develop,
        # no protected branch may still block force pushes.
        prot = self.gl.meta(f"{NS}/alpha")["protected"]
        self.assertIn("main", prot)
        self.assertTrue(all(p["allow_force_push"] for p in prot.values()), prot)
        self.assertTrue(self.gl.meta(f"{NS}/beta")["protected"]["master"]["allow_force_push"])
        # Created project carries the ownership markers.
        p = self.gl.get_project(f"{NS}/alpha")
        self.assertTrue(p.has_topic and p.has_desc_marker)
        self.assertEqual(s.failed, 0)

    def test_second_run_is_noop(self):
        self.run_sync()
        calls_before = list(self.gl.calls)
        pushes_before = self.gl.meta(f"{NS}/alpha")["pushes"]
        s = self.run_sync()
        self.assertEqual(self.status(s, "alpha"), S.UNCHANGED)
        self.assertEqual(self.status(s, "beta"), S.UNCHANGED)
        self.assertEqual(self.gl.calls, calls_before, "second run must not write to GitLab")
        self.assertEqual(self.gl.meta(f"{NS}/alpha")["pushes"], pushes_before, "second run must not push")

    def test_local_mirror_has_no_credentials(self):
        self.run_sync()
        config = (self.cfg.repos_dir / "alpha.git" / "config").read_text()
        self.assertNotIn("TESTTOKEN", config)
        self.assertEqual(
            git("config", "--get-all", "remote.origin.fetch", cwd=self.cfg.repos_dir / "alpha.git").splitlines(),
            REFSPECS)


class TestUpdates(SyncTestBase):
    def setUp(self):
        super().setUp()
        self.a = self.repo("alpha")
        self.a.commit("main")
        self.a.commit("feature")
        self.a.push("main", "feature")
        self.a.tag("v1", "main")
        self.a.tag("v2", "main")
        self.a.set_default("main")
        self.run_sync()

    def test_new_commits_branches_tags(self):
        self.a.commit("main")
        self.a.commit("new-branch")
        self.a.push("main", "new-branch")
        self.a.tag("v3", "new-branch", annotated=True)
        s = self.run_sync()
        self.assertEqual(self.status(s, "alpha"), S.SYNCED)
        self.assertMirrored(self.a)

    def test_force_push_on_protected_default_branch(self):
        git("checkout", "--quiet", "main", cwd=self.a.work)
        git("commit", "--quiet", "--amend", "-m", "rewritten history", cwd=self.a.work)
        self.a.push("main", force=True)
        s = self.run_sync()
        self.assertEqual(self.status(s, "alpha"), S.SYNCED, s.results)
        self.assertMirrored(self.a)

    def test_force_push_recovers_when_protection_was_reset(self):
        # Someone re-protected main on GitLab without force push: the push is rejected,
        # the mirror allows force push via the API and retries.
        self.gl.protect(f"{NS}/alpha", "main", allow_force_push=False)
        git("checkout", "--quiet", "main", cwd=self.a.work)
        git("commit", "--quiet", "--amend", "-m", "rewritten again", cwd=self.a.work)
        self.a.push("main", force=True)
        s = self.run_sync()
        self.assertEqual(self.status(s, "alpha"), S.SYNCED, s.results)
        self.assertIn(("allow_force_push", f"{NS}/alpha", "main"), self.gl.calls)
        self.assertMirrored(self.a)

    def test_deleted_branch_and_tag_are_pruned(self):
        self.a.push(":feature")
        self.a.push(":refs/tags/v1")
        s = self.run_sync()
        self.assertEqual(self.status(s, "alpha"), S.SYNCED)
        gl = self.gl.refs(f"{NS}/alpha")
        self.assertNotIn("refs/heads/feature", gl)
        self.assertNotIn("refs/tags/v1", gl)
        self.assertMirrored(self.a)

    def test_moved_tag_is_followed(self):
        self.a.commit("main")
        git("tag", "-f", "v2", "main", cwd=self.a.work)
        self.a.push("+refs/tags/v2")
        self.run_sync()
        self.assertMirrored(self.a)


class TestDefaultBranchRename(SyncTestBase):
    """GitHub default renamed master -> main: GitLab refuses to delete its default branch,
    so the mirror must push without prune, switch the default, then prune."""

    def setUp(self):
        super().setUp()
        self.a = self.repo("legacy")
        self.a.commit("master")
        self.a.commit("topic")
        self.a.push("master", "topic")
        self.a.set_default("master")
        self.run_sync()
        meta = self.gl.meta(f"{NS}/legacy")
        self.assertIn("master", meta["protected"])  # GitLab protected the initial default

        # Rename on GitHub: create main from master, make it default, delete master.
        git("branch", "-m", "master", "main", cwd=self.a.work)
        self.a.push("main")
        self.a.set_default("main")
        self.a.push(":master")

    def test_naive_prune_push_is_rejected(self):
        """Sanity check that the fake GitLab really behaves like GitLab here."""
        clone = self.cfg.repos_dir / "legacy.git"
        git("fetch", "--prune", "--quiet", "origin", cwd=clone)
        url = self.gl.get_project(f"{NS}/legacy").http_url_to_repo
        proc = subprocess.run(["git", "push", "--prune", url, *REFSPECS], cwd=clone, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("default branch", proc.stderr)

    def test_rename_is_mirrored(self):
        s = self.run_sync()
        self.assertEqual(self.status(s, "legacy"), S.SYNCED, s.results)
        p = self.gl.get_project(f"{NS}/legacy")
        self.assertEqual(p.default_branch, "main")
        gl = self.gl.refs(f"{NS}/legacy")
        self.assertIn("refs/heads/main", gl)
        self.assertNotIn("refs/heads/master", gl)
        self.assertMirrored(self.a)
        # Order of GitLab API writes: default switched before the old protected branch was unprotected.
        names = [c[0] for c in self.gl.calls]
        self.assertLess(names.index("set_default_branch"), names.index("unprotect_branch"))
        self.assertIn(("unprotect_branch", f"{NS}/legacy", "master"), self.gl.calls)
        # Second run: nothing left to do.
        self.assertEqual(self.status(self.run_sync(), "legacy"), S.UNCHANGED)


class TestOwnership(SyncTestBase):
    def setUp(self):
        super().setUp()
        self.a = self.repo("shared")
        self.a.commit("main")
        self.a.push("main")
        self.a.set_default("main")

    def test_foreign_project_is_skipped(self):
        foreign = self.gl.create_foreign(f"{NS}/shared")
        before = self.gl.refs(f"{NS}/shared")
        s = self.run_sync()
        self.assertEqual(self.status(s, "shared"), S.SKIPPED_FOREIGN)
        self.assertEqual(self.gl.refs(f"{NS}/shared"), before, "foreign project must not be touched")
        self.assertEqual(s.counts[S.SKIPPED_FOREIGN], 1)
        self.assertIn("skipped_foreign=1", s.line())
        self.assertTrue(foreign.exists())

    def test_adopt_existing(self):
        self.gl.create_foreign(f"{NS}/shared")
        s = self.run_sync(adopt_existing=True)
        self.assertEqual(self.status(s, "shared"), S.SYNCED, s.results)
        self.assertTrue(self.gl.get_project(f"{NS}/shared").has_topic)
        self.assertMirrored(self.a)

    def test_description_marker_alone_is_enough_and_topic_is_restored(self):
        self.run_sync()
        self.gl.set_description(f"{NS}/shared", f"{MARKER_DESC} hand edited", topics=[])
        self.a.commit("main")
        self.a.push("main")
        s = self.run_sync()
        self.assertEqual(self.status(s, "shared"), S.SYNCED)
        self.assertTrue(self.gl.get_project(f"{NS}/shared").has_topic)

    def test_topic_alone_is_enough(self):
        self.run_sync()
        self.gl.set_description(f"{NS}/shared", "I rewrote the description", topics=["github-mirror"])
        self.a.commit("main")
        self.a.push("main")
        self.assertEqual(self.status(self.run_sync(), "shared"), S.SYNCED)
        self.assertMirrored(self.a)


class TestMisc(SyncTestBase):
    def test_failure_does_not_stop_others(self):
        good = self.repo("good")
        good.commit("main")
        good.push("main")
        good.set_default("main")
        self.gh.extra.append(Repo(name="broken", full_name=f"{NS}/broken", clone_url=str(self.tmp / "nope.git"),
                                  html_url="https://github.com/x/broken", default_branch="main"))
        s = self.run_sync()
        self.assertEqual(self.status(s, "broken"), S.FAILED)
        self.assertEqual(self.status(s, "good"), S.CREATED)
        self.assertEqual(s.failed, 1)
        self.assertMirrored(good)

    def test_empty_repo(self):
        self.repo("empty")
        s = self.run_sync()
        self.assertEqual(self.status(s, "empty"), S.EMPTY)
        self.assertIsNotNone(self.gl.get_project(f"{NS}/empty"))

    def test_dry_run_writes_nothing(self):
        a = self.repo("alpha")
        a.commit("main")
        a.push("main")
        a.set_default("main")
        s = self.run_sync(dry_run=True)
        self.assertEqual(self.status(s, "alpha"), S.WOULD_CREATE)
        self.assertEqual(self.gl.projects, {})
        self.assertEqual(self.gl.calls, [])
        self.assertFalse((self.cfg.repos_dir / "alpha.git").exists())
        self.assertIn("would_create=1", s.line())

    def test_dry_run_on_existing_project_reports_diff(self):
        a = self.repo("alpha")
        a.commit("main")
        a.push("main")
        a.set_default("main")
        self.run_sync()
        a.commit("main")
        a.push("main")
        calls = list(self.gl.calls)
        before = self.gl.refs(f"{NS}/alpha")
        s = self.run_sync(dry_run=True)
        self.assertEqual(self.status(s, "alpha"), S.WOULD_SYNC)
        self.assertEqual(self.gl.refs(f"{NS}/alpha"), before)
        self.assertEqual(self.gl.calls, calls)

    def test_name_sanitising(self):
        r = self.repo(".github")
        r.commit("main")
        r.push("main")
        r.set_default("main")
        s = self.run_sync()
        self.assertEqual(self.status(s, ".github"), S.CREATED)
        self.assertMirrored(r, gitlab_path="dot-github")

    def test_disappeared_repo_is_reported_not_deleted(self):
        a = self.repo("gone")
        a.commit("main")
        a.push("main")
        a.set_default("main")
        self.run_sync()
        del self.gh.repos["gone"]
        s = self.run_sync()
        self.assertEqual(s.disappeared, ["gone"])
        self.assertIsNotNone(self.gl.get_project(f"{NS}/gone"))
        self.assertTrue((self.cfg.repos_dir / "gone.git").exists())

    def test_filters(self):
        for name, kw in (("own", {}), ("forked", {"fork": True}), ("old", {"archived": True}), ("secret", {})):
            r = self.repo(name, **kw)
            r.commit("main")
            r.push("main")
            r.set_default("main")
        s = self.run_sync(exclude_repos=["secret"], include_archived=False)
        self.assertEqual({r.repo for r in s.results}, {"own"})
        self.assertEqual(s.filtered, 3)

    def test_tokens_never_logged(self):
        clear_secrets()
        register_secret(self.cfg.github_token)
        register_secret(self.cfg.gitlab_token)
        self.addCleanup(clear_secrets)
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        import mirror.logs as L
        h = Capture()
        h.setFormatter(L.RedactingFormatter("%(message)s"))
        L.log.handlers[:] = [h]
        L.log.setLevel(logging.DEBUG)
        a = self.repo("alpha")
        a.commit("main")
        a.push("main")
        a.set_default("main")
        self.gh.extra.append(Repo(name="broken", full_name="x/broken", clone_url=str(self.tmp / "nope.git"),
                                  html_url="https://github.com/x/broken", default_branch="main"))
        self.run_sync()
        setup_logging(logging.CRITICAL)
        text = "\n".join(records)
        self.assertTrue(records)
        self.assertNotIn("TESTTOKEN", text)


if __name__ == "__main__":
    unittest.main()
