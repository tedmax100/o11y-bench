"""GitHub REST tools for deploy correlation during RCA.

Output is aggressively trimmed — a single compare can otherwise dump 100K+
tokens of patch into the LLM context. The hard caps here are deliberate; if
the agent needs more, it should narrow the ref range or pull specific files
with `github_get_file`.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
from langchain_core.tools import tool

from ..config import settings

GH_API = "https://api.github.com"

MAX_COMMITS = 30
MAX_FILES = 20
MAX_PATCH_LINES = 50
MAX_FILE_LINES = 400


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


def _trim_patch(patch: str | None) -> str:
    if not patch:
        return ""
    lines = patch.splitlines()
    if len(lines) <= MAX_PATCH_LINES:
        return patch
    head = lines[:MAX_PATCH_LINES]
    return "\n".join(head) + f"\n... ({len(lines) - MAX_PATCH_LINES} more lines truncated)"


@tool
async def github_compare(repo: str, base: str, head: str) -> dict[str, Any]:
    """Compare two refs on a GitHub repo and return commits + per-file diff.

    Use this when a deployment log shows `<old> -> <new>` and you want to know
    what code changed between those two versions. Refs can be SHAs, tags
    (e.g. `v2.5.0`), or branch names.

    Args:
        repo: Full repo path, `owner/name` (e.g. `tedmax100/o11y-bench`).
        base: The older ref (e.g. `v2.4.1`).
        head: The newer ref (e.g. `v2.5.0`).

    Output is trimmed to fit context: at most 30 commits and 20 files; each
    file's patch is capped at 50 lines. If the diff is bigger than that, follow
    up with `github_get_file` on suspicious paths.
    """
    url = f"{GH_API}/repos/{repo}/compare/{base}...{head}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_headers())
    if r.status_code == 404:
        return {"error": f"repo or refs not found: {repo} {base}...{head}"}
    if r.status_code == 401 or r.status_code == 403:
        return {"error": f"github auth/rate-limit ({r.status_code}): {r.text[:200]}"}
    r.raise_for_status()
    d = r.json()

    commits = d.get("commits") or []
    files = d.get("files") or []

    return {
        "repo": repo,
        "base": base,
        "head": head,
        "status": d.get("status"),
        "ahead_by": d.get("ahead_by"),
        "behind_by": d.get("behind_by"),
        "total_commits": len(commits),
        "total_files": len(files),
        "commits": [
            {
                "sha": c["sha"][:8],
                "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                "msg": c["commit"]["message"].splitlines()[0],
            }
            for c in commits[:MAX_COMMITS]
        ],
        "files": [
            {
                "path": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "patch": _trim_patch(f.get("patch")),
            }
            for f in files[:MAX_FILES]
        ],
        "truncated": {
            "commits": len(commits) > MAX_COMMITS,
            "files": len(files) > MAX_FILES,
        },
    }


@tool
async def github_get_file(
    repo: str,
    path: str,
    ref: str,
    start: int = 1,
    end: int = 200,
) -> dict[str, Any]:
    """Read a slice of a file at a specific ref.

    Always pass `start` and `end` — do not dump whole files. Use this after
    `github_compare` flags a suspicious file, to read the surrounding code
    at the post-deploy ref.

    Args:
        repo: `owner/name`.
        path: File path within the repo.
        ref: SHA, tag, or branch.
        start: First line to return (1-indexed). Defaults to 1.
        end: Last line to return (inclusive). Defaults to 200. Hard-capped at
             400 lines past `start`.
    """
    if end < start:
        return {"error": f"end ({end}) must be >= start ({start})"}
    end = min(end, start + MAX_FILE_LINES - 1)

    url = f"{GH_API}/repos/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, params={"ref": ref}, headers=_headers())
    if r.status_code == 404:
        return {"error": f"file not found: {repo}@{ref}:{path}"}
    if r.status_code in (401, 403):
        return {"error": f"github auth/rate-limit ({r.status_code}): {r.text[:200]}"}
    r.raise_for_status()
    d = r.json()

    if d.get("encoding") != "base64" or "content" not in d:
        return {"error": f"unsupported content encoding: {d.get('encoding')}"}

    try:
        text = base64.b64decode(d["content"]).decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"file is not utf-8 text: {path}"}

    lines = text.splitlines()
    total = len(lines)
    sliced = lines[start - 1 : end]

    return {
        "repo": repo,
        "path": path,
        "ref": ref,
        "total_lines": total,
        "start": start,
        "end": min(end, total),
        "content": "\n".join(sliced),
    }
