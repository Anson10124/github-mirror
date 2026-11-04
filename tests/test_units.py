"""Unit tests: config, names, filters, HTTP retries, GitLab API calls, health, lock, askpass."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

from tests.helpers import make_config  # noqa: F401  (also makes git hermetic)

from mirror.config import ConfigError, load_config, parse_duration, parse_list
from mirror.github import GitHubClient, Repo, filter_repos
from mirror.gitlab import GitLabClient, Project
from mirror.gitops import ASKPASS, GitError
from mirror.http import HttpClient, HttpError, parse_next_link, retry_delay
from mirror.logs import RedactingFormatter, clear_secrets, redact, register_secret
from mirror.main import LockBusy, check_health, run_lock
from mirror.names import gitlab_path

ENV = {"GITHUB_TOKEN": "fake-gh-token", "GITLAB_TOKEN": "fake-gl-token", "GITLAB_NAMESPACE": "Anson10124"}


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        c = load_config(ENV)
        self.assertEqual(c.gitlab_url, "https://gitlab.com")
        self.assertEqual(c.sync_interval, 3600)
        self.assertFalse(c.include_forks)
        self.assertTrue(c.include_archived)
        self.assertEqual(c.gitlab_visibility, "private")
        self.assertFalse(c.enable_lfs)
        self.assertFalse(c.dry_run)
        self.assertFalse(c.adopt_existing)
        self.assertEqual(c.exclude_repos, [])
        self.assertEqual(str(c.data_dir), "/data")

    def test_overrides(self):
        c = load_config({**ENV, "SYNC_INTERVAL": "30m", "INCLUDE_FORKS": "yes", "INCLUDE_ARCHIVED": "false",
                         "EXCLUDE_REPOS": "Foo, Anson10124/bar ,,baz-*", "GITLAB_VISIBILITY": "Internal",
                         "DRY_RUN": "1", "GITLAB_URL": "https://git.example.com/"})
        self.assertEqual(c.sync_interval, 1800)
        self.assertTrue(c.include_forks)
        self.assertFalse(c.include_archived)
        self.assertEqual(c.exclude_repos, ["foo", "bar", "baz-*"])
        self.assertEqual(c.gitlab_visibility, "internal")
        self.assertTrue(c.dry_run)
        self.assertEqual(c.gitlab_url, "https://git.example.com")

    def test_missing_tokens(self):
        with self.assertRaises(ConfigError) as cm:
            load_config({"GITLAB_NAMESPACE": "x", "GITHUB_TOKEN": " "})
        self.assertIn("GITHUB_TOKEN", str(cm.exception))
        self.assertIn("GITLAB_TOKEN", str(cm.exception))

    def test_bad_values(self):
        for k, v in (("GITLAB_VISIBILITY", "secret"), ("DRY_RUN", "maybe"), ("SYNC_INTERVAL", "1x"),
                     ("SYNC_INTERVAL", "10")):
            with self.assertRaises(ConfigError, msg=f"{k}={v}"):
                load_config({**ENV, k: v})

    def test_durations(self):
        self.assertEqual(parse_duration("3600", 1), 3600)
        self.assertEqual(parse_duration("1h30m", 1), 5400)
        self.assertEqual(parse_duration("2h", 1), 7200)
        self.assertEqual(parse_duration("", 99), 99)
        self.assertEqual(parse_list("a\nb, c"), ["a", "b", "c"])

    def test_describe_has_no_secrets(self):
        self.assertNotIn("fake-gh-token", load_config(ENV).describe())


class TestNames(unittest.TestCase):
    def test_mapping(self):
        cases = {
            "website": "website",
            "SF-Pro-Family": "SF-Pro-Family",
            "anson10124.github.io": "anson10124.github.io",
            "apple_map_proxy": "apple_map_proxy",
            ".github": "dot-github",
            "-weird": "dash-weird",
            "foo.git": "foo.git-repo",
            "feed.atom": "feed.atom-repo",
            "has space+plus": "has-space-plus",
        }
        for src, want in cases.items():
            self.assertEqual(gitlab_path(src), want, src)


def _repo(name, **kw):
    return Repo(name=name, full_name=f"o/{name}", clone_url="u", html_url="h", default_branch="main", **kw)


class TestFilters(unittest.TestCase):
    def test_filtering(self):
        repos = [_repo("a"), _repo("Fork", fork=True), _repo("arch", archived=True), _repo("Kollod-v1"),
                 _repo("dis", disabled=True)]
        cfg = make_config(Path("/tmp"), exclude_repos=["kollod-*"])
        inc, skip = filter_repos(repos, cfg)
        self.assertEqual([r.name for r in inc], ["a", "arch"])
        self.assertEqual({r.name for r, _ in skip}, {"Fork", "Kollod-v1", "dis"})
        cfg.include_forks, cfg.include_archived = True, False
        inc, _ = filter_repos(repos, cfg)
        self.assertEqual([r.name for r in inc], ["a", "Fork"])


# ---------------------------------------------------------------- HTTP

class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Replays a list of (status, body, headers); records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout):
        body = req.data.decode() if req.data else None
        self.requests.append((req.get_method(), req.full_url, body, dict(req.header_items())))
        status, rbody, headers = self.responses.pop(0)
        if isinstance(status, Exception):
            raise status
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", headers or {}, io.BytesIO(rbody.encode()))
        return FakeResp(status, rbody, headers)


def client(responses, **kw):
    sleeps = []
    opener = FakeOpener(responses)
    c = HttpClient("https://api.example.com", {"Authorization": "Bearer SECRET"}, name="t", opener=opener,
                   sleep=sleeps.append, clock=lambda: 1000.0, **kw)
    return c, opener, sleeps


class TestHttp(unittest.TestCase):
    def test_retry_on_429_with_retry_after(self):
        c, o, sleeps = client([(429, "slow down", {"Retry-After": "7"}), (200, '{"ok": 1}', {})])
        self.assertEqual(c.get("/x").data, {"ok": 1})
        self.assertEqual(sleeps, [8.0])

    def test_github_primary_limit_waits_until_reset(self):
        c, o, sleeps = client([(403, '{"message":"API rate limit exceeded"}',
                                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}),
                               (200, "[]", {})])
        c.get("/x")
        self.assertEqual(sleeps, [101.0])

    def test_github_secondary_limit(self):
        c, o, sleeps = client([(403, '{"message":"You have exceeded a secondary rate limit"}', {}),
                               (200, "[]", {})])
        c.get("/x")
        self.assertGreaterEqual(sleeps[0], 60)

    def test_plain_403_is_not_retried(self):
        c, o, sleeps = client([(403, '{"message":"Resource not accessible"}', {})])
        with self.assertRaises(HttpError) as cm:
            c.get("/x")
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(sleeps, [])

    def test_5xx_and_network_errors_retry_then_give_up(self):
        c, o, sleeps = client([(502, "bad gw", {}), (urllib.error.URLError("reset"), "", {}), (503, "x", {})],
                              max_attempts=3)
        with self.assertRaises(HttpError):
            c.get("/x")
        self.assertEqual(len(sleeps), 2)

    def test_wait_is_capped(self):
        c, o, sleeps = client([(429, "", {"Retry-After": "99999"}), (200, "{}", {})], max_wait=900)
        c.get("/x")
        self.assertEqual(sleeps, [900])

    def test_404_allowed(self):
        c, o, _ = client([(404, '{"message":"404 Project Not Found"}', {})])
        self.assertIsNone(c.get("/x", allow_404=True))

    def test_pagination(self):
        c, o, _ = client([
            (200, '[{"n":1},{"n":2}]', {"Link": '<https://api.example.com/x?page=2>; rel="next", <...>; rel="last"'}),
            (200, '[{"n":3}]', {}),
        ])
        self.assertEqual([i["n"] for i in c.paginate("/x", {"per_page": 2})], [1, 2, 3])
        self.assertEqual(o.requests[1][1], "https://api.example.com/x?page=2")

    def test_retry_delay_none_for_4xx(self):
        self.assertIsNone(retry_delay(400, {}, "", 0, 0))
        self.assertIsNone(retry_delay(404, {}, "", 0, 0))

    def test_parse_next_link(self):
        self.assertIsNone(parse_next_link(None))
        self.assertEqual(parse_next_link('<a>; rel="prev", <b>; rel="next"'), "b")

    def test_error_message_redacted(self):
        clear_secrets()
        register_secret("SUPERSECRETTOKEN")
        self.addCleanup(clear_secrets)
        e = HttpError("GET", "u", 401, "bad token SUPERSECRETTOKEN")
        self.assertNotIn("SUPERSECRETTOKEN", str(e))


class TestGitHubClient(unittest.TestCase):
    def test_list_repos_query(self):
        c, o, _ = client([(200, json.dumps([{"name": "a", "full_name": "o/a", "clone_url": "https://github.com/o/a.git",
                                             "html_url": "h", "default_branch": "main", "private": True,
                                             "fork": False, "archived": False}]), {})])
        repos = GitHubClient(make_config(Path("/tmp")), http=c).list_repos()
        self.assertEqual(repos[0].name, "a")
        url = o.requests[0][1]
        self.assertIn("/user/repos?", url)
        self.assertIn("affiliation=owner", url)
        self.assertIn("visibility=all", url)
        self.assertIn("per_page=100", url)


PROJECT = {"id": 7, "path_with_namespace": "Anson10124/x", "http_url_to_repo": "https://gitlab.com/Anson10124/x.git",
           "default_branch": "main", "description": "", "topics": []}


class TestGitLabClient(unittest.TestCase):
    def gl(self, responses):
        c, o, _ = client(responses)
        return GitLabClient(make_config(Path("/tmp")), http=c), o

    def test_get_project_encodes_path(self):
        g, o = self.gl([(200, json.dumps(PROJECT), {})])
        p = g.get_project("Anson10124/x")
        self.assertEqual(p.id, 7)
        self.assertTrue(o.requests[0][1].endswith("/projects/Anson10124%2Fx"))

    def test_get_project_missing_and_redirect(self):
        g, _ = self.gl([(404, "{}", {})])
        self.assertIsNone(g.get_project("Anson10124/x"))
        g, _ = self.gl([(200, json.dumps({**PROJECT, "path_with_namespace": "Anson10124/renamed"}), {})])
        self.assertIsNone(g.get_project("Anson10124/x"))

    def test_create_project_payload(self):
        g, o = self.gl([(201, json.dumps({**PROJECT, "topics": ["github-mirror"]}), {})])
        p = g.create_project("x", 42, "private", "[github-mirror] Read-only mirror")
        body = json.loads(o.requests[0][2])
        self.assertEqual(o.requests[0][0], "POST")
        self.assertEqual(body["namespace_id"], 42)
        self.assertEqual(body["path"], "x")
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(body["topics"], ["github-mirror"])
        self.assertEqual(body["builds_access_level"], "disabled")
        self.assertTrue(p.is_ours)

    def test_allow_force_push_patch(self):
        g, o = self.gl([(200, '{"name":"release/1.0","allow_force_push":false}', {}), (200, "{}", {})])
        self.assertTrue(g.allow_force_push(Project.from_api(PROJECT), "release/1.0"))
        self.assertTrue(o.requests[0][1].endswith("/projects/7/protected_branches/release%2F1.0"))
        self.assertEqual(o.requests[1][0], "PATCH")
        self.assertEqual(json.loads(o.requests[1][2]), {"allow_force_push": True})

    def test_allow_force_push_noop_when_unprotected_or_allowed(self):
        g, o = self.gl([(404, "{}", {})])
        self.assertFalse(g.allow_force_push(Project.from_api(PROJECT), "main"))
        g, o = self.gl([(200, '{"name":"main","allow_force_push":true}', {})])
        self.assertFalse(g.allow_force_push(Project.from_api(PROJECT), "main"))
        self.assertEqual(len(o.requests), 1)

    def test_allow_force_push_fallback_without_patch(self):
        g, o = self.gl([(200, '{"name":"main","allow_force_push":false}', {}), (405, "{}", {}),
                        (204, "", {}), (201, "{}", {})])
        self.assertTrue(g.allow_force_push(Project.from_api(PROJECT), "main"))
        self.assertEqual([r[0] for r in o.requests], ["GET", "PATCH", "DELETE", "POST"])

    def test_ownership(self):
        self.assertFalse(Project.from_api(PROJECT).is_ours)
        self.assertTrue(Project.from_api({**PROJECT, "topics": ["a", "github-mirror"]}).is_ours)
        self.assertTrue(Project.from_api({**PROJECT, "description": "[github-mirror] x"}).is_ours)
        self.assertTrue(Project.from_api({**PROJECT, "topics": None, "tag_list": ["github-mirror"]}).is_ours)

    def test_token_in_header_not_url(self):
        g, o = self.gl([(200, json.dumps(PROJECT), {})])
        g.get_project("Anson10124/x")
        method, url, _, headers = o.requests[0]
        self.assertNotIn("SECRET", url)


# ---------------------------------------------------------------- misc

class TestHealth(unittest.TestCase):
    def test_health(self):
        self.assertEqual(check_health({"last_success": 1000}, 3600, 1000 + 3 * 3600)[0], True)
        self.assertEqual(check_health({"last_success": 1000}, 3600, 1000 + 3 * 3600 + 1)[0], False)
        self.assertEqual(check_health({"started_at": 1000}, 3600, 2000)[0], True)
        self.assertEqual(check_health({"started_at": 1000}, 60, 2000)[0], False)
        self.assertEqual(check_health({}, 3600, 2000)[0], False)


class TestLock(unittest.TestCase):
    def test_lock_excludes_second_holder(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        with run_lock(d / ".lock"):
            with self.assertRaises(LockBusy):
                with run_lock(d / ".lock"):
                    pass
            # A different process can't take it either.
            code = ("import fcntl,sys; f=open(sys.argv[1],'a+');\n"
                    "try:\n fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB); print('got')\n"
                    "except BlockingIOError: print('busy')")
            out = subprocess.run(["python3", "-c", code, str(d / ".lock")], capture_output=True, text=True).stdout
            self.assertEqual(out.strip(), "busy")
        with run_lock(d / ".lock"):
            pass  # released


class TestAskpassAndRedaction(unittest.TestCase):
    def test_askpass(self):
        os.chmod(ASKPASS, 0o755)
        env = {**os.environ, "MIRROR_GIT_USERNAME": "oauth2", "MIRROR_GIT_PASSWORD": "tok"}
        user = subprocess.run([ASKPASS, "Username for 'https://gitlab.com': "], env=env, capture_output=True, text=True)
        pw = subprocess.run([ASKPASS, "Password for 'https://oauth2@gitlab.com': "], env=env,
                            capture_output=True, text=True)
        self.assertEqual(user.stdout, "oauth2\n")
        self.assertEqual(pw.stdout, "tok\n")

    def test_redaction(self):
        clear_secrets()
        register_secret("fake-secret-abcdef")
        self.addCleanup(clear_secrets)
        self.assertEqual(redact("x fake-secret-abcdef y"), "x *** y")
        import logging
        rec = logging.LogRecord("m", logging.INFO, "", 0, "token=%s", ("fake-secret-abcdef",), None)
        self.assertNotIn("fake-secret-abcdef", RedactingFormatter("%(message)s").format(rec))

    def test_git_error_rejected_branches(self):
        out = ("To https://gitlab.com/a/b.git\n"
               "!\trefs/heads/main:refs/heads/main\t[remote rejected] (pre-receive hook declined)\n"
               "=\trefs/heads/dev:refs/heads/dev\t[up to date]\nDone\n")
        e = GitError(["push"], 1, out, "remote: GitLab: You are not allowed to force push code to a protected branch")
        self.assertTrue(e.is_protected_rejection)
        self.assertEqual(e.rejected_branches(), ["main"])


if __name__ == "__main__":
    unittest.main()
