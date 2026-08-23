import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CasesPage from './CasesPage';

const CASE = {
  case_key: 'k1',
  first_ts: '2026-08-20T00:00:00Z',
  last_ts: '2026-08-21T00:00:00Z',
  alertname: 'PaymentDeclineRateHigh',
  service: 'payment-service',
  symptom: 'decline rate up',
  occurrences: 7,
  root_cause: null,
  root_cause_source: null,
  confirmed_run_id: null,
  resolution: null,
  status: 'open',
};

function mockFetch(body: unknown) {
  global.fetch = jest.fn().mockResolvedValue({ ok: true, status: 200, json: async () => body }) as jest.Mock;
}

const DETAIL = {
  case: CASE,
  recallable: false,
  runs: [{ run_id: 'r1', fp: 'fp1', ts: '2026-08-21T00:00:00Z', correct: null, grading_mode: null, error_dimension: null }],
  dead_ends: [],
};

describe('CasesPage', () => {
  it('marks a case that has learned nothing yet', async () => {
    mockFetch({ cases: [CASE], total: 1 });
    render(<CasesPage agentServiceUrl="http://agent" />);

    await waitFor(() => expect(screen.getByText('PaymentDeclineRateHigh')).toBeInTheDocument());
    expect(screen.getByText('no root cause yet')).toBeInTheDocument();
    expect(screen.getByText('seen 7×')).toBeInTheDocument();
  });

  it('lets a person write the root cause nobody has said yet', async () => {
    const post = jest.fn();
    global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        post(url, JSON.parse(String(init.body)));
        return Promise.resolve({ ok: true, status: 200, json: async () => CASE });
      }
      const body = String(url).includes('/cases/k1') ? DETAIL : { cases: [CASE], total: 1 };
      return Promise.resolve({ ok: true, status: 200, json: async () => body });
    }) as jest.Mock;

    render(<CasesPage agentServiceUrl="http://agent" />);
    fireEvent.click(await screen.findByText('PaymentDeclineRateHigh'));

    const box = await screen.findByPlaceholderText(/session cache was disabled/i);
    fireEvent.change(box, { target: { value: 'flag flip disabled the session cache' } });
    fireEvent.click(screen.getByText('Save root cause'));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [url, body] = post.mock.calls[0];
    expect(url).toContain('/cases/k1/root-cause');
    expect(body.root_cause).toBe('flag flip disabled the session cache');
    // The run being looked at is named, so the verdict stays replayable.
    expect(body.run_id).toBe('r1');
  });

  it('is honest about an empty memory', async () => {
    mockFetch({ cases: [], total: 0 });
    render(<CasesPage agentServiceUrl="http://agent" />);
    await waitFor(() => expect(screen.getByText(/No cases yet/)).toBeInTheDocument());
  });
});
