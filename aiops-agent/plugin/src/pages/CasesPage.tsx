import React, { useCallback, useEffect, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Alert, Badge, BadgeColor, Button, Drawer, Field, RadioButtonGroup, Spinner, Stack, TextArea, useStyles2 } from '@grafana/ui';

type Props = { agentServiceUrl: string };

export type Case = {
  case_key: string;
  first_ts: string;
  last_ts: string;
  alertname: string | null;
  service: string | null;
  symptom: string;
  occurrences: number;
  root_cause: string | null;
  // Who says so. `self` never earns recall — the agent vouching for its own
  // conclusion is what this column exists to keep out.
  root_cause_source: string | null;
  confirmed_run_id: string | null;
  resolution: { action?: string; args?: Record<string, unknown>; outcome?: string } | null;
  status: string;
};

type CaseRun = {
  run_id: string | null;
  fp: string;
  ts: string;
  correct: number | null;
  grading_mode: string | null;
  error_dimension: string | null;
};

type DeadEnd = {
  id: number;
  kind: string;
  subject: string;
  evidence: string;
  disproved_by: string;
  still_valid: number;
  ts: string;
};

type CaseDetail = {
  case: Case;
  // Whether this case actually reaches a prompt. A case can hold a root cause
  // and still be invisible to recall (untrusted source, aged out, marked a false
  // positive), and a UI that showed only the root cause would describe memory
  // the agent does not have.
  recallable: boolean;
  runs: CaseRun[];
  dead_ends: DeadEnd[];
};

const STATUS_COLOR: Record<string, BadgeColor> = {
  open: 'orange',
  resolved: 'green',
  recurring: 'purple',
  false_positive: 'red',
};

const FILTERS = [
  { label: 'All', value: 'all' },
  { label: 'Needs a root cause', value: 'unlabeled' },
  { label: 'Recalled', value: 'labeled' },
];

function CasesPage({ agentServiceUrl }: Props) {
  const styles = useStyles2(getStyles);
  const [items, setItems] = useState<Case[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState('all');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [forgetting, setForgetting] = useState(false);
  const [rootCauseDraft, setRootCauseDraft] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const q = filter === 'all' ? '' : `&unlabeled=${filter === 'unlabeled'}`;
      const res = await fetch(`${agentServiceUrl}/cases?limit=100${q}`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      setItems(data.cases ?? []);
      setTotal(data.total ?? 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [agentServiceUrl, filter]);

  useEffect(() => {
    // Same initial-fetch shape as the other pages here. The rule objects to the
    // synchronous setState inside the effect; the alternative is a spinner that
    // flickers on every filter change, so it stays and is silenced explicitly.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  const open = useCallback(
    async (key: string) => {
      setDetailLoading(true);
      try {
        const res = await fetch(`${agentServiceUrl}/cases/${encodeURIComponent(key)}`);
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const d: CaseDetail = await res.json();
        setDetail(d);
        setRootCauseDraft(d.case.root_cause ?? '');
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setDetailLoading(false);
      }
    },
    [agentServiceUrl]
  );

  // The half of case memory that was missing: everything else could write a
  // root cause — the grader, the eval harness, the label path on a run — but the
  // person looking straight at the case could not, so this queue never moved.
  const saveRootCause = useCallback(
    async (key: string, runId: string | null) => {
      setSaving(true);
      try {
        const res = await fetch(`${agentServiceUrl}/cases/${encodeURIComponent(key)}/root-cause`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ root_cause: rootCauseDraft, run_id: runId, actor: 'operator' }),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        await open(key);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSaving(false);
      }
    },
    [agentServiceUrl, rootCauseDraft, open, load]
  );

  // The button for saying the ground moved: an environment was rebuilt, a policy
  // changed, a diagnosis turned out to be wrong. It retracts what the case
  // claims to know without pretending the incident never happened.
  const forget = useCallback(
    async (key: string) => {
      setForgetting(true);
      try {
        const res = await fetch(`${agentServiceUrl}/cases/${encodeURIComponent(key)}/forget`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ actor: 'operator' }),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        setDetail(null);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setForgetting(false);
      }
    },
    [agentServiceUrl, load]
  );

  return (
    <PluginPage>
      <div className={styles.wrapper}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <div className={styles.intro}>
            What the agent has learned from past incidents. A case is one incident across every time
            it fired — the key survives a redeploy on purpose — and it only becomes precedent once
            somebody who is not the agent says what caused it.
          </div>
          <Button variant="secondary" icon="sync" onClick={load} disabled={loading}>
            Refresh
          </Button>
        </Stack>

        <Stack direction="row" gap={1} alignItems="center">
          <RadioButtonGroup options={FILTERS} value={filter} onChange={setFilter} />
          <span className={styles.count}>{total} case(s)</span>
        </Stack>

        {error && (
          <Alert title="Could not load cases" severity="error">
            {error}
          </Alert>
        )}
        {loading && <Spinner inline />}
        {!loading && items.length === 0 && (
          <div className={styles.empty}>
            No cases yet. Every alert-driven investigation records one; they show up here as soon as
            case memory is enabled.
          </div>
        )}

        <div className={styles.list}>
          {items.map((c) => (
            <div key={c.case_key} className={styles.card} onClick={() => open(c.case_key)} role="button" tabIndex={0}>
              <div className={styles.cardHead}>
                <Stack direction="row" gap={1} alignItems="center" wrap="wrap">
                  <strong>{c.alertname || '(no alertname)'}</strong>
                  {c.service && <Badge text={c.service} color="blue" />}
                  <Badge text={c.status} color={STATUS_COLOR[c.status] ?? 'darkgrey'} />
                  <Badge text={`seen ${c.occurrences}×`} color="purple" />
                  {c.root_cause_source && <Badge text={`cause by ${c.root_cause_source}`} color="green" />}
                  {!c.root_cause && <Badge text="no root cause yet" color="orange" />}
                </Stack>
                <span className={styles.ts}>{c.last_ts}</span>
              </div>
              <div className={styles.summary}>{c.root_cause || c.symptom || '(nothing recorded yet)'}</div>
              {c.resolution?.action && (
                <div className={styles.reason}>fixed by: <code>{c.resolution.action}</code> — {c.resolution.outcome}</div>
              )}
            </div>
          ))}
        </div>
      </div>

      {(detail || detailLoading) && (
        <Drawer title="Case" onClose={() => setDetail(null)} size="md">
          {detailLoading && <Spinner inline />}
          {detail && (
            <div className={styles.wrapper}>
              <Stack direction="row" gap={1} alignItems="center" wrap="wrap">
                <strong>{detail.case.alertname || '(no alertname)'}</strong>
                {detail.case.service && <Badge text={detail.case.service} color="blue" />}
                <Badge text={detail.case.status} color={STATUS_COLOR[detail.case.status] ?? 'darkgrey'} />
                <Badge
                  text={detail.recallable ? 'recalled by the next run' : 'not recalled'}
                  color={detail.recallable ? 'green' : 'orange'}
                />
              </Stack>
              <div className={styles.reason}>
                {detail.recallable
                  ? 'The next investigation of this service is shown this case.'
                  : 'Nothing here reaches a prompt yet — a case is recalled only with a root cause or a recorded fix from a trusted source, inside the freshness window, and never when it is marked a false positive.'}
              </div>

              <section>
                <h4>Root cause</h4>
                {detail.case.root_cause_source && (
                  <div className={styles.reason}>
                    said by {detail.case.root_cause_source}
                    {detail.case.confirmed_run_id ? ` on run ${detail.case.confirmed_run_id}` : ''}
                  </div>
                )}
                <Field
                  label={detail.case.root_cause ? 'What caused it' : 'Nobody has said what caused this yet'}
                  description="Written down as a human verdict. It becomes what the next investigation of this service is told — so say what was actually wrong, not what was proposed."
                >
                  <TextArea
                    rows={3}
                    placeholder="e.g. the session cache was disabled by a flag flip, so every auth lookup hit the cold path"
                    value={rootCauseDraft}
                    onChange={(e) => setRootCauseDraft(e.currentTarget.value)}
                  />
                </Field>
                <Stack direction="row" gap={1}>
                  <Button
                    variant="primary"
                    icon="save"
                    disabled={saving || !rootCauseDraft.trim() || rootCauseDraft === detail.case.root_cause}
                    onClick={() => saveRootCause(detail.case.case_key, detail.runs[0]?.run_id ?? null)}
                  >
                    {saving ? 'Saving…' : 'Save root cause'}
                  </Button>
                </Stack>
              </section>

              <section>
                <h4>Runs ({detail.runs.length})</h4>
                {detail.runs.map((r) => (
                  <div key={`${r.run_id}-${r.ts}`} className={styles.row}>
                    <code>{r.run_id || r.fp}</code>
                    <span className={styles.ts}>{r.ts}</span>
                    {r.correct === null && <Badge text="unlabelled" color="orange" />}
                    {r.correct === 1 && <Badge text="correct" color="green" />}
                    {r.correct === 0 && <Badge text={`wrong${r.error_dimension ? ` (${r.error_dimension})` : ''}`} color="red" />}
                  </div>
                ))}
              </section>

              <section>
                <h4>Ruled out ({detail.dead_ends.length})</h4>
                <div className={styles.reason}>
                  The paths that did not work. Retired entries are kept and shown struck through —
                  &quot;this was ruled out and later un-ruled-out&quot; is the part worth auditing.
                </div>
                {detail.dead_ends.map((d) => (
                  <div key={d.id} className={d.still_valid ? styles.row : styles.rowRetired}>
                    <Badge text={d.kind} color="blue" />
                    <code>{d.subject}</code>
                    <span className={styles.reason}>— {d.evidence || `disproved by ${d.disproved_by}`}</span>
                  </div>
                ))}
              </section>

              <Stack direction="row" justifyContent="flex-end">
                <Button variant="destructive" icon="trash-alt" disabled={forgetting} onClick={() => forget(detail.case.case_key)}>
                  {forgetting ? 'Forgetting…' : 'Forget what this case claims'}
                </Button>
              </Stack>
            </div>
          )}
        </Drawer>
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
  count: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.sm};
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
    cursor: pointer;
    &:hover {
      border-color: ${theme.colors.border.medium};
    }
  `,
  cardHead: css`
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: ${theme.spacing(1)};
  `,
  row: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(1)};
    font-size: ${theme.typography.size.sm};
    padding: 2px 0;
  `,
  rowRetired: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(1)};
    font-size: ${theme.typography.size.sm};
    padding: 2px 0;
    text-decoration: line-through;
    color: ${theme.colors.text.disabled};
  `,
  ts: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.xs};
    white-space: nowrap;
  `,
  summary: css`
    font-size: ${theme.typography.size.md};
  `,
  reason: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.sm};
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    text-align: center;
    padding: ${theme.spacing(4)};
  `,
});

export default CasesPage;
