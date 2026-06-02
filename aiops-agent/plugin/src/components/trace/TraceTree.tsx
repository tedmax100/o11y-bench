import React, { useState } from 'react';
import { css, cx } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { Icon, useStyles2 } from '@grafana/ui';
import { TraceNode } from './types';
import { TraceStep } from './TraceStep';

// Recursive call-tree. Each row shows the span's role/label + (for LLM/tool)
// model, token in→out, cost and duration; clicking a row reveals its detail
// (TraceStep). Children are always nested so the agent→llm→tool structure reads
// top-to-bottom like the source trace.

function fmtCost(c?: number | null): string {
  if (c == null) {
    return '';
  }
  return c < 0.01 ? `$${c.toFixed(6)}` : `$${c.toFixed(4)}`;
}

function TreeRow({ node, depth }: { node: TraceNode; depth: number }) {
  const styles = useStyles2(getStyles);
  const [open, setOpen] = useState(false);
  const isLlmOrTool = node.kind === 'llm' || node.kind === 'tool';

  return (
    <div>
      <div
        className={cx(styles.row, node.error && styles.rowError)}
        style={{ paddingLeft: `${depth * 14 + 8}px` }}
        onClick={() => setOpen((o) => !o)}
        role="button"
      >
        <Icon name={open ? 'angle-down' : 'angle-right'} className={styles.chevron} />
        <span
          className={cx(
            styles.dot,
            { llm: styles.dotLlm, tool: styles.dotTool, http: styles.dotHttp, business: styles.dotBusiness }[
              node.kind
            ]
          )}
        />
        <span className={styles.label}>{node.label}</span>
        {node.model ? <span className={styles.model}>{node.model}</span> : null}
        <span className={styles.spacer} />
        {isLlmOrTool && (node.input_tokens != null || node.output_tokens != null) ? (
          <span className={styles.tok}>
            {node.input_tokens ?? 0}
            {node.cache_read_tokens ? <span className={styles.cache}> ({node.cache_read_tokens}↺)</span> : null}
            {' → '}
            {node.output_tokens ?? 0} tok
          </span>
        ) : null}
        {node.cost != null ? <span className={styles.cost}>{fmtCost(node.cost)}</span> : null}
        {node.duration_ms != null ? <span className={styles.dur}>{node.duration_ms}ms</span> : null}
      </div>
      {open ? <TraceStep node={node} /> : null}
      {node.children.map((c) => (
        <TreeRow key={c.span_id} node={c} depth={depth + 1} />
      ))}
    </div>
  );
}

export function TraceTree({ roots }: { roots: TraceNode[] }) {
  const styles = useStyles2(getStyles);
  if (!roots || roots.length === 0) {
    return <div className={styles.empty}>No spans in this trace.</div>;
  }
  return (
    <div className={styles.tree}>
      {roots.map((r) => (
        <TreeRow key={r.span_id} node={r} depth={0} />
      ))}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  tree: css`
    font-size: ${theme.typography.bodySmall.fontSize};
  `,
  row: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(0.75)};
    padding: ${theme.spacing(0.5, 1)};
    border-radius: ${theme.shape.radius.default};
    cursor: pointer;
    &:hover {
      background: ${theme.colors.action.hover};
    }
  `,
  rowError: css`
    border-left: 2px solid ${theme.colors.error.main};
  `,
  chevron: css`
    color: ${theme.colors.text.secondary};
    flex-shrink: 0;
  `,
  dot: css`
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  `,
  dotLlm: css`
    background: ${theme.colors.primary.main};
  `,
  dotTool: css`
    background: ${theme.colors.warning.main};
  `,
  dotHttp: css`
    background: ${theme.colors.text.secondary};
  `,
  dotBusiness: css`
    background: ${theme.colors.info.main};
  `,
  label: css`
    font-weight: ${theme.typography.fontWeightMedium};
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 320px;
  `,
  model: css`
    color: ${theme.colors.text.secondary};
    font-family: ${theme.typography.fontFamilyMonospace};
    font-size: ${theme.typography.size.xs};
  `,
  spacer: css`
    flex: 1;
  `,
  tok: css`
    color: ${theme.colors.text.secondary};
    white-space: nowrap;
  `,
  cache: css`
    color: ${theme.colors.success.text};
  `,
  cost: css`
    color: ${theme.colors.text.primary};
    font-variant-numeric: tabular-nums;
    min-width: 64px;
    text-align: right;
  `,
  dur: css`
    color: ${theme.colors.text.secondary};
    font-variant-numeric: tabular-nums;
    min-width: 56px;
    text-align: right;
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    padding: ${theme.spacing(2)};
  `,
});
