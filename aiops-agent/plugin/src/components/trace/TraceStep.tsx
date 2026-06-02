import React from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { useStyles2 } from '@grafana/ui';
import { ChatMessage, ChatPart, GenericPayload, LlmPayload, ToolPayload, TraceNode } from './types';

// Renders the detail body for a single span, switched on its kind:
//  - llm:   the prompt (input) + completion (output) messages
//  - tool:  the call arguments + result
//  - other: a small attribute table

function messageText(m: ChatMessage): string {
  if (typeof m.content === 'string') {
    return m.content;
  }
  if (Array.isArray(m.parts)) {
    return m.parts.map((p: ChatPart) => p.content ?? '').join('');
  }
  return '';
}

function MessageList({ title, messages }: { title: string; messages?: ChatMessage[] | string }) {
  const styles = useStyles2(getStyles);
  if (!messages) {
    return null;
  }
  const list: ChatMessage[] = Array.isArray(messages)
    ? messages
    : [{ role: 'text', content: String(messages) }];
  if (list.length === 0) {
    return null;
  }
  return (
    <div className={styles.block}>
      <div className={styles.blockTitle}>{title}</div>
      {list.map((m, i) => (
        <div key={i} className={styles.msg}>
          <div className={styles.msgRole}>
            {m.role ?? 'message'}
            {m.finish_reason ? ` · ${m.finish_reason}` : ''}
          </div>
          <pre className={styles.msgText}>{messageText(m) || '—'}</pre>
        </div>
      ))}
    </div>
  );
}

function asPretty(v: unknown): string {
  if (v == null) {
    return '—';
  }
  if (typeof v === 'string') {
    return v;
  }
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export function TraceStep({ node }: { node: TraceNode }) {
  const styles = useStyles2(getStyles);

  if (node.kind === 'llm') {
    const p = node.payload as LlmPayload;
    const sys = p.system_instructions;
    const sysText = Array.isArray(sys) ? sys.map((s) => s.content ?? '').join('') : sys;
    return (
      <div className={styles.body}>
        {sysText ? (
          <div className={styles.block}>
            <div className={styles.blockTitle}>system</div>
            <pre className={styles.msgText}>{sysText}</pre>
          </div>
        ) : null}
        <MessageList title="input" messages={p.input_messages} />
        <MessageList title="output" messages={p.output_messages} />
      </div>
    );
  }

  if (node.kind === 'tool') {
    const p = node.payload as ToolPayload;
    return (
      <div className={styles.body}>
        <div className={styles.block}>
          <div className={styles.blockTitle}>arguments</div>
          <pre className={styles.msgText}>{asPretty(p.arguments)}</pre>
        </div>
        <div className={styles.block}>
          <div className={styles.blockTitle}>result</div>
          <pre className={styles.msgText}>{asPretty(p.result)}</pre>
        </div>
      </div>
    );
  }

  const p = node.payload as GenericPayload;
  const attrs = p.attributes ?? {};
  const keys = Object.keys(attrs);
  return (
    <div className={styles.body}>
      {keys.length === 0 ? (
        <div className={styles.empty}>No notable attributes.</div>
      ) : (
        <table className={styles.attrs}>
          <tbody>
            {keys.map((k) => (
              <tr key={k}>
                <td className={styles.attrKey}>{k}</td>
                <td className={styles.attrVal}>{String(attrs[k])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  body: css`
    padding: ${theme.spacing(1)};
    background: ${theme.colors.background.primary};
    border-radius: ${theme.shape.radius.default};
  `,
  block: css`
    margin-bottom: ${theme.spacing(1)};
  `,
  blockTitle: css`
    font-size: ${theme.typography.size.sm};
    font-weight: ${theme.typography.fontWeightMedium};
    color: ${theme.colors.text.secondary};
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: ${theme.spacing(0.5)};
  `,
  msg: css`
    margin-bottom: ${theme.spacing(0.5)};
  `,
  msgRole: css`
    font-size: ${theme.typography.size.sm};
    color: ${theme.colors.text.link};
  `,
  msgText: css`
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
    font-size: ${theme.typography.bodySmall.fontSize};
    background: ${theme.colors.background.secondary};
    padding: ${theme.spacing(0.5, 1)};
    border-radius: ${theme.shape.radius.default};
    max-height: 320px;
    overflow: auto;
  `,
  attrs: css`
    width: 100%;
    font-size: ${theme.typography.bodySmall.fontSize};
    border-collapse: collapse;
  `,
  attrKey: css`
    color: ${theme.colors.text.secondary};
    padding: ${theme.spacing(0.25, 1, 0.25, 0)};
    white-space: nowrap;
    vertical-align: top;
  `,
  attrVal: css`
    font-family: ${theme.typography.fontFamilyMonospace};
    word-break: break-word;
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
  `,
});
