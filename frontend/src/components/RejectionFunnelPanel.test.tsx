import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { RejectionFunnelPanel } from './RejectionFunnelPanel';
import type { ScanFunnel } from '../api/types';

const FUNNEL: ScanFunnel = {
  top_n: 20,
  total_rejections: 3,
  by_reason: { illiquid: 2, score_below_threshold: 1 },
  by_strategy_reason: {
    sentiment_breakout: { illiquid: 2 },
    sentiment_pullback: { score_below_threshold: 1 },
  },
  near_misses: [
    {
      symbol: 'AAPL',
      strategy: 'sentiment_pullback',
      reason_code: 'score_below_threshold',
      metric: 44.5,
      threshold: 48.0,
      margin: -3.5,
      overall_score: 44.5,
      confidence: 0.62,
      weakest_components: [
        ['pullback_depth', 2.1],
        ['volume_confirmation', 5.4],
      ],
      strongest_components: [['technical_setup', 18.0]],
    },
  ],
};

describe('RejectionFunnelPanel', () => {
  it('renders nothing when there were no rejections', () => {
    const { container } = render(
      <RejectionFunnelPanel funnel={{ ...FUNNEL, total_rejections: 0, by_reason: {}, near_misses: [] }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders the reason-count table and the near-miss table', () => {
    render(<RejectionFunnelPanel funnel={FUNNEL} />);

    expect(screen.getByText('Why no signals?')).toBeInTheDocument();
    expect(screen.getByText(/3 candidate evaluation/)).toBeInTheDocument();

    // Reason -> count table.
    expect(screen.getByText('illiquid')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    // Appears once in the reason table and once in the near-miss row.
    expect(screen.getAllByText('score_below_threshold')).toHaveLength(2);

    // Near-miss row.
    expect(screen.getByText('AAPL')).toBeInTheDocument();
    expect(screen.getByText('sentiment_pullback')).toBeInTheDocument();
    expect(screen.getByText('44.5 / 48.0')).toBeInTheDocument();
    expect(screen.getByText(/pullback_depth/)).toBeInTheDocument();
  });

  it('omits the near-miss table when there are none', () => {
    render(<RejectionFunnelPanel funnel={{ ...FUNNEL, near_misses: [] }} />);
    expect(screen.queryByText('Closest near-misses')).not.toBeInTheDocument();
  });
});
