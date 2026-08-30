import React, { useCallback, useEffect, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Alert, Badge, Button, Field, Input, Modal, RadioButtonGroup, Spinner, Stack, useStyles2 } from '@grafana/ui';
import type { Case } from './CasesPage';

type Props = { agentServiceUrl: string };

type PendingRun = {
  fp: string;
  ts: string;
  alertname: string | null;
  service: string | null;
  summary: string;
  confidence: number;
};

type WrongModalState = {
  fp: string;
  errorDimension: string;
  correctionNote: string;
};

// Same four dimensions the Investigations page records. Kept identical on
// purpose: a verdict written from here has to mean exactly what one written
// there means, or the error breakdown is measuring two different things.
const ERROR_DIMENSION_OPTIONS = [
  { label: 'Root cause', value: 'root_cause', description: 'Wrong root cause identified' },
  { label: 'Scope', value: 'scope', description: 'Wrong service or affected scope' },
  { label: 'Action', value: 'action', description: 'Proposed remediation was wrong' },
  { label: 'Other', value: 'other', description: 'Other issue' },
];

type PendingRequest = {
  request_id: string;
  fp: string;
  action: string;
  status: string;
  expires_ts: string;
};

// An action that really ran and that nobody has passed a verdict on. This is
// the AE-SLO's missing numerator: nine had executed, three had been graded, and
// because the ratio divided by the graded rows the other six were not a low
// score — they were not on any screen at all.
type UngradedAction = {
  request_id: string;
  fp: string;
  action: string;
  status: string;
  created_ts: string;
  drill: boolean;
  outcome: string;
};

type Gate = { gate: string; proven_good: boolean; note: string };

type Autonomy = {
  granted?: boolean;
  actions_enabled?: boolean;
  gates?: Gate[];
  blockers?: Gate[];
  calibration?: {
    labeled: number;
    labeled_required: number;
    human_labeled: number;
    human_labeled_required: number;
    band_lo: number;
    band_n: number | null;
    band_n_required: number;
    band_accuracy: number | null;
    band_accuracy_required: number;
    overconfidence: number | null;
    overconfidence_max: number;
    worst_bin_gap: number | null;
    worst_bin_gap_max: number;
  };
  error?: string;
};

type Todo = {
  investigations_to_label: { count: number; items: PendingRun[] };
  requests_to_decide: { count: number; items: PendingRequest[]; expired_unattended: number };
  cases_to_label: { count: number; items: Case[] };
  actions_to_grade: { count: number; items: UngradedAction[] };
  autonomy: Autonomy;
};

// Each row is "current vs required", because the useful question is not whether
// the gate is closed — it always is — but how far from open it is.
function calibrationRows(c: NonNullable<Autonomy['calibration']>) {
  return [
    { label: 'Labeled runs', now: c.labeled, need: `≥ ${c.labeled_required}`, ok: c.labeled >= c.labeled_required },
    {
      label: 'Labeled by a human or grader',
      now: c.human_labeled,
      need: `≥ ${c.human_labeled_required}`,
      ok: c.human_labeled >= c.human_labeled_required,
    },
    {
      label: `Runs in the decision band (confidence ≥ ${c.band_lo})`,
      now: c.band_n ?? '—',
      need: `≥ ${c.band_n_required}`,
      ok: (c.band_n ?? 0) >= c.band_n_required,
    },
    {
      label: 'Accuracy in that band',
      now: c.band_accuracy ?? '—',
      need: `≥ ${c.band_accuracy_required}`,
      ok: (c.band_accuracy ?? 0) >= c.band_accuracy_required,
    },
    {
      label: 'Mean overconfidence',
      now: c.overconfidence ?? '—',
      need: `≤ ${c.overconfidence_max}`,
      ok: c.overconfidence !== null && Math.abs(c.overconfidence) <= c.overconfidence_max,
    },
    {
      label: 'Worst bin gap',
      now: c.worst_bin_gap ?? '—',
      need: `≤ ${c.worst_bin_gap_max}`,
      ok: c.worst_bin_gap !== null && c.worst_bin_gap <= c.worst_bin_gap_max,
    },
  ];
}

function TodoPage({ agentServiceUrl }: Props) {
  const styles = useStyles2(getStyles);
  const [todo, setTodo] = useState<Todo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [labelingFp, setLabelingFp] = useState<string | null>(null);
  const [gradingId, setGradingId] = useState<string | null>(null);
  const [wrongModal, setWrongModal] = useState<WrongModalState | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${agentServiceUrl}/todo?limit=20`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      setTodo(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setTodo(null);
    } finally {
      setLoading(false);
    }
  }, [agentServiceUrl]);

  useEffect(() => {
    // Same initial-fetch shape as the other pages here. The rule objects to the
    // synchronous setState inside the effect; the alternative is a spinner that
    // flickers on every filter change, so it stays and is silenced explicitly.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  const decide = useCallback(
    async (requestId: string, verdict: 'approve' | 'reject') => {
      setDecidingId(requestId);
      try {
        const res = await fetch(`${agentServiceUrl}/actions/requests/${requestId}/${verdict}`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(verdict === 'reject' ? { reason: '' } : {}),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setDecidingId(null);
      }
    },
    [agentServiceUrl, load]
  );

  // Labeling used to live only on the Investigations page, which meant the queue
  // and the way to clear it were two clicks and one context switch apart. The
  // endpoint is the same one — a verdict from here is not a different kind of
  // verdict.
  // "Did the incident actually end" is a different question from "was the root
  // cause right", and it has a different endpoint on purpose — grading a wrong
  // remediation by clicking Wrong on the investigation would put the mistake in
  // the culprit calibration curve, which is not what that curve measures.
  const grade = useCallback(
    async (requestId: string, resolved: boolean, sideEffect: boolean) => {
      setGradingId(requestId);
      try {
        const res = await fetch(`${agentServiceUrl}/actions/requests/${requestId}/outcome`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ resolved, side_effect: sideEffect, actor: 'oncall' }),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setGradingId(null);
      }
    },
    [agentServiceUrl, load]
  );

  const label = useCallback(
    async (fp: string, correct: boolean, errorDimension?: string, correctionNote?: string) => {
      setLabelingFp(fp);
      try {
        const body: Record<string, unknown> = { correct };
        if (errorDimension) {
          body.error_dimension = errorDimension;
        }
        if (correctionNote) {
          body.correction_note = correctionNote;
        }
        const res = await fetch(`${agentServiceUrl}/investigations/${fp}/label`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLabelingFp(null);
      }
    },
    [agentServiceUrl, load]
  );

  const submitWrong = useCallback(async () => {
    if (!wrongModal) {
      return;
    }
    await label(wrongModal.fp, false, wrongModal.errorDimension, wrongModal.correctionNote);
    setWrongModal(null);
  }, [wrongModal, label]);

  const auto = todo?.autonomy;
  const cal = auto?.calibration;

  return (
    <PluginPage>
      <div className={styles.wrapper}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <div className={styles.intro}>
            What is waiting on a person. Three of the four things this system is meant to do are
            blocked on work only a human can do — label a run, decide on a proposal, say what caused
            an incident — and none of it had an entry point until this page.
          </div>
          <Button variant="secondary" icon="sync" onClick={load} disabled={loading}>
            Refresh
          </Button>
        </Stack>

        {error && (
          <Alert title="Could not load the queue" severity="error">
            {error}
          </Alert>
        )}
        {loading && <Spinner inline />}

        {todo && (
          <>
            <div className={styles.card}>
              <Stack direction="row" gap={1} alignItems="center" wrap="wrap">
                <h4 className={styles.h4}>Autonomy</h4>
                <Badge
                  text={auto?.granted ? 'AUTO would be granted' : 'AUTO withheld'}
                  color={auto?.granted ? 'green' : 'orange'}
                />
                {/* Policy and the kill switch fail in ways that look identical
                    from outside, so they are reported apart. */}
                <Badge
                  text={auto?.actions_enabled ? 'actions enabled' : 'kill switch: actions off'}
                  color={auto?.actions_enabled ? 'blue' : 'red'}
                />
              </Stack>
              {auto?.error && <Alert title="Gate state unavailable" severity="warning">{auto.error}</Alert>}
              {auto?.gates?.map((g) => (
                <div key={g.gate} className={styles.row}>
                  <Badge text={g.gate} color={g.proven_good ? 'green' : 'red'} />
                  <span className={styles.reason}>{g.note}</span>
                </div>
              ))}
              {cal && (
                <table className={styles.table}>
                  <tbody>
                    {calibrationRows(cal).map((r) => (
                      <tr key={r.label}>
                        <td>{r.label}</td>
                        <td className={r.ok ? styles.ok : styles.bad}>{String(r.now)}</td>
                        <td className={styles.reason}>{r.need}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className={styles.card}>
              <Stack direction="row" gap={1} alignItems="center">
                <h4 className={styles.h4}>Proposals waiting on a decision</h4>
                <Badge text={String(todo.requests_to_decide.count)} color={todo.requests_to_decide.count ? 'orange' : 'green'} />
                {todo.requests_to_decide.expired_unattended > 0 && (
                  <Badge
                    text={`${todo.requests_to_decide.expired_unattended} expired unanswered`}
                    color="red"
                  />
                )}
              </Stack>
              {todo.requests_to_decide.items.map((r) => (
                <div key={r.request_id} className={styles.row}>
                  <code>{r.action}</code>
                  <span className={styles.reason}>expires {r.expires_ts}</span>
                  <Stack direction="row" gap={1}>
                    <Button size="sm" variant="primary" disabled={decidingId === r.request_id} onClick={() => decide(r.request_id, 'approve')}>
                      Approve
                    </Button>
                    <Button size="sm" variant="destructive" disabled={decidingId === r.request_id} onClick={() => decide(r.request_id, 'reject')}>
                      Reject
                    </Button>
                  </Stack>
                </div>
              ))}
            </div>

            <div className={styles.card}>
              <Stack direction="row" gap={1} alignItems="center">
                <h4 className={styles.h4}>Runs waiting to be labeled</h4>
                <Badge text={String(todo.investigations_to_label.count)} color={todo.investigations_to_label.count ? 'orange' : 'green'} />
                <a className={styles.link} href="../investigations">
                  open Investigations →
                </a>
              </Stack>
              {todo.investigations_to_label.items.map((r) => (
                <div key={r.fp + r.ts} className={styles.row}>
                  <strong>{r.alertname || 'question'}</strong>
                  {r.service && <Badge text={r.service} color="blue" />}
                  {/* The band the gate actually reads. A run below it can be
                      labeled all day without moving anything. */}
                  <Badge
                    text={`confidence ${(r.confidence * 100).toFixed(0)}%`}
                    color={r.confidence >= 0.8 ? 'green' : 'orange'}
                  />
                  <span className={styles.reason}>{r.summary}</span>
                  <Stack direction="row" gap={1}>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={labelingFp === r.fp}
                      onClick={() => label(r.fp, true)}
                    >
                      Correct
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={labelingFp === r.fp}
                      onClick={() => setWrongModal({ fp: r.fp, errorDimension: 'root_cause', correctionNote: '' })}
                    >
                      Wrong
                    </Button>
                  </Stack>
                </div>
              ))}
            </div>

            <div className={styles.card}>
              <Stack direction="row" gap={1} alignItems="center">
                <h4 className={styles.h4}>Actions waiting on a verdict</h4>
                <Badge
                  text={String(todo.actions_to_grade.count)}
                  color={todo.actions_to_grade.count ? 'orange' : 'green'}
                />
                <span className={styles.reason}>
                  these ran; until somebody says whether the incident ended, they are missing from
                  the effectiveness ratio rather than counted against it
                </span>
              </Stack>
              {todo.actions_to_grade.items.map((a) => (
                <div key={a.request_id} className={styles.row}>
                  <code>{a.action}</code>
                  <Badge text={a.status} color={a.status === 'succeeded' ? 'blue' : 'red'} />
                  {/* A rehearsal is graded too, but into its own ratio. */}
                  {a.drill && <Badge text="drill" color="purple" />}
                  <span className={styles.reason}>ran {a.created_ts}</span>
                  <Stack direction="row" gap={1}>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={gradingId === a.request_id}
                      onClick={() => grade(a.request_id, true, false)}
                    >
                      Incident ended
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={gradingId === a.request_id}
                      onClick={() => grade(a.request_id, true, true)}
                    >
                      Ended, broke something
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={gradingId === a.request_id}
                      onClick={() => grade(a.request_id, false, false)}
                    >
                      Still open
                    </Button>
                  </Stack>
                </div>
              ))}
            </div>

            <div className={styles.card}>
              <Stack direction="row" gap={1} alignItems="center">
                <h4 className={styles.h4}>Incidents with no root cause</h4>
                <Badge text={String(todo.cases_to_label.count)} color={todo.cases_to_label.count ? 'orange' : 'green'} />
                <a className={styles.link} href="../cases">
                  open Cases →
                </a>
              </Stack>
              {todo.cases_to_label.items.map((c) => (
                <div key={c.case_key} className={styles.row}>
                  <strong>{c.alertname || '(no alertname)'}</strong>
                  {c.service && <Badge text={c.service} color="blue" />}
                  <Badge text={`seen ${c.occurrences}×`} color="purple" />
                  <span className={styles.reason}>{c.symptom}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {wrongModal && (
        <Modal title="Mark investigation as wrong" isOpen onDismiss={() => setWrongModal(null)}>
          <div className={styles.modalBody}>
            <Field label="Which part was incorrect?">
              <RadioButtonGroup
                options={ERROR_DIMENSION_OPTIONS}
                value={wrongModal.errorDimension}
                onChange={(v) => setWrongModal((prev) => (prev ? { ...prev, errorDimension: v } : prev))}
              />
            </Field>
            <Field
              label="Correction note (optional)"
              description="This is injected into a re-investigation, so a correction that is itself wrong sends the agent down a wrong path and every run that follows gets marked wrong for obeying it. Say only what you can support."
            >
              <Input
                placeholder="e.g. the flag flip, not the deploy, is what broke it"
                value={wrongModal.correctionNote}
                onChange={(e) => setWrongModal((prev) => (prev ? { ...prev, correctionNote: e.currentTarget.value } : prev))}
              />
            </Field>
            <Stack direction="row" gap={1} justifyContent="flex-end">
              <Button variant="secondary" onClick={() => setWrongModal(null)}>
                Cancel
              </Button>
              <Button variant="destructive" icon="sync" onClick={submitWrong}>
                Mark Wrong &amp; Re-investigate
              </Button>
            </Stack>
          </div>
        </Modal>
      )}
    </PluginPage>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrapper: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(2)};
  `,
  intro: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.sm};
    max-width: 70ch;
  `,
  card: css`
    background: ${theme.colors.background.secondary};
    border: 1px solid ${theme.colors.border.weak};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(1.5)};
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
  `,
  h4: css`
    margin: 0;
  `,
  row: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(1)};
    font-size: ${theme.typography.size.sm};
  `,
  table: css`
    font-size: ${theme.typography.size.sm};
    td {
      padding: 2px ${theme.spacing(2)} 2px 0;
    }
  `,
  ok: css`
    color: ${theme.colors.success.text};
  `,
  bad: css`
    color: ${theme.colors.error.text};
  `,
  reason: css`
    color: ${theme.colors.text.secondary};
  `,
  modalBody: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(2)};
  `,
  link: css`
    font-size: ${theme.typography.size.sm};
    margin-left: auto;
  `,
});

export default TodoPage;
