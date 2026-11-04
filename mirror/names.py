"""Map GitHub repository names to valid GitLab project paths, predictably.

GitLab project paths may contain only letters, digits, '_', '-' and '.',
must not start with '-' or '.', and must not end in '.git' or '.atom'.

Rules (applied in order):
  1. Any character outside [A-Za-z0-9._-] becomes '-'.
  2. A leading '.' becomes 'dot-'   (".github"  -> "dot-github").
     A leading '-' becomes 'dash-'  ("-foo"     -> "dash-foo").
  3. A trailing '.git' / '.atom' gets '-repo' appended ("x.git" -> "x.git-repo").
Names that are already valid are left untouched (case is preserved).
"""

from __future__ import annotations

import re

_INVALID = re.compile(r"[^A-Za-z0-9._-]")


def gitlab_path(name: str) -> str:
    p = _INVALID.sub("-", name)
    if p.startswith("."):
        p = "dot-" + p[1:]
    elif p.startswith("-"):
        p = "dash-" + p[1:]
    if p.lower().endswith((".git", ".atom")):
        p += "-repo"
    return p
