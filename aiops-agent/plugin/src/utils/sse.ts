// Shared Server-Sent-Events reader for the agent service's SSE endpoints
// (`/chat`, `/traces/{id}/chat`). Extracted from ChatPage so both the RCA chat
// and the Trace Explorer side-chat parse the stream identically.
//
// sse-starlette emits `event:` + `data:` line pairs separated by a blank line.
// We normalize CRLF (line endings vary), split on the blank-line block
// boundary, and hand each parsed payload to `onEvent(eventName, data)`.

export type SSEHandler = (eventName: string, data: any) => void;

export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal,
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
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');

    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() ?? '';
    for (const block of blocks) {
      let event = 'message';
      let dataRaw = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) {
          event = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          dataRaw += line.slice(6);
        }
      }
      if (!dataRaw) {
        continue;
      }
      let parsed: any;
      try {
        parsed = JSON.parse(dataRaw);
      } catch {
        continue;
      }
      onEvent(event, parsed);
    }
  }
}
