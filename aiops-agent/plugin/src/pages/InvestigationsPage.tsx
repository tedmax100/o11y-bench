import React, { useCallback, useEffect, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Alert, Badge, BadgeColor, Button, Field, Input, Modal, RadioButtonGroup, Spinner, Stack, useStyles2 } from '@grafana/ui';

type Props = { agentServiceUrl: string };

type Decision = {
  action: string;
  autonomy: 'auto' | 'propose' | 'escalate';
  reason: string;
  requires_human: boolean;
};

type Investigation = {
  fp: string;
  ts: string;
  alertname: string | null;
  service: string | null;
  git_version: string | null;
  summary: string;
  hypothesis: string;
  confidence: number;
  suspected_version: string | null;
  services: string[];
  decisions: Decision[];
  answer: string;
  correct: boolean | null;
  source?: 'alert' | 'chat';
  trace_id?: string | null;
};

// What the executor's read-only dry-run predicted, stored with the proposal so
// the person approving it can see the size before they agree to it.
type BlastRadius = {
  affected_pods?: number;
  current_revision?: string | null;
  target_revision?: string | null;
  namespace?: string;
  policy_ok?: boolean;
  policy_reason?: string;
};

type ActionRequest = {
  request_id: string;
  fp: string;
  action: string;
  status: string;
  blast_radius?: BlastRadius | null;
};

type WrongModalState = {
  fp: string;
  errorDimension: string;
  correctionNote: string;
};

const AUTONOMY_COLOR: Record<Decision['autonomy'], BadgeColor> = {
  auto: 'green',
  propose: 'orange',
  escalate: 'red',
};

const ERROR_DIMENSION_OPTIONS = [
  { label: 'Root cause', value: 'root_cause', description: 'Wrong root cause identified' },
  { label: 'Scope', value: 'scope', description: 'Wrong service or affected scope' },
  { label: 'Action', value: 'action', description: 'Proposed remediation was wrong' },
  { label: 'Other', value: 'other', description: 'Other issue' },
];

function confidenceColor(c: number): BadgeColor {
  if (c >= 0.8) {
    return 'green';
  }
  if (c >= 0.5) {
    return 'orange';
  }
  return 'red';
}

function InvestigationsPage({ agentServiceUrl }: Props) {
  const styles = useStyles2(getStyles);
  const [items, setItems] = useState<Investigation[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [wrongModal, setWrongModal] = useState<WrongModalState | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [reinvestigatingFps, setReinvestigatingFps] = useState<Set<string>>(new Set());
  const [proposals, setProposals] = useState<Record<string, ActionRequest[]>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${agentServiceUrl}/investigations?limit=50`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      setItems(data.investigations ?? []);

      // Proposals are a separate resource; index them by fingerprint so each
      // investigation can show what it proposed AND how big that action is.
      try {
        const ar = await fetch(`${agentServiceUrl}/actions/requests?limit=100`);
        if (ar.ok) {
          const rows: ActionRequest[] = (await ar.json()).requests ?? [];
          const byFp: Record<string, ActionRequest[]> = {};
          rows.forEach((r) => {
            byFp[r.fp] = [...(byFp[r.fp] ?? []), r];
          });
          setProposals(byFp);
        }
      } catch {
        // A missing footprint must not cost the list itself.
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [agentServiceUrl]);

  useEffect(() => {
    load();
  }, [load]);

  const label = useCallback(
    async (fp: string, correct: boolean, errorDimension?: string, correctionNote?: string) => {
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
        const data = await res.json();
        setItems((prev) => prev.map((it) => (it.fp === fp ? { ...it, correct } : it)));
        if (data.reinvestigating) {
          setReinvestigatingFps((prev) => new Set(prev).add(fp));
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [agentServiceUrl]
  );

  const openWrongModal = useCallback((fp: string) => {
    setWrongModal({ fp, errorDimension: 'root_cause', correctionNote: '' });
  }, []);

  const submitWrong = useCallback(async () => {
    if (!wrongModal) {
      return;
    }
    setSubmitting(true);
    await label(wrongModal.fp, false, wrongModal.errorDimension, wrongModal.correctionNote);
    setSubmitting(false);
    setWrongModal(null);
  }, [wrongModal, label]);

  return (
    <PluginPage>
      <div className={styles.wrapper}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <div className={styles.intro}>
            Alert-driven RCA runs — what the agent investigated on its own, its conclusion, and the
            governance gate&apos;s decision. Mark each correct/wrong to feed calibration (CE).
          </div>
          <Button variant="secondary" icon="sync" onClick={load} disabled={loading}>
            Refresh
          </Button>
        </Stack>

        {error && (
          <Alert title="Could not load investigations" severity="error">
            {error}
          </Alert>
        )}
        {loading && <Spinner inline />}
        {!loading && items.length === 0 && (
          <div className={styles.empty}>
            No investigations yet. Fire a Grafana alert at <code>/webhook/alert</code> and the
            headless RCA results will show up here.
          </div>
        )}

        <div className={styles.list}>
          {items.map((it) => (
            <div key={it.fp + it.ts} className={styles.card}>
              <div className={styles.cardHead}>
                <Stack direction="row" gap={1} alignItems="center" wrap="wrap">
                  <strong>{it.alertname || (it.source === 'chat' ? 'question' : 'alert')}</strong>
                  {it.source === 'chat' && <Badge text="chat" color="purple" />}
                  {it.service && <Badge text={it.service} color="blue" />}
                  {it.git_version && <Badge text={it.git_version} color="purple" />}
                  <Badge text={`confidence ${(it.confidence * 100).toFixed(0)}%`} color={confidenceColor(it.confidence)} />
                  {it.correct === true && <Badge text="verified ✓" color="green" />}
                  {it.correct === false && !reinvestigatingFps.has(it.fp) && <Badge text="wrong ✗" color="red" />}
                  {it.correct === false && reinvestigatingFps.has(it.fp) && <Badge text="re-investigating…" color="orange" />}
                </Stack>
                <Stack direction="row" gap={1} alignItems="center">
                  {it.trace_id && (
                    <a
                      className={styles.traceLink}
                      href={`../traces?trace=${it.trace_id}`}
                      title="Every node, tool call and prompt of this run"
                    >
                      看它怎麼想的
                    </a>
                  )}
                  <span className={styles.ts}>{it.ts}</span>
                </Stack>
              </div>

              <div className={styles.summary}>{it.summary || '(no conclusion)'}</div>

              {it.decisions.length > 0 && (
                <div className={styles.decisions}>
                  {it.decisions.map((d, i) => {
                    const req = (proposals[it.fp] ?? []).find((r) => r.action === d.action);
                    const br = req?.blast_radius;
                    return (
                      <div key={i} className={styles.decision}>
                        <Stack direction="row" gap={1} alignItems="center" wrap="wrap">
                          <Badge text={d.autonomy.toUpperCase()} color={AUTONOMY_COLOR[d.autonomy]} />
                          <code>{d.action}</code>
                          <span className={styles.reason}>— {d.reason}</span>
                        </Stack>
                        {br && (
                          <div className={styles.footprint}>
                            <Badge
                              text={`${br.affected_pods ?? '?'} pod(s)`}
                              color={br.policy_ok === false ? 'red' : 'blue'}
                            />
                            {br.current_revision && (
                              <Badge
                                text={`revision ${br.current_revision} → ${br.target_revision ?? '?'}`}
                                color="purple"
                              />
                            )}
                            {br.namespace && <Badge text={`ns ${br.namespace}`} color="blue" />}
                            <span className={styles.reason}>{br.policy_reason}</span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              <Stack direction="row" gap={1} alignItems="center">
                <span className={styles.labelPrompt}>Was this correct?</span>
                <Button
                  size="sm"
                  variant={it.correct === true ? 'primary' : 'secondary'}
                  fill="outline"
                  icon="check"
                  onClick={() => label(it.fp, true)}
                >
                  Correct
                </Button>
                <Button
                  size="sm"
                  variant={it.correct === false ? 'destructive' : 'secondary'}
                  fill="outline"
                  icon="times"
                  onClick={() => openWrongModal(it.fp)}
                >
                  Wrong
                </Button>
                <span className={styles.fp}>fp: {it.fp}</span>
              </Stack>
            </div>
          ))}
        </div>
      </div>

      {wrongModal && (
        <Modal
          title="What was wrong with this RCA?"
          isOpen
          onDismiss={() => setWrongModal(null)}
        >
          <div className={styles.modalBody}>
            <Field label="Which part was incorrect?">
              <RadioButtonGroup
                options={ERROR_DIMENSION_OPTIONS}
                value={wrongModal.errorDimension}
                onChange={(v) => setWrongModal((prev) => prev ? { ...prev, errorDimension: v } : prev)}
              />
            </Field>
            <Field label="Correction note (optional)" description="Tell the agent what was actually wrong so it can re-investigate with better context.">
              <Input
                placeholder="e.g. Root cause was DB connection exhaustion, not a code regression"
                value={wrongModal.correctionNote}
                onChange={(e) => setWrongModal((prev) => prev ? { ...prev, correctionNote: e.currentTarget.value } : prev)}
              />
            </Field>
            <Stack direction="row" gap={1} justifyContent="flex-end">
              <Button variant="secondary" onClick={() => setWrongModal(null)}>
                Cancel
              </Button>
              <Button variant="destructive" icon="sync" onClick={submitWrong} disabled={submitting}>
                {submitting ? 'Submitting…' : 'Mark Wrong & Re-investigate'}
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
  list: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1.5)};
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
  cardHead: css`
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: ${theme.spacing(1)};
  `,
  ts: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.xs};
    white-space: nowrap;
  `,
  summary: css`
    font-size: ${theme.typography.size.md};
  `,
  traceLink: css`
    font-size: 12px;
    white-space: nowrap;
  `,
  footprint: css`
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-top: 4px;
    padding-left: 8px;
  `,
  decisions: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(0.5)};
    border-left: 2px solid ${theme.colors.border.medium};
    padding-left: ${theme.spacing(1)};
  `,
  decision: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(1)};
    font-size: ${theme.typography.size.sm};
  `,
  reason: css`
    color: ${theme.colors.text.secondary};
  `,
  labelPrompt: css`
    font-size: ${theme.typography.size.sm};
    color: ${theme.colors.text.secondary};
  `,
  fp: css`
    margin-left: auto;
    color: ${theme.colors.text.disabled};
    font-size: ${theme.typography.size.xs};
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    text-align: center;
    padding: ${theme.spacing(4)};
  `,
  modalBody: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(2)};
  `,
});

export default InvestigationsPage;
