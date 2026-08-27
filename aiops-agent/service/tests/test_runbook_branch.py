"""The runbook branch: which remediation the diagnostics select (day36).

Until now a runbook offered one fix per alert, and the applicability check
struck the wrong one off afterwards. These tests pin the front-end version of
that: the branch is decided by what the Tier 1 diagnostics answered, and it
fails *open* — a step is dropped only when a condition is decidedly false.
"""

from app.runbook import (
    Condition,
    DiagnosticResult,
    Runbook,
    Step,
    format_remediation_choices,
    load_runbooks,
    select_remediation,
)

TEMPLATE_CHANGED = (
    "the last rollout changed image — a rollback restores a genuinely different pod template"
)
CONFIG_CHANGED = (
    "the last rollout changed nothing the process runs (at most a version label or a restart). "
    "If behaviour changed, the cause is outside the template — check the mounted config: "
    "configMap/payment-flags"
)


def _rb() -> Runbook:
    return Runbook(
        id="rb",
        diagnostics=[Step(id="prov", desc="what changed", action="k8s_change_provenance")],
        remediation=[
            Step(
                desc="roll back",
                action="k8s.rollout_undo",
                when=Condition(
                    diagnostic="prov", output_contains="genuinely different pod template"
                ),
            ),
            Step(
                desc="put the flag back",
                action="k8s.configmap_flag_set",
                when=Condition(diagnostic="prov", output_contains="outside the template"),
            ),
            Step(desc="page the owner", action="noop.page"),
        ],
    )


def _result(text: str, status: str = "ran") -> DiagnosticResult:
    return DiagnosticResult(
        id="prov",
        desc="what changed",
        action="k8s_change_provenance",
        status=status,
        output_preview=text[:500],
        output_text=text,
    )


def _chosen(results, params=None):
    return [c.step.action for c in select_remediation(_rb(), results, params or {}) if c.applicable]


def test_template_change_selects_rollback_not_the_flag():
    assert _chosen([_result(TEMPLATE_CHANGED)]) == ["k8s.rollout_undo", "noop.page"]


def test_config_change_selects_the_flag_not_rollback():
    """The whole point: the same alert, the same `git_version` label, and the
    rollback is never offered."""
    assert _chosen([_result(CONFIG_CHANGED)]) == ["k8s.configmap_flag_set", "noop.page"]


def test_unconditional_step_always_applies():
    assert "noop.page" in _chosen([_result(CONFIG_CHANGED)])
    assert "noop.page" in _chosen(None)


def test_no_diagnostics_keeps_every_step():
    """Fail-open: with nothing to branch on we are back to the old behaviour,
    not to an empty proposal list."""
    assert _chosen(None) == ["k8s.rollout_undo", "k8s.configmap_flag_set", "noop.page"]


def test_unknown_diagnostic_id_keeps_the_step():
    """An authoring bug in the runbook is not a verdict about the incident."""
    other = _result(TEMPLATE_CHANGED)
    other.id = "something-else"
    assert _chosen([other]) == ["k8s.rollout_undo", "k8s.configmap_flag_set", "noop.page"]


def test_errored_diagnostic_does_not_decide_the_branch():
    assert _chosen([_result("", status="error")]) == [
        "k8s.rollout_undo",
        "k8s.configmap_flag_set",
        "noop.page",
    ]


def test_status_clause():
    rb = Runbook(
        id="rb",
        remediation=[
            Step(desc="x", action="a", when=Condition(diagnostic="prov", status="pass")),
            Step(desc="y", action="b", when=Condition(diagnostic="prov", status=["fail", "ran"])),
        ],
    )
    res = [_result("whatever", status="fail")]
    got = [c.step.action for c in select_remediation(rb, res, {}) if c.applicable]
    assert got == ["b"]


def test_param_equals_needs_no_diagnostic():
    rb = Runbook(
        id="rb",
        remediation=[
            Step(
                desc="x",
                action="a",
                when=Condition(param_equals={"service_name": "payment-service"}),
            )
        ],
    )
    assert [
        c.applicable for c in select_remediation(rb, None, {"service_name": "order-service"})
    ] == [False]
    assert [
        c.applicable for c in select_remediation(rb, None, {"service_name": "payment-service"})
    ] == [True]


def test_excluded_steps_stay_visible_with_a_reason():
    """A silently shortened list teaches the on-call nothing."""
    text = format_remediation_choices(select_remediation(_rb(), [_result(CONFIG_CHANGED)], {}))
    assert "NOT FOR THIS INCIDENT" in text
    assert "k8s.rollout_undo" in text
    assert "prov does not say" in text


def test_shipped_payment_runbook_branches_both_ways():
    rb = next(b for b in load_runbooks("runbooks") if b.id == "payment-bad-deploy")
    prov = next(s for s in rb.diagnostics if s.id == "provenance")
    # the branch diagnostic must carry no check: execution.py aborts an approved
    # action on any failed diagnostic check, and this one exists to sort, not assert
    assert prov.check is None

    def chosen(text):
        res = DiagnosticResult(
            id="provenance",
            desc=prov.desc,
            action=prov.action,
            status="ran",
            output_preview=text[:500],
            output_text=text,
        )
        return [c.step.action for c in select_remediation(rb, [res], {}) if c.applicable]

    assert chosen(TEMPLATE_CHANGED) == ["k8s.rollout_undo"]
    assert chosen(CONFIG_CHANGED) == ["manual.configmap_flag_set_and_restart"]
