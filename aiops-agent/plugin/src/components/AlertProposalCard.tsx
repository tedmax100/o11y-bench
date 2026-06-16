import React, { useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { Alert, Badge, Button, useStyles2 } from '@grafana/ui';
import { testIds } from './testIds';

// The agent's ```alert``` proposal, parsed from JSON. Mirrors the service-side
// AlertSpec — only title/expr/threshold are guaranteed; the rest have defaults.
export type AlertSpec = {
  title: string;
  expr: string;
  threshold: number;
  comparison?: 'gt' | 'lt';
  for_duration?: string;
  severity?: string;
  summary?: string;
  service_name?: string;
};

type Props = {
  spec: AlertSpec;
  agentServiceUrl: string;
};

// Renders an agent-proposed alert rule as a card with a "Create alert" button.
// The agent only ever *proposes* (emits the block); provisioning happens solely
// on this click — the human-in-the-loop gate for the first side-effecting
// capability. POSTs the spec to the service, which writes it to Grafana.
export function AlertProposalCard({ spec, agentServiceUrl }: Props) {
  const styles = useStyles2(getStyles);
  const [state, setState] = useState<'idle' | 'creating' | 'done' | 'error'>('idle');
  const [detail, setDetail] = useState<string | null>(null);

  const cmp = spec.comparison === 'lt' ? '<' : '>';

  const onCreate = async () => {
    setState('creating');
    setDetail(null);
    try {
      const resp = await fetch(`${agentServiceUrl}/alerts/provision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(spec),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${resp.status}`);
      }
      const body = await resp.json();
      setDetail(body?.uid ? `Created (uid ${body.uid})` : 'Created');
      setState('done');
    } catch (e) {
      setDetail(e instanceof Error ? e.message : String(e));
      setState('error');
    }
  };

  return (
    <div className={styles.card}>
      <div className={styles.header}>
        <span className={styles.title}>📟 Proposed alert: {spec.title}</span>
        <Badge text={spec.severity ?? 'warning'} color={spec.severity === 'critical' ? 'red' : 'orange'} />
      </div>
      <div className={styles.meta}>
        Fires when value {cmp} {spec.threshold} for {spec.for_duration ?? '5m'}
        {spec.service_name ? ` · ${spec.service_name}` : ''}
      </div>
      <pre className={styles.expr}>{spec.expr}</pre>
      {spec.summary && <div className={styles.summary}>{spec.summary}</div>}

      {state === 'done' ? (
        <Alert title={detail ?? 'Alert created'} severity="success" />
      ) : (
        <>
          <Button
            size="sm"
            icon={state === 'creating' ? 'fa fa-spinner' : 'bell'}
            disabled={state === 'creating'}
            onClick={onCreate}
            data-testid={testIds.chat.alertCreate}
          >
            {state === 'creating' ? 'Creating…' : 'Create alert'}
          </Button>
          {state === 'error' && detail && (
            <Alert title="Could not create alert" severity="error">
              {detail}
            </Alert>
          )}
        </>
      )}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  card: css`
    margin: ${theme.spacing(1)} 0;
    border: 1px solid ${theme.colors.border.medium};
    border-left: 3px solid ${theme.colors.warning.border};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(1.5)};
    background: ${theme.colors.background.primary};
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
  `,
  header: css`
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: ${theme.spacing(1)};
  `,
  title: css`
    font-weight: ${theme.typography.fontWeightMedium};
  `,
  meta: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.sm};
  `,
  expr: css`
    margin: 0;
    padding: ${theme.spacing(1)};
    background: ${theme.colors.background.secondary};
    border-radius: ${theme.shape.radius.default};
    font-size: ${theme.typography.size.sm};
    white-space: pre-wrap;
    word-break: break-all;
  `,
  summary: css`
    font-size: ${theme.typography.size.sm};
    color: ${theme.colors.text.secondary};
  `,
});
