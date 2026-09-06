"""The registry's label names have to survive the trip to decision time.

Each test pins one way that trip used to fail silently: a label the store
carries but the artifact drops, a value domain that leaks the incident's answer
into the prompt, and a missing registry reported as "no labels exist".
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from app.signals.vocabulary import (
    Label,
    Vocabulary,
    build_vocabulary,
    get_vocabulary,
    render_vocabulary,
)

_COMMON = """
groups:
  - id: registry.app
    type: attribute_group
    brief: app attrs
    attributes:
      - id: app.outcome
        type:
          members:
            - id: declined
              value: declined
              brief: b
              stability: development
        stability: development
        brief: outcome
        note: "Flat key in current code: `status` (on metrics)."
      - id: app.fail_reason
        type:
          members:
            - id: new_validator
              value: new_validator
              brief: b
              stability: development
        stability: development
        brief: reason
        note: "Flat key in current code: `reason`."
  - id: registry.deploy_provenance
    type: attribute_group
    brief: provenance
    attributes:
      - id: service.name
        type: string
        stability: development
        brief: the service
        note: "Flat key in current code: `service_name` (resource attr). NOT `service`."
  - id: resource.demo_service
    type: resource
    stability: development
    brief: resource
    attributes:
      - ref: service.name
        requirement_level: required
"""

_METRICS = """
groups:
  - id: metric.app.payment.charges.count
    type: metric
    metric_name: app.payment.charges.count
    stability: development
    brief: charges
    note: "Current code metric: `payment_charges_total`."
    instrument: counter
    unit: "{charge}"
    attributes:
      - ref: app.outcome
        requirement_level: required
      - ref: app.fail_reason
        requirement_level: recommended
  - id: metric.app.not.shipped
    type: metric
    metric_name: app.not.shipped
    stability: development
    brief: declared but not emitted yet
    instrument: counter
    unit: "1"
    attributes:
      - ref: app.outcome
        requirement_level: required
"""


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "common.yaml").write_text(textwrap.dedent(_COMMON), encoding="utf-8")
    (d / "metrics.yaml").write_text(textwrap.dedent(_METRICS), encoding="utf-8")
    return d


def test_identity_label_survives_compilation(model_dir):
    """The regression this module exists for: `service_name` is on every series,
    so a "common labels" filter drops it as uninteresting — and the agent, left
    with no evidence, writes the conventional `service` instead."""
    v = build_vocabulary(model_dir)
    assert [label.key for label in v.identity_labels] == ["service_name"]
    assert v.identity_labels[0].declared_as == "service.name"


def test_metric_labels_resolve_to_the_emitted_flat_key(model_dir):
    v = build_vocabulary(model_dir)
    charges = next(m for m in v.metrics if m.prom_name == "payment_charges_total")
    assert [label.key for label in charges.labels] == ["status", "reason"]
    assert charges.declared_as == "app.payment.charges.count"
    assert charges.instrument == "counter"


def test_a_metric_with_no_emitted_name_is_skipped(model_dir):
    """Declared-but-not-shipped metrics have no Prom name to query by. Emitting
    them would put names into the prompt that resolve against nothing."""
    assert [m.prom_name for m in build_vocabulary(model_dir).metrics] == ["payment_charges_total"]


def test_rendered_block_names_the_identity_label(model_dir):
    block = render_vocabulary(build_vocabulary(model_dir))
    assert "`service_name`" in block
    assert "payment_charges_total" in block
    assert "`status`" in block


def test_rendered_block_withholds_value_domains(model_dir):
    """Label names are the environment's shape; label values are often the
    finding. `new_validator` is this demo's root cause, and it is a declared
    enum member — the artifact keeps it, the injected block must not."""
    v = build_vocabulary(model_dir)
    reason = next(label for m in v.metrics for label in m.labels if label.key == "reason")
    assert reason.values == ["new_validator"]
    assert "new_validator" not in render_vocabulary(v)


def test_missing_registry_is_empty_not_wrong(tmp_path):
    """Fail-open: an absent registry yields no block, not a claim that this
    environment has no labels."""
    v = build_vocabulary(tmp_path / "nope")
    assert v.metrics == [] and v.identity_labels == []
    assert render_vocabulary(v) == ""


def test_get_vocabulary_round_trips_the_artifact(tmp_path, model_dir):
    p = tmp_path / "label_vocabulary.yaml"
    p.write_text(yaml.safe_dump(build_vocabulary(model_dir).model_dump()), encoding="utf-8")
    assert get_vocabulary(p).identity_labels[0].key == "service_name"


def test_unreadable_artifact_is_empty_not_an_exception(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("{[not yaml", encoding="utf-8")
    assert get_vocabulary(p) == Vocabulary()


def test_shipped_artifact_declares_the_label_the_stores_use():
    """The committed artifact, not a fixture: whatever the registry says today,
    the block the agent receives has to name `service_name`."""
    shipped = get_vocabulary()
    assert Label(key="service_name", declared_as="service.name", values=[], note="").key in [
        label.key for label in shipped.identity_labels
    ]
