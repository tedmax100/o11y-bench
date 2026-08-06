import React, { useCallback, useEffect, useRef, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { PluginPage } from '@grafana/runtime';
import { Badge, Button, Input, Stack, useStyles2, Spinner, Collapse, Alert } from '@grafana/ui';
import { testIds } from '../components/testIds';
import { streamSSE } from '../utils/sse';
import { PromqlPanel } from '../components/PromqlPanel';
import { LogsPanel } from '../components/LogsPanel';
import { TracesPanel } from '../components/TracesPanel';
import { AlertProposalCard, AlertSpec } from '../components/AlertProposalCard';

type ChatEvent =
  | { type: 'thread'; thread_id: string }
  | { type: 'token'; text: string }
  | { type: 'final'; text: string }
  | { type: 'tool_start'; tool: string; input: unknown }
  | { type: 'tool_end'; tool: string; output_preview: string }
  | { type: 'status'; phase: string; label: string }
  | { type: 'suggestions'; items: string[] }
  | { type: 'clarify'; prompt: string; options: string[] }
  | {
      type: 'findings';
      summary: string;
      hypothesis: string;
      confidence: number;
      services: string[];
      suspected_version: string | null;
    }
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
  // Current progress phase ("思考中…" etc). Set by `status` events, cleared once
  // the answer starts streaming / the turn ends.
  status?: string;
  // LLM-suggested follow-up questions, rendered as clickable chips under the answer.
  suggestions?: string[];
  // The structured verdict of an investigate-mode turn (the alert path has
  // always produced one; chat did not until it was wired up). Absent on lookup
  // turns, where "here is the panel" is the whole answer.
  findings?: {
    summary: string;
    confidence: number;
    services: string[];
    suspectedVersion: string | null;
  };
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
        await streamSSE(
          `${agentServiceUrl}/chat`,
          { message: text, thread_id: threadIdRef.current, service_hint: serviceHint },
          (event, parsed) => {
            if (event === 'thread' && parsed?.thread_id) {
              threadIdRef.current = parsed.thread_id;
              return;
            }
            setMessages((prev) => applyEvent(prev, parsed as ChatEvent));
          }
        );
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
            <MessageBubble
              key={i}
              message={m}
              index={i}
              onClarify={handleClarifySelect}
              onFollowUp={(text) => sendMessage(text)}
              busy={busy}
              agentServiceUrl={agentServiceUrl}
            />
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
  onFollowUp: (text: string) => void;
  busy: boolean;
  agentServiceUrl: string;
};

function confidenceColor(c: number): 'green' | 'orange' | 'red' {
  if (c >= 0.8) {
    return 'green';
  }
  if (c >= 0.5) {
    return 'orange';
  }
  return 'red';
}

// The verdict line under an investigation's answer. Deliberately plain: it
// shows what was concluded and how sure the agent claims to be, so the number
// can be argued with instead of being buried in prose.
function FindingsBar({ findings }: { findings: NonNullable<Message['findings']> }) {
  const styles = useStyles2(getStyles);
  return (
    <div className={styles.findings}>
      <Badge text={`confidence ${(findings.confidence * 100).toFixed(0)}%`} color={confidenceColor(findings.confidence)} />
      {findings.services.map((s) => (
        <Badge key={s} text={s} color="blue" />
      ))}
      {findings.suspectedVersion && <Badge text={findings.suspectedVersion} color="purple" />}
      <span className={styles.findingsSummary}>{findings.summary}</span>
    </div>
  );
}

function MessageBubble({ message, index, onClarify, onFollowUp, busy, agentServiceUrl }: MessageBubbleProps) {
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
          case 'alert':
            return <AlertProposalCard key={i} spec={seg.spec} agentServiceUrl={agentServiceUrl} />;
          default:
            return <div key={i} className={styles.text}>{seg.value}</div>;
        }
      })}
      {message.findings && <FindingsBar findings={message.findings} />}
      {message.status && (
        <div className={styles.status}>
          <Spinner inline size="sm" />
          <span>{message.status}</span>
        </div>
      )}
      {message.clarify && (
        <ClarifyMenu
          clarify={message.clarify}
          disabled={busy || !!message.clarify.answered}
          onSelect={(service) => onClarify(index, service, message.clarify!.question)}
        />
      )}
      {message.suggestions && message.suggestions.length > 0 && (
        <div className={styles.followups}>
          <div className={styles.followupLabel}>Follow-up</div>
          <Stack direction="column" gap={0.5} alignItems="flex-start">
            {message.suggestions.map((s, i) => (
              <Button
                key={i}
                size="sm"
                variant="secondary"
                fill="outline"
                icon="comment-alt"
                disabled={busy}
                onClick={() => onFollowUp(s)}
              >
                {s}
              </Button>
            ))}
          </Stack>
        </div>
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
  | { kind: 'promql' | 'logql' | 'traceql'; query: string; limit?: number }
  | { kind: 'alert'; spec: AlertSpec };

// Walk the assistant's text and split out fenced ```promql / ```logql /
// ```traceql / ```alert blocks so we can render each as a live Scenes panel
// (timeseries / logs / traces table) or, for ```alert, a proposal card with a
// provision button. An optional count on a query fence info line (```logql 10 /
// ```traceql 3) becomes the panel's row/line limit, so "show me 3 traces"
// renders exactly 3. An ```alert body is JSON (an AlertSpec); a malformed one
// falls through as plain text so the user still sees what the agent wrote.
function splitQueryBlocks(text: string): Segment[] {
  if (!text) {
    return [];
  }
  // group 1: language, group 2: rest of info line (optional count), group 3: body
  // `json` is in here on purpose: the model gets the alert JSON right far more
  // often than it gets the fence tag right, and a proposal rendered as a code
  // block instead of a button is one the user has to hand-carry into Grafana.
  // A ```json``` block only becomes a card if it validates as an AlertSpec.
  const re = /```(promql|logql|traceql|alert|json)([^\n]*)\n?([\s\S]*?)```/g;
  const out: Segment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      out.push({ kind: 'text', value: text.slice(last, m.index) });
    }
    if (m[1] === 'alert' || m[1] === 'json') {
      const spec = parseAlertSpec(m[3]);
      // Keep the raw block as text if it isn't valid JSON / lacks the essentials
      // — a ```json``` block that is not an alert spec is just JSON to display.
      out.push(spec ? { kind: 'alert', spec } : { kind: 'text', value: m[0] });
    } else {
      const limitMatch = m[2].match(/\d+/);
      const limit = limitMatch ? parseInt(limitMatch[0], 10) : undefined;
      out.push({ kind: m[1] as 'promql' | 'logql' | 'traceql', query: m[3].trim(), limit });
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    out.push({ kind: 'text', value: text.slice(last) });
  }
  return out;
}

function parseAlertSpec(body: string): AlertSpec | null {
  try {
    const o = JSON.parse(body);
    if (typeof o?.title === 'string' && typeof o?.expr === 'string' && typeof o?.threshold === 'number') {
      return o as AlertSpec;
    }
  } catch {
    // fall through
  }
  return null;
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
    case 'status':
      updated.status = evt.label;
      break;
    case 'suggestions':
      updated.suggestions = evt.items;
      break;
    case 'findings':
      // An investigate-mode turn ends with the same structured verdict the
      // alert path produces. Prose alone cannot be graded, filed, or compared;
      // this is what makes a typed question a first-class investigation.
      updated.findings = {
        summary: evt.summary,
        confidence: evt.confidence,
        services: evt.services ?? [],
        suspectedVersion: evt.suspected_version ?? null,
      };
      break;
    case 'token':
      updated.text = last.text + evt.text;
      updated.status = undefined; // answer is streaming — stop showing "thinking"
      break;
    case 'final':
      // Only adopt the final text if streaming tokens never arrived.
      if (!last.text) {
        updated.text = evt.text;
      }
      updated.status = undefined;
      break;
    case 'tool_start':
      updated.toolCalls.push({ tool: evt.tool, input: evt.input, open: false });
      updated.status = undefined; // the tool call view conveys the activity now
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
      updated.status = undefined;
      break;
  }
  return [...messages.slice(0, idx), updated];
}

const getStyles = (theme: GrafanaTheme2) => ({
  findings: css`
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-top: ${theme.spacing(1)};
    padding-top: ${theme.spacing(1)};
    border-top: 1px solid ${theme.colors.border.weak};
  `,
  findingsSummary: css`
    color: ${theme.colors.text.secondary};
    font-size: 12px;
  `,
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
  status: css`
    display: flex;
    align-items: center;
    gap: ${theme.spacing(1)};
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.size.sm};
    font-style: italic;
  `,
  clarify: css`
    margin-top: ${theme.spacing(1)};
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(1)};
  `,
  followups: css`
    margin-top: ${theme.spacing(1.5)};
    border-top: 1px solid ${theme.colors.border.weak};
    padding-top: ${theme.spacing(1)};
  `,
  followupLabel: css`
    font-size: ${theme.typography.size.sm};
    color: ${theme.colors.text.secondary};
    margin-bottom: ${theme.spacing(0.5)};
  `,
  empty: css`
    color: ${theme.colors.text.secondary};
    font-style: italic;
    text-align: center;
    padding: ${theme.spacing(4)};
  `,
});

export default ChatPage;
