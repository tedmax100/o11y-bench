import React, { useCallback, useEffect, useRef, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Button, Input, Stack, useStyles2, Spinner, Collapse, Alert } from '@grafana/ui';
import { testIds } from '../components/testIds';

type ChatEvent =
  | { type: 'thread'; thread_id: string }
  | { type: 'token'; text: string }
  | { type: 'final'; text: string }
  | { type: 'tool_start'; tool: string; input: unknown }
  | { type: 'tool_end'; tool: string; output_preview: string }
  | { type: 'done' };

type ToolCall = {
  tool: string;
  input: unknown;
  outputPreview?: string;
  open: boolean;
};

type Message = {
  role: 'user' | 'assistant';
  text: string;
  toolCalls: ToolCall[];
};

type ChatPageProps = {
  agentServiceUrl: string;
};

function ChatPage({ agentServiceUrl }: ChatPageProps) {
  const styles = useStyles2(getStyles);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const threadIdRef = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) {
      return;
    }
    setInput('');
    setError(null);
    setBusy(true);

    setMessages((prev) => [
      ...prev,
      { role: 'user', text, toolCalls: [] },
      { role: 'assistant', text: '', toolCalls: [] },
    ]);

    try {
      const res = await fetch(`${agentServiceUrl}/chat`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message: text, thread_id: threadIdRef.current }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`agent service returned ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }
        // Normalize CRLF so split works regardless of sse-starlette's line endings.
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');

        const blocks = buffer.split('\n\n');
        buffer = blocks.pop() ?? '';
        for (const block of blocks) {
          const lines = block.split('\n');
          let event = 'message';
          let dataRaw = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              event = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              dataRaw += line.slice(6);
            }
          }
          if (!dataRaw) {
            continue;
          }
          let parsed: ChatEvent | { thread_id: string };
          try {
            parsed = JSON.parse(dataRaw);
          } catch {
            continue;
          }

          if (event === 'thread' && 'thread_id' in parsed) {
            threadIdRef.current = parsed.thread_id;
            continue;
          }

          setMessages((prev) => applyEvent(prev, parsed as ChatEvent));
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [input, busy, agentServiceUrl]);

  return (
    <PluginPage>
      <div className={styles.wrapper}>
        {error && <Alert title="Agent service error" severity="error">{error}</Alert>}

        <div ref={scrollRef} className={styles.messages}>
          {messages.length === 0 && (
            <div className={styles.empty}>
              Ask something like: <em>list available datasources</em> or{' '}
              <em>why is checkout p99 latency high in the last hour?</em>
            </div>
          )}
          {messages.map((m, i) => (
            <MessageBubble key={i} message={m} />
          ))}
          {busy && <Spinner inline />}
        </div>

        <Stack direction="row" gap={1}>
          <Input
            value={input}
            data-testid={testIds.chat.input}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setInput(e.currentTarget.value)}
            onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask the agent..."
            disabled={busy}
          />
          <Button onClick={handleSend} disabled={busy || !input.trim()} data-testid={testIds.chat.send}>
            Send
          </Button>
        </Stack>
      </div>
    </PluginPage>
  );
}

function MessageBubble({ message }: { message: Message }) {
  const styles = useStyles2(getStyles);
  return (
    <div className={message.role === 'user' ? styles.userBubble : styles.assistantBubble}>
      <div className={styles.role}>{message.role}</div>
      {message.toolCalls.map((tc, i) => (
        <ToolCallView key={i} call={tc} />
      ))}
      <div className={styles.text}>{message.text}</div>
    </div>
  );
}

function ToolCallView({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(call.open);
  const label = call.outputPreview ? `🔧 ${call.tool}` : `⏳ ${call.tool}`;
  return (
    <Collapse label={label} isOpen={open} onToggle={() => setOpen(!open)} collapsible>
      <div>
        <strong>input:</strong>
        <pre>{JSON.stringify(call.input, null, 2)}</pre>
        {call.outputPreview && (
          <>
            <strong>output:</strong>
            <pre>{call.outputPreview}</pre>
          </>
        )}
      </div>
    </Collapse>
  );
}

function applyEvent(messages: Message[], evt: ChatEvent): Message[] {
  if (messages.length === 0) {
    return messages;
  }
  const idx = messages.length - 1;
  const last = messages[idx];
  if (last.role !== 'assistant') {
    return messages;
  }
  const updated = { ...last, toolCalls: [...last.toolCalls] };

  switch (evt.type) {
    case 'token':
      updated.text = last.text + evt.text;
      break;
    case 'final':
      // Only adopt the final text if streaming tokens never arrived.
      if (!last.text) {
        updated.text = evt.text;
      }
      break;
    case 'tool_start':
      updated.toolCalls.push({ tool: evt.tool, input: evt.input, open: false });
      break;
    case 'tool_end': {
      for (let i = updated.toolCalls.length - 1; i >= 0; i--) {
        if (updated.toolCalls[i].tool === evt.tool && !updated.toolCalls[i].outputPreview) {
          updated.toolCalls[i] = { ...updated.toolCalls[i], outputPreview: evt.output_preview };
          break;
        }
      }
      break;
    }
    case 'done':
      break;
  }
  return [...messages.slice(0, idx), updated];
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrapper: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(2)};
    height: calc(100vh - 200px);
  `,
  messages: css`
    flex: 1;
    overflow-y: auto;
    padding: ${theme.spacing(1)};
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
  `,
  userBubble: css`
    align-self: flex-end;
    max-width: 70%;
    background: ${theme.colors.primary.transparent};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(1, 1.5)};
  `,
  assistantBubble: css`
    align-self: flex-start;
    max-width: 80%;
    background: ${theme.colors.background.secondary};
    border-radius: ${theme.shape.radius.default};
    padding: ${theme.spacing(1, 1.5)};
  `,
  role: css`
    font-size: ${theme.typography.size.sm};
    color: ${theme.colors.text.secondary};
    margin-bottom: ${theme.spacing(0.5)};
  `,
  text: css`
    white-space: pre-wrap;
    word-wrap: break-word;
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    text-align: center;
    padding: ${theme.spacing(4)};
  `,
});

export default ChatPage;
