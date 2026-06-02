import React, { useEffect, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { Icon, Spinner, useStyles2 } from '@grafana/ui';

// Fetches the one-shot "AI analysis" verdict for a trace. Loaded separately
// from the tree so the tree paints immediately and this fills in when ready.

export function AiAnalysisBanner({ agentServiceUrl, traceId }: { agentServiceUrl: string; traceId: string }) {
  const styles = useStyles2(getStyles);
  const [text, setText] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setText(null);
    fetch(`${agentServiceUrl}/traces/${traceId}/analysis`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (!cancelled) {
          setText(d.analysis ?? '');
        }
      })
      .catch(() => {
        if (!cancelled) {
          setText(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [agentServiceUrl, traceId]);

  return (
    <div className={styles.banner}>
      <Icon name="info-circle" className={styles.icon} />
      <span className={styles.label}>AI analysis</span>
      {loading ? <Spinner inline size="sm" /> : <span className={styles.text}>{text || '—'}</span>}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  banner: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(1)};
    padding: ${theme.spacing(1, 1.5)};
    background: ${theme.colors.background.secondary};
    border-radius: ${theme.shape.radius.default};
    border-left: 3px solid ${theme.colors.primary.main};
  `,
  icon: css`
    color: ${theme.colors.primary.text};
  `,
  label: css`
    font-weight: ${theme.typography.fontWeightMedium};
    color: ${theme.colors.text.secondary};
    white-space: nowrap;
  `,
  text: css`
    color: ${theme.colors.text.primary};
  `,
});
