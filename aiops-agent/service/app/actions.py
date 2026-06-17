"""Action registry — the typed, whitelisted boundary for any state-changing
action (v3 §5.3). This is the *safe front half* of Tier 2: it defines the
contract (what an action is, whether it's reversible, whether it needs approval)
but **does not execute anything** in this increment.

Why a registry at all: it closes off the "LLM directly runs kubectl delete" path
architecturally. The agent can only ever name an action that is *registered* here
— typed, reversible-flagged, approval-flagged — and even then execution is gated
behind a master kill switch (`actions_enabled`, default False) AND a real
implementation that this tier deliberately leaves unwired. Read tools and write
actions stay in separate modules with separate (eventual) credentials; nothing
here imports a mutating client.

The governance gate (governance.py) decides *whether* an action may run; this
registry decides *what* an action is and is the only thing that *could* run it.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from .config import settings

logger = logging.getLogger("aiops_agent.actions")


class ActionDisabled(RuntimeError):
    """Raised when execution is attempted while the kill switch is off."""


class ActionSpec(BaseModel):
    """The contract for one remediation action. Pure metadata + an optional
    impl; the registry — not callers — owns invocation so the kill switch can't
    be bypassed."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    # Risk metadata the governance gate reads. Irreversible actions can never be
    # granted autonomous execution; approval-required ones can never be AUTO.
    reversible: bool
    requires_approval: bool
    category: str = "k8s"
    # Intentionally None in this tier — no action has a real, wired implementation
    # yet. Registering an impl is a later, separately-reviewed change (7b-4).
    impl: Callable[[dict], Awaitable[Any]] | None = Field(default=None, exclude=True)
    # Read-only blast-radius predictor (7b-2). Safe to wire now (no mutation): the
    # executor calls it before execution to compute + policy-check the footprint.
    dry_run: Callable[[dict], Awaitable[Any]] | None = Field(default=None, exclude=True)


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if spec.name in self._actions:
            raise ValueError(f"action already registered: {spec.name}")
        self._actions[spec.name] = spec

    def get(self, name: str) -> ActionSpec | None:
        return self._actions.get(name)

    def names(self) -> list[str]:
        return sorted(self._actions)

    async def execute(self, name: str, args: dict) -> Any:
        """Run a registered action — but only when BOTH the master kill switch is
        on AND the action has a real implementation. This tier ships neither, so
        every call refuses. The governance gate's verdict does not bypass this:
        'allowed to run' (policy) and 'able to run' (this switch) are separate."""
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"unknown action: {name}")
        if not settings.actions_enabled:
            raise ActionDisabled(
                f"action execution is disabled (actions_enabled=False); '{name}' "
                "would require human-approved enablement")
        if spec.impl is None:
            raise ActionDisabled(f"action '{name}' has no wired implementation (propose-only tier)")
        logger.warning("executing action %s args=%s", name, args)  # audit (step 7 expands this)
        return await spec.impl(args)


# Module-level registry seeded with the demo's remediation vocabulary. All are
# reversible + approval-required, and none has an impl — so nothing can run. The
# `dry_run` is read-only (blast_radius.py) so it's wired now; `impl` waits for 7b-4.
from .blast_radius import dry_run_rollout_undo, dry_run_scale  # noqa: E402

registry = ActionRegistry()
registry.register(ActionSpec(
    name="k8s.rollout_undo",
    description="Roll a Deployment back to its previous ReplicaSet (kubectl rollout undo).",
    reversible=True, requires_approval=True, dry_run=dry_run_rollout_undo))
registry.register(ActionSpec(
    name="k8s.scale",
    description="Change a Deployment's replica count.",
    reversible=True, requires_approval=True, dry_run=dry_run_scale))
