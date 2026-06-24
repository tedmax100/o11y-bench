"""Draft runbook synthesis — knowledge-loop §1 閉環二.

When an investigation is labeled correct=True and no active runbook matched the
alert, we know: (a) the agent found the root cause, and (b) there is no SOP for
this alert yet. This module synthesises a draft runbook skeleton from the
investigation record so that knowledge doesn't disappear into a log file.

The synthesis is **template-based** (no LLM): it maps investigation fields
(hypothesis, decisions, suspected_version) to runbook structure fields. The
`diagnostics` and `rollback` contracts are intentionally left empty — those
require human judgment to fill in correctly. The skeleton is written to
`runbooks/drafts/` so it's not auto-loaded by `match_runbook` until a human
promotes it to `runbooks/`.

If `settings.draft_runbook_pr_enabled` is True and `settings.draft_runbook_repo`
is set, a GitHub PR is opened so the on-call can review and merge it.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from .config import settings
from .investigations import InvestigationRecord
from .runbook import match_runbook

logger = logging.getLogger("aiops_agent.draft_runbook")


def synthesize_draft_runbook(inv: InvestigationRecord) -> dict:
    """Build a runbook skeleton dict from a correct investigation record.

    `diagnostics` is intentionally empty — the tool-call history in the plugin
    shows what the agent queried; a human should decide which checks are the right
    preconditions to assert. `rollback` on each remediation step is also left None
    for the same reason.
    """
    remediation = []
    for d in inv.decisions:
        if not d.action:
            continue
        step: dict = {
            "desc": d.reason or d.action,
            "action": d.action,
            "reversible": None,       # human fills in
            "requires_approval": True,  # safe default for drafts
        }
        remediation.append(step)

    draft = {
        "id": _draft_id(inv),
        "title": f"[DRAFT] {(inv.hypothesis or inv.summary or inv.alertname or 'unknown')[:80]}",
        "_meta": {
            "source_investigation": inv.fp,
            "confidence": round(inv.confidence, 2),
            "generated_ts": inv.ts,
            "status": "draft — requires human review before activation",
            "instructions": (
                "Fill in `diagnostics` checks and `rollback` contracts before "
                "promoting to runbooks/. Set `autonomy: auto` only after a human "
                "has reviewed and the CI runbook-schema check passes."
            ),
        },
        "trigger": {
            "alertname": inv.alertname or "",
            "labels": {"service_name": inv.service} if inv.service else {},
        },
        "diagnostics": [],   # intentionally empty — human fills from agent's tool history
        "remediation": remediation,
    }
    return draft


def _draft_id(inv: InvestigationRecord) -> str:
    parts = ["draft"]
    if inv.alertname:
        parts.append(inv.alertname.lower().replace(" ", "-"))
    if inv.service:
        parts.append(inv.service.lower().replace(" ", "-"))
    return "-".join(parts)


def _draft_filename(inv: InvestigationRecord) -> str:
    date = (inv.ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))[:10]
    return f"{_draft_id(inv)}-{date}.yaml"


def _drafts_dir() -> Path:
    return Path(settings.runbook_dir) / "drafts"


async def maybe_synthesize_draft(inv: InvestigationRecord) -> str | None:
    """Check if the investigation's alert has no active runbook, and if so
    synthesize + save a draft. Returns the file path written, or None."""
    if not settings.draft_runbook_enabled:
        return None

    # Only synthesize when no active runbook covers this alert.
    labels = (inv.alert.get("labels") or {}) if inv.alert else {}
    if not labels and inv.alertname:
        labels = {"alertname": inv.alertname}
        if inv.service:
            labels["service_name"] = inv.service
    annotations = (inv.alert.get("annotations") or {}) if inv.alert else {}

    if match_runbook(labels, annotations) is not None:
        logger.debug(
            "draft_runbook: skipping fp=%s — active runbook already covers this alert", inv.fp
        )
        return None

    draft = synthesize_draft_runbook(inv)
    path = _save_draft(draft, inv)
    logger.info("draft_runbook: wrote %s (fp=%s)", path, inv.fp)

    if settings.draft_runbook_pr_enabled:
        await _open_github_pr(draft, path, inv)

    return str(path)


def _save_draft(draft: dict, inv: InvestigationRecord) -> Path:
    """Serialise the draft to YAML and write it to runbooks/drafts/. Creates the
    directory if needed."""
    d = _drafts_dir()
    d.mkdir(parents=True, exist_ok=True)
    filename = _draft_filename(inv)
    p = d / filename
    p.write_text(yaml.dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


async def _open_github_pr(draft: dict, local_path: Path, inv: InvestigationRecord) -> None:
    """Open a GitHub PR adding the draft runbook file.

    Flow (GitHub REST API):
      1. GET /repos/{repo}/git/ref/heads/{base}   → base SHA
      2. POST /repos/{repo}/git/blobs             → blob SHA for the file content
      3. POST /repos/{repo}/git/trees             → tree SHA
      4. POST /repos/{repo}/git/commits           → commit SHA
      5. POST /repos/{repo}/git/refs              → create branch pointing at commit
      6. POST /repos/{repo}/pulls                 → PR
    """
    if not settings.github_token or not settings.draft_runbook_repo:
        logger.warning(
            "draft_runbook_pr_enabled but github_token or draft_runbook_repo not set — skipping PR"
        )
        return

    repo = settings.draft_runbook_repo
    base_branch = "main"
    new_branch = f"draft-runbook/{_draft_filename(inv).removesuffix('.yaml')}"
    file_path_in_repo = f"runbooks/drafts/{_draft_filename(inv)}"
    content = local_path.read_text(encoding="utf-8")
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{repo}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Get base branch SHA
            r = await client.get(f"{api}/git/ref/heads/{base_branch}", headers=headers)
            r.raise_for_status()
            base_sha = r.json()["object"]["sha"]

            # 2. Create blob
            r = await client.post(
                f"{api}/git/blobs",
                headers=headers,
                json={"content": content_b64, "encoding": "base64"},
            )
            r.raise_for_status()
            blob_sha = r.json()["sha"]

            # 3. Create tree
            r = await client.post(
                f"{api}/git/trees",
                headers=headers,
                json={
                    "base_tree": base_sha,
                    "tree": [{"path": file_path_in_repo, "mode": "100644",
                               "type": "blob", "sha": blob_sha}],
                },
            )
            r.raise_for_status()
            tree_sha = r.json()["sha"]

            # 4. Create commit
            commit_msg = (
                f"feat(runbooks): add draft runbook {_draft_id(inv)}\n\n"
                f"Auto-synthesized from investigation {inv.fp[:8]} "
                f"(confidence {inv.confidence:.0%}). Requires human review before activation."
            )
            r = await client.post(
                f"{api}/git/commits",
                headers=headers,
                json={"message": commit_msg, "tree": tree_sha, "parents": [base_sha]},
            )
            r.raise_for_status()
            commit_sha = r.json()["sha"]

            # 5. Create branch
            r = await client.post(
                f"{api}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{new_branch}", "sha": commit_sha},
            )
            r.raise_for_status()

            # 6. Open PR
            pr_body = (
                f"## Draft runbook: `{_draft_id(inv)}`\n\n"
                f"Auto-synthesized from investigation `{inv.fp}` "
                f"(confidence {inv.confidence:.0%}).\n\n"
                f"**Hypothesis:** {inv.hypothesis}\n\n"
                "## Before merging to `runbooks/`\n\n"
                "- [ ] Fill in `diagnostics` checks with the right precondition queries\n"
                "- [ ] Add `rollback` contract to each remediation step\n"
                "- [ ] Decide `autonomy: auto` vs `autonomy: propose`\n"
                "- [ ] Remove `_meta` block (or keep as comment)\n"
                "- [ ] CI runbook-schema check passes\n"
            )
            r = await client.post(
                f"{api}/pulls",
                headers=headers,
                json={
                    "title": f"feat(runbooks): draft — {_draft_id(inv)}",
                    "body": pr_body,
                    "head": new_branch,
                    "base": base_branch,
                },
            )
            r.raise_for_status()
            pr_url = r.json().get("html_url", "")
            logger.info("draft_runbook: opened PR %s (fp=%s)", pr_url, inv.fp)

    except Exception as e:
        logger.warning("draft_runbook: GitHub PR failed fp=%s: %s", inv.fp, e)
