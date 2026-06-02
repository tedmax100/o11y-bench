import React, { useCallback, useEffect, useRef, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { Button, Input, Spinner, Stack, useStyles2 } from '@grafana/ui';
import { streamSSE } from '../../utils/sse';

// "Chat about this trace" side panel. Bound to the selected trace; each turn
// POSTs the question + prior turns to /traces/{id}/chat and streams the answer.
// The trace JSON is the assistant's only context (server-side) — it does NOT
// run live queries.

type Msg = { role: 'user' | 'assistant'; text: string };

export function TraceChat({ agentServiceUrl, traceId }: { agentServiceUrl: string; traceId: string }) {
  const styles = useStyles2(getStyles);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // New trace selected → start a fresh conversation.
  useEffect(() => {
    setMessages([]);
    setInput('');
  }, [traceId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) {
      return;
    }
    setInput('');
    setBusy(true);
    const history = messages.map((m) => ({ role: m.role, text: m.text }));
    setMessages((prev) => [...prev, { role: 'user', text }, { role: 'assistant', text: '' }]);

    try {
      await streamSSE(`${agentServiceUrl}/traces/${traceId}/chat`, { message: text, history }, (event, parsed) => {
        if (parsed?.type === 'token' && parsed.text) {
          setMessages((prev) => {
            const i = prev.length - 1;
            return prev.map((m, j) => (j === i ? { ...m, text: m.text + parsed.text } : m));
          });
        }
      });
    } catch (e) {
      setMessages((prev) => {
        const i = prev.length - 1;
        const err = e instanceof Error ? e.message : String(e);
        return prev.map((m, j) => (j === i ? { ...m, text: m.text || `Error: ${err}` } : m));
      });
    } finally {
      setBusy(false);
    }
  }, [input, busy, messages, agentServiceUrl, traceId]);

  return (
    <div className={styles.wrapper}>
      <div className={styles.header}>Ask about this trace</div>
      <div ref={scrollRef} className={styles.messages}>
        {messages.length === 0 && (
          <div className={styles.empty}>
            e.g. <em>哪一次 LLM 呼叫最貴？</em> · <em>為什麼跑了這麼多次？</em>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? styles.userBubble : styles.assistantBubble}>
            {m.text || (busy && i === messages.length - 1 ? <Spinner inline size="sm" /> : null)}
          </div>
        ))}
      </div>
      <Stack direction="row" gap={1}>
        <Input
          value={input}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setInput(e.currentTarget.value)}
          onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder="Ask about this trace..."
          disabled={busy}
        />
        <Button onClick={send} disabled={busy || !input.trim()}>
          Send
        </Button>
      </Stack>
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrapper: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
    height: 100%;
    min-height: 0;
  `,
  header: css`
    font-weight: ${theme.typography.fontWeightMedium};
    color: ${theme.colors.text.secondary};
  `,
  messages: css`
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
    min-height: 0;
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    font-size: ${theme.typography.bodySmall.fontSize};
  `,
  userBubble: css`
    align-self: flex-end;
    max-width: 90%;
    background: ${theme.colors.primary.transparent};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(0.75, 1)};
    white-space: pre-wrap;
    word-break: break-word;
  `,
  assistantBubble: css`
    align-self: flex-start;
    max-width: 95%;
    background: ${theme.colors.background.secondary};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(0.75, 1)};
    white-space: pre-wrap;
    word-break: break-word;
  `,
});
