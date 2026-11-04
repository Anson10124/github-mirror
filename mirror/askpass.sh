#!/bin/sh
# GIT_ASKPASS helper. Git calls this with a prompt ("Username for ..." /
# "Password for ...") and reads the answer from stdout. The credentials come
# from environment variables set only on the single git process that needs
# them, so tokens are never written to disk, git config or remote URLs.
case "$1" in
  Username*|username*) printf '%s\n' "${MIRROR_GIT_USERNAME:-}" ;;
  *) printf '%s\n' "${MIRROR_GIT_PASSWORD:-}" ;;
esac
