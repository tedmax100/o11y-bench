import React, { useCallback, useEffect, useRef, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Button, Input, Stack, useStyles2, Spinner, Collapse, Alert } from '@grafana/ui';
import { testIds } from '../components/testIds';
import { PromqlPanel } from '../components/PromqlPanel';
import { LogsPanel } from '../components/LogsPanel';
import { TracesPanel } from '../components/TracesPanel';

type ChatEvent =
  | { type: 'thread'; thread_id: string }
  | { type: 'token'; text: string }
  | { type: 'final'; text: string }
  | { type: 'tool_start'; tool: string; input: unknown }
  | { type: 'tool_end'; tool: string; output_preview: string }
  | { type: 'clarify'; prompt: string; options: string[] }
  | { type: 'done' };

type ToolCall = {
  tool: string;
  input: unknown;
  outputPreview?: string;
  open: boolean;
};

// When resolution is ambiguous the agent asks which service is meant; we render
// `options` as buttons and resend the original `question` with the picked one.
type Clarify = {
  prompt: string;
  options: string[];
  question: string;
  answered?: string;
};

type Message = {
  role: 'user' | 'assistant';
  text: string;
  toolCalls: ToolCall[];
  clarify?: Clarify;
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

  // Core send. `serviceHint` is set when resuming from a clarify menu — the
  // question bubble already exists, so we only append a fresh assistant bubble.
  const sendMessage = useCallback(
    async (text: string, serviceHint?: string) => {
      if (!text || busy) {
        return;
      }
      setError(null);
      setBusy(true);

      setMessages((prev) => [
        ...prev,
        ...(serviceHint ? [] : [{ role: 'user' as const, text, toolCalls: [] }]),
        { role: 'assistant' as const, text: '', toolCalls: [] },
      ]);

      try {
        const res = await fetch(`${agentServiceUrl}/chat`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ message: text, thread_id: threadIdRef.current, service_hint: serviceHint }),
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
    },
    [busy, agentServiceUrl]
  );

  const handleSend = useCallback(() => {
    const text = input.trim();
    if (!text || busy) {
      return;
    }
    setInput('');
    sendMessage(text);
  }, [input, busy, sendMessage]);

  // User picked a service from a clarify menu: mark it answered and resend the
  // original question with the chosen service as a hint.
  const handleClarifySelect = useCallback(
    (msgIndex: number, service: string, question: string) => {
      setMessages((prev) =>
        prev.map((m, i) =>
          i === msgIndex && m.clarify ? { ...m, clarify: { ...m.clarify, answered: service } } : m
        )
      );
      sendMessage(question, service);
    },
    [sendMessage]
  );

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
            <MessageBubble key={i} message={m} index={i} onClarify={handleClarifySelect} busy={busy} />
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

type MessageBubbleProps = {
  message: Message;
  index: number;
  onClarify: (msgIndex: number, service: string, question: string) => void;
  busy: boolean;
};

function MessageBubble({ message, index, onClarify, busy }: MessageBubbleProps) {
  const styles = useStyles2(getStyles);
  const segments = message.role === 'assistant' ? splitQueryBlocks(message.text) : [{ kind: 'text' as const, value: message.text }];
  return (
    <div className={message.role === 'user' ? styles.userBubble : styles.assistantBubble}>
      <div className={styles.role}>{message.role}</div>
      {message.toolCalls.map((tc, i) => (
        <ToolCallView key={i} call={tc} />
      ))}
      {segments.map((seg, i) => {
        switch (seg.kind) {
          case 'promql':
            return <PromqlPanel key={i} expr={seg.query} />;
          case 'logql':
            return <LogsPanel key={i} expr={seg.query} maxLines={seg.limit} />;
          case 'traceql':
            return <TracesPanel key={i} query={seg.query} limit={seg.limit} />;
          default:
            return <div key={i} className={styles.text}>{seg.value}</div>;
        }
      })}
      {message.clarify && (
        <ClarifyMenu
          clarify={message.clarify}
          disabled={busy || !!message.clarify.answered}
          onSelect={(service) => onClarify(index, service, message.clarify!.question)}
        />
      )}
    </div>
  );
}

function ClarifyMenu({
  clarify,
  disabled,
  onSelect,
}: {
  clarify: Clarify;
  disabled: boolean;
  onSelect: (service: string) => void;
}) {
  const styles = useStyles2(getStyles);
  return (
    <div className={styles.clarify}>
      <div className={styles.text}>{clarify.prompt}</div>
      <Stack direction="row" gap={1} wrap="wrap">
        {clarify.options.map((opt) => (
          <Button
            key={opt}
            size="sm"
            variant={clarify.answered === opt ? 'primary' : 'secondary'}
            disabled={disabled}
            onClick={() => onSelect(opt)}
          >
            {opt}
          </Button>
        ))}
      </Stack>
    </div>
  );
}

type Segment =
  | { kind: 'text'; value: string }
  | { kind: 'promql' | 'logql' | 'traceql'; query: string; limit?: number };

// Walk the assistant's text and split out fenced ```promql / ```logql /
// ```traceql blocks so we can render each as a live Scenes panel (timeseries /
// logs / traces table) instead of monospace code. An optional count on the
// fence info line (```logql 10 / ```traceql 3) becomes the panel's row/line
// limit, so "show me 3 traces" renders exactly 3. Other fences fall through as
// plain text.
function splitQueryBlocks(text: string): Segment[] {
  if (!text) {
    return [];
  }
  // group 1: language, group 2: rest of info line (optional count), group 3: body
  const re = /```(promql|logql|traceql)([^\n]*)\n?([\s\S]*?)```/g;
  const out: Segment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      out.push({ kind: 'text', value: text.slice(last, m.index) });
    }
    const limitMatch = m[2].match(/\d+/);
    const limit = limitMatch ? parseInt(limitMatch[0], 10) : undefined;
    out.push({ kind: m[1] as 'promql' | 'logql' | 'traceql', query: m[3].trim(), limit });
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    out.push({ kind: 'text', value: text.slice(last) });
  }
  return out;
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
    case 'clarify': {
      // The originating question is the preceding user message — stash it so a
      // menu pick can resend it with the chosen service hint.
      const question = idx >= 1 && messages[idx - 1].role === 'user' ? messages[idx - 1].text : '';
      updated.clarify = { prompt: evt.prompt, options: evt.options, question };
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
  clarify: css`
    margin-top: ${theme.spacing(1)};
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    text-align: center;
    padding: ${theme.spacing(4)};
  `,
});

export default ChatPage;
