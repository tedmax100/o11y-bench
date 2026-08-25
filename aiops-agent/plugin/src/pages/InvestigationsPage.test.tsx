import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import InvestigationsPage from './InvestigationsPage';

/**
 * What the on-call sees about *why the run stopped*.
 *
 * The card used to carry one number for this: a confidence the model wrote
 * about its own work. These tests pin the replacement — the unmet checks are
 * shown with the measurement behind them, and a run recorded before the gate
 * existed shows neither verdict.
 */

const BASE = {
  fp: 'fp-1',
  ts: '2026-08-26T00:00:00Z',
  alertname: 'PaymentDeclineRateHigh',
  service: 'payment-service',
  git_version: 'v2.5.0',
  summary: 'validator declines odd cents',
  hypothesis: 'bad deploy',
  confidence: 0.9,
  suspected_version: 'v2.5.0',
  services: ['payment-service'],
  decisions: [],
  answer: 'a',
  correct: null,
  source: 'alert' as const,
};

function mockFetch(items: unknown[]) {
  global.fetch = jest.fn().mockImplementation((url: string) =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: async () =>
        String(url).includes('/investigations') ? { investigations: items } : { requests: [] },
    })
  ) as jest.Mock;
}

afterEach(() => jest.resetAllMocks());

test('a run that stopped short shows what it was missing, not just a score', async () => {
  mockFetch([
    {
      ...BASE,
      sufficiency: {
        sufficient: false,
        checks: [
          { name: 'observed', passed: true, detail: '3/4 tool results were usable as evidence' },
          {
            name: 'independent_sources',
            passed: false,
            detail: "1 independent source(s) ['runtime']; needs 2",
          },
          {
            name: 'causal_roles',
            passed: false,
            detail: "observations speak to ['mechanism']; needs 2 distinct roles",
          },
          {
            name: 'conclusion_cites_evidence',
            passed: true,
            detail: '2 evidence item(s) cited in the conclusion',
          },
        ],
      },
    },
  ]);

  render(<InvestigationsPage agentServiceUrl="http://agent" />);

  await waitFor(() => expect(screen.getByText('證據缺 2 項')).toBeInTheDocument());
  // The two gaps are named, with the measurement that produced them.
  expect(screen.getByText('不只一個來源')).toBeInTheDocument();
  expect(screen.getByText("1 independent source(s) ['runtime']; needs 2")).toBeInTheDocument();
  expect(screen.getByText('不只一種因果角色')).toBeInTheDocument();
  // Checks that passed are not listed: the card is about what is missing.
  expect(screen.queryByText('有量到東西')).not.toBeInTheDocument();
});

test('a sufficient run says so once, without listing the checks', async () => {
  mockFetch([
    {
      ...BASE,
      sufficiency: {
        sufficient: true,
        checks: [
          { name: 'observed', passed: true, detail: '4/4' },
          { name: 'independent_sources', passed: true, detail: '2' },
          { name: 'causal_roles', passed: true, detail: '2' },
          { name: 'conclusion_cites_evidence', passed: true, detail: '2' },
        ],
      },
    },
  ]);

  render(<InvestigationsPage agentServiceUrl="http://agent" />);

  await waitFor(() => expect(screen.getByText('證據足夠')).toBeInTheDocument());
  expect(screen.queryByText('不只一個來源')).not.toBeInTheDocument();
});

test('a run recorded before the gate existed shows neither verdict', async () => {
  // Not "sufficient", not "缺 0 項" — an unrecorded verdict is not a passing one.
  mockFetch([{ ...BASE, sufficiency: null }]);

  render(<InvestigationsPage agentServiceUrl="http://agent" />);

  await waitFor(() => expect(screen.getByText('validator declines odd cents')).toBeInTheDocument());
  expect(screen.queryByText('證據足夠')).not.toBeInTheDocument();
  expect(screen.queryByText(/證據缺/)).not.toBeInTheDocument();
});

test('an unknown check name still reaches the screen', async () => {
  // The service can add a check before the plugin knows its label; falling back
  // to the raw name keeps the new gap visible instead of silently dropping it.
  mockFetch([
    {
      ...BASE,
      sufficiency: {
        sufficient: false,
        checks: [{ name: 'refutation_attempted', passed: false, detail: 'no refutation was tried' }],
      },
    },
  ]);

  render(<InvestigationsPage agentServiceUrl="http://agent" />);

  await waitFor(() => expect(screen.getByText('refutation_attempted')).toBeInTheDocument());
  expect(screen.getByText('no refutation was tried')).toBeInTheDocument();
});
