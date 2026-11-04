"""Entry points: scheduler loop, single run, healthcheck."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config, ConfigError, interval_from_env, load_config
from .github import GitHubClient
from .gitlab import GitLabClient
from .logs import log, register_secret, setup_logging
from .sync import Syncer

_stop = threading.Event()


class LockBusy(Exception):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock so two runs can never overlap (also across processes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise LockBusy(str(path)) from e
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---- state -------------------------------------------------------------

def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def update_state(path: Path, **changes: object) -> dict:
    state = read_state(path)
    state.update(changes)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return state


def check_health(state: dict, interval: int, now: float) -> tuple[bool, str]:
    """Healthy if the last fully successful run (or, before the first one, container start)
    is younger than 3x the sync interval."""
    limit = 3 * interval
    last_ok = state.get("last_success")
    if last_ok:
        age = now - float(last_ok)
        return age <= limit, f"last successful run {age:.0f}s ago (limit {limit}s)"
    started = state.get("started_at")
    if started:
        age = now - float(started)
        return age <= limit, f"no successful run yet; started {age:.0f}s ago (limit {limit}s)"
    return False, "no state file"


# ---- running -----------------------------------------------------------

def run_once(cfg: Config) -> bool:
    """Do one locked sync run. Returns True if it completed with no failed repos."""
    try:
        with run_lock(cfg.lock_file):
            update_state(cfg.state_file, last_run_started=time.time())
            try:
                summary = Syncer(cfg, GitHubClient(cfg), GitLabClient(cfg)).run()
            except Exception as e:
                log.error("Run aborted: %s", e)
                update_state(cfg.state_file, last_run_finished=time.time(), last_run_ok=False,
                             last_error=str(e)[:500])
                return False
            ok = summary.failed == 0
            changes: dict[str, object] = {
                "last_run_finished": time.time(),
                "last_run_ok": ok,
                "last_summary": summary.line(),
                "last_error": None,
                "last_failed": [r.repo for r in summary.results if r.status == "failed"],
            }
            if ok:
                changes["last_success"] = time.time()
            update_state(cfg.state_file, **changes)
            return ok
    except LockBusy:
        log.warning("Another sync run is still in progress (lock %s held); skipping this one", cfg.lock_file)
        return False


def _handle_signal(signum: int, _frame: object) -> None:
    log.info("Received signal %d; stopping after the current step", signum)
    _stop.set()


def check_data_dir(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    probe = cfg.data_dir / ".write-test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise ConfigError(f"{cfg.data_dir} is not writable by uid {os.getuid()}: {e}") from e
    cfg.repos_dir.mkdir(parents=True, exist_ok=True)


def loop(cfg: Config) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    update_state(cfg.state_file, started_at=time.time())
    while not _stop.is_set():
        started = time.monotonic()
        run_once(cfg)
        wait = max(0.0, cfg.sync_interval - (time.monotonic() - started))
        if not _stop.is_set():
            log.info("Next run in %.0fs", wait)
        _stop.wait(wait)
    log.info("Stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mirror", description="Mirror GitHub repositories to GitLab")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "once", "healthcheck"],
                        help="run: sync now and then every SYNC_INTERVAL (default); once: a single sync; "
                             "healthcheck: exit 0 if the last successful run is recent")
    args = parser.parse_args(argv)

    if args.command == "healthcheck":
        data_dir = Path(os.environ.get("DATA_DIR", "").strip() or "/data")
        try:
            interval = interval_from_env()
        except ConfigError as e:
            print(f"unhealthy: {e}")
            return 1
        ok, msg = check_health(read_state(data_dir / "state.json"), interval, time.time())
        print(("healthy: " if ok else "unhealthy: ") + msg)
        return 0 if ok else 1

    setup_logging()
    try:
        cfg = load_config()
        register_secret(cfg.github_token)
        register_secret(cfg.gitlab_token)
        check_data_dir(cfg)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        return 2

    log.info("github-mirror starting: %s", cfg.describe())
    if args.command == "once":
        return 0 if run_once(cfg) else 1
    loop(cfg)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
