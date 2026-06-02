import React, { useCallback, useEffect, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2, SelectableValue } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Alert, Button, Input, Select, Spinner, useStyles2 } from '@grafana/ui';
import { AiAnalysisBanner } from '../components/trace/AiAnalysisBanner';
import { TraceChat } from '../components/trace/TraceChat';
import { TraceTree } from '../components/trace/TraceTree';
import { Rollup, TraceDetail, TraceSummary } from '../components/trace/types';

type Props = { agentServiceUrl: string };

const SERVICE_OPTIONS: Array<SelectableValue<string>> = [
  { label: 'aiops-agent (LLM)', value: 'aiops-agent' },
  { label: 'webapp', value: 'webapp' },
  { label: 'api-gateway', value: 'api-gateway' },
  { label: 'order-service', value: 'order-service' },
  { label: 'payment-service', value: 'payment-service' },
  { label: 'user-service', value: 'user-service' },
  { label: '(all services)', value: '' },
];

const RANGE_OPTIONS: Array<SelectableValue<string>> = [
  { label: 'Last 15m', value: 'now-15m' },
  { label: 'Last 1h', value: 'now-1h' },
  { label: 'Last 6h', value: 'now-6h' },
  { label: 'Last 24h', value: 'now-24h' },
];

function RollupChips({ rollup }: { rollup: Rollup }) {
  const styles = useStyles2(getStyles);
  const chips: Array<[string, string]> = [
    ['spans', String(rollup.span_count)],
    ['LLM calls', String(rollup.llm_calls)],
    ['tools', String(rollup.tool_calls)],
    ['tokens', `${rollup.total_tokens} (${rollup.input_tokens}→${rollup.output_tokens})`],
  ];
  if (rollup.cache_read_tokens) {
    chips.push(['cached', `${rollup.cache_read_tokens}↺`]);
  }
  if (rollup.cost != null) {
    chips.push(['cost', `$${rollup.cost.toFixed(6)}`]);
  }
  if (rollup.error_count) {
    chips.push(['errors', String(rollup.error_count)]);
  }
  return (
    <div className={styles.chips}>
      {chips.map(([k, v]) => (
        <span key={k} className={styles.chip}>
          <span className={styles.chipKey}>{k}</span>
          <span className={styles.chipVal}>{v}</span>
        </span>
      ))}
    </div>
  );
}

function TraceExplorerPage({ agentServiceUrl }: Props) {
  const styles = useStyles2(getStyles);
  const [service, setService] = useState<string>('aiops-agent');
  const [range, setRange] = useState<string>('now-1h');
  const [traceql, setTraceql] = useState<string>('');
  const [list, setList] = useState<TraceSummary[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadList = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const params = new URLSearchParams({ start: range, end: 'now', limit: '40' });
      if (traceql.trim()) {
        params.set('q', traceql.trim());
      } else if (service) {
        params.set('service', service);
      }
      const res = await fetch(`${agentServiceUrl}/traces?${params}`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      setList(data.traces ?? []);
    } catch (e) {
      setListError(e instanceof Error ? e.message : String(e));
      setList([]);
    } finally {
      setListLoading(false);
    }
  }, [agentServiceUrl, service, range, traceql]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    fetch(`${agentServiceUrl}/traces/${selected}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null))
      .finally(() => !cancelled && setDetailLoading(false));
    return () => {
      cancelled = true;
    };
  }, [agentServiceUrl, selected]);

  return (
    <PluginPage>
      <div className={styles.filters}>
        <Select
          width={24}
          options={SERVICE_OPTIONS}
          value={service}
          onChange={(v) => setService(v.value ?? '')}
          disabled={!!traceql.trim()}
        />
        <Select width={18} options={RANGE_OPTIONS} value={range} onChange={(v) => setRange(v.value ?? 'now-1h')} />
        <Input
          value={traceql}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTraceql(e.currentTarget.value)}
          placeholder='Optional TraceQL, e.g. { status = error }'
        />
        <Button variant="secondary" onClick={loadList}>
          Refresh
        </Button>
      </div>

      <div className={styles.layout}>
        <div className={styles.listPane}>
          {listLoading && <Spinner inline />}
          {listError && (
            <Alert title="Trace search failed" severity="error">
              {listError}
            </Alert>
          )}
          {!listLoading && !listError && list.length === 0 && <div className={styles.muted}>No traces in range.</div>}
          {list.map((t) => (
            <div
              key={t.traceID}
              className={selected === t.traceID ? styles.listItemActive : styles.listItem}
              onClick={() => setSelected(t.traceID)}
              role="button"
            >
              <div className={styles.listTitle}>{t.rootTraceName ?? t.traceID.slice(0, 12)}</div>
              <div className={styles.listMeta}>
                <span>{t.rootServiceName}</span>
                {t.durationMs != null ? <span>{t.durationMs}ms</span> : null}
              </div>
            </div>
          ))}
        </div>

        <div className={styles.centerPane}>
          {!selected && <div className={styles.muted}>Select a trace on the left.</div>}
          {selected && detailLoading && <Spinner inline />}
          {selected && detail && (
            <>
              <AiAnalysisBanner agentServiceUrl={agentServiceUrl} traceId={selected} />
              <RollupChips rollup={detail.rollup} />
              <div className={styles.treeScroll}>
                <TraceTree roots={detail.roots} />
              </div>
            </>
          )}
        </div>

        <div className={styles.chatPane}>
          {selected ? (
            <TraceChat agentServiceUrl={agentServiceUrl} traceId={selected} />
          ) : (
            <div className={styles.muted}>Pick a trace to ask about it.</div>
          )}
        </div>
      </div>
    </PluginPage>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  filters: css`
    display: flex;
    gap: ${theme.spacing(1)};
    align-items: center;
    margin-bottom: ${theme.spacing(1.5)};
  `,
  layout: css`
    display: grid;
    grid-template-columns: 280px 1fr 360px;
    gap: ${theme.spacing(1.5)};
    height: calc(100vh - 220px);
    min-height: 0;
  `,
  listPane: css`
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(0.5)};
    padding-right: ${theme.spacing(0.5)};
  `,
  listItem: css`
    padding: ${theme.spacing(1)};
    border-radius: ${theme.shape.radius.default};
    background: ${theme.colors.background.secondary};
    cursor: pointer;
    &:hover {
      background: ${theme.colors.action.hover};
    }
  `,
  listItemActive: css`
    padding: ${theme.spacing(1)};
    border-radius: ${theme.shape.radius.default};
    background: ${theme.colors.action.selected};
    border-left: 3px solid ${theme.colors.primary.main};
    cursor: pointer;
  `,
  listTitle: css`
    font-weight: ${theme.typography.fontWeightMedium};
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  `,
  listMeta: css`
    display: flex;
    justify-content: space-between;
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.xs};
  `,
  centerPane: css`
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
    border-left: 1px solid ${theme.colors.border.weak};
    border-right: 1px solid ${theme.colors.border.weak};
    padding: 0 ${theme.spacing(1.5)};
    min-height: 0;
  `,
  treeScroll: css`
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  `,
  chatPane: css`
    min-height: 0;
    overflow: hidden;
  `,
  chips: css`
    display: flex;
    flex-wrap: wrap;
    gap: ${theme.spacing(0.5)};
  `,
  chip: css`
    display: inline-flex;
    gap: ${theme.spacing(0.5)};
    align-items: baseline;
    background: ${theme.colors.background.secondary};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(0.25, 0.75)};
    font-size: ${theme.typography.bodySmall.fontSize};
  `,
  chipKey: css`
    color: ${theme.colors.text.secondary};
  `,
  chipVal: css`
    font-variant-numeric: tabular-nums;
    font-weight: ${theme.typography.fontWeightMedium};
  `,
  muted: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    padding: ${theme.spacing(2)};
  `,
});

export default TraceExplorerPage;
