import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import TodoPage from './TodoPage';

// The point of this page is that it renders a distance, not a verdict — "17 of
// 20" is actionable, "denied" is not — and that a queue nobody answered is
// still visible.
const TODO = {
  investigations_to_label: {
    count: 2,
    items: [
      {
        fp: 'fp1',
        ts: '2026-08-22T15:40:17Z',
        alertname: 'order-cancel-rate-high',
        service: 'order-service',
        summary: 'session store degraded',
        confidence: 0.9,
      },
    ],
  },
  requests_to_decide: { count: 0, items: [], expired_unattended: 10 },
  cases_to_label: { count: 1, items: [] },
  autonomy: {
    granted: false,
    actions_enabled: false,
    gates: [{ gate: 'calibration', proven_good: false, note: 'calibration unproven (15 labeled run(s) < 20)' }],
    blockers: [],
    calibration: {
      labeled: 15,
      labeled_required: 20,
      human_labeled: 15,
      human_labeled_required: 20,
      band_lo: 0.8,
      band_n: 6,
      band_n_required: 3,
      band_accuracy: 1.0,
      band_accuracy_required: 0.7,
      overconfidence: -0.28,
      overconfidence_max: 0.1,
      worst_bin_gap: 0.4,
      worst_bin_gap_max: 0.25,
    },
  },
};

function mockFetch(body: unknown, ok = true) {
  global.fetch = jest.fn().mockResolvedValue({ ok, status: ok ? 200 : 500, json: async () => body }) as jest.Mock;
}

describe('TodoPage', () => {
  it('shows how far AUTO is, gate by gate', async () => {
    mockFetch(TODO);
    render(<TodoPage agentServiceUrl="http://agent" />);

    await waitFor(() => expect(screen.getByText('AUTO withheld')).toBeInTheDocument());
    expect(screen.getByText('kill switch: actions off')).toBeInTheDocument();
    // 15 labeled, 15 of them human — both rows short of the same bar.
    expect(screen.getAllByText('15')).toHaveLength(2);
    expect(screen.getAllByText('≥ 20')).toHaveLength(2);
    expect(screen.getByText(/calibration unproven/)).toBeInTheDocument();
  });

  it('reports proposals that expired unanswered', async () => {
    mockFetch(TODO);
    render(<TodoPage agentServiceUrl="http://agent" />);
    await waitFor(() => expect(screen.getByText('10 expired unanswered')).toBeInTheDocument());
  });

  it('labels a run without leaving the queue', async () => {
    const post = jest.fn();
    global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        post(url, JSON.parse(String(init.body)));
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => TODO });
    }) as jest.Mock;

    render(<TodoPage agentServiceUrl="http://agent" />);
    fireEvent.click(await screen.findByText('Correct'));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [url, body] = post.mock.calls[0];
    expect(url).toBe('http://agent/investigations/fp1/label');
    expect(body).toEqual({ correct: true });
  });

  it('makes a wrong verdict carry its dimension and note', async () => {
    const post = jest.fn();
    global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        post(url, JSON.parse(String(init.body)));
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => TODO });
    }) as jest.Mock;

    render(<TodoPage agentServiceUrl="http://agent" />);
    fireEvent.click(await screen.findByText('Wrong'));
    fireEvent.change(await screen.findByPlaceholderText(/flag flip/i), {
      target: { value: 'the flag flip, not the deploy' },
    });
    fireEvent.click(screen.getByText(/Mark Wrong/));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [, body] = post.mock.calls[0];
    expect(body).toEqual({
      correct: false,
      error_dimension: 'root_cause',
      correction_note: 'the flag flip, not the deploy',
    });
  });

  it('says so when the queue cannot be read', async () => {
    mockFetch({}, false);
    render(<TodoPage agentServiceUrl="http://agent" />);
    await waitFor(() => expect(screen.getByText('Could not load the queue')).toBeInTheDocument());
  });
});
