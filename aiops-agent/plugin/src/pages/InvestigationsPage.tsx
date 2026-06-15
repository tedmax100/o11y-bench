import React, { useCallback, useEffect, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Alert, Badge, BadgeColor, Button, Spinner, Stack, useStyles2 } from '@grafana/ui';

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
};

const AUTONOMY_COLOR: Record<Decision['autonomy'], BadgeColor> = {
  auto: 'green',
  propose: 'orange',
  escalate: 'red',
};

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
    async (fp: string, correct: boolean) => {
      try {
        const res = await fetch(`${agentServiceUrl}/investigations/${fp}/label`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ correct }),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        setItems((prev) => prev.map((it) => (it.fp === fp ? { ...it, correct } : it)));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [agentServiceUrl]
  );

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
                  <strong>{it.alertname || 'alert'}</strong>
                  {it.service && <Badge text={it.service} color="blue" />}
                  {it.git_version && <Badge text={it.git_version} color="purple" />}
                  <Badge text={`confidence ${(it.confidence * 100).toFixed(0)}%`} color={confidenceColor(it.confidence)} />
                  {it.correct === true && <Badge text="verified ✓" color="green" />}
                  {it.correct === false && <Badge text="wrong ✗" color="red" />}
                </Stack>
                <span className={styles.ts}>{it.ts}</span>
              </div>

              <div className={styles.summary}>{it.summary || '(no conclusion)'}</div>

              {it.decisions.length > 0 && (
                <div className={styles.decisions}>
                  {it.decisions.map((d, i) => (
                    <div key={i} className={styles.decision}>
                      <Badge text={d.autonomy.toUpperCase()} color={AUTONOMY_COLOR[d.autonomy]} />
                      <code>{d.action}</code>
                      <span className={styles.reason}>— {d.reason}</span>
                    </div>
                  ))}
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
                  onClick={() => label(it.fp, false)}
                >
                  Wrong
                </Button>
                <span className={styles.fp}>fp: {it.fp}</span>
              </Stack>
            </div>
          ))}
        </div>
      </div>
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
});

export default InvestigationsPage;
