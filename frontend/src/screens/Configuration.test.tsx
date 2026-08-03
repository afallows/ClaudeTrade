/**
 * Component test for the Configuration screen's two revamped pieces:
 * the "Signal Weightings" section (renders first, PUTs the edited value)
 * and the diagnostics-style, pipeline-grouped credential cards.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import type { AIConfig, CredentialsResponse, SignalWeights, SignalWeightsUpdateResult } from '../api/types';

const weights = vi.fn();
const updateWeights = vi.fn();
const aiConfig = vi.fn();
const credentials = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    weights: (...args: unknown[]) => weights(...args),
    updateWeights: (...args: unknown[]) => updateWeights(...args),
    aiConfig: (...args: unknown[]) => aiConfig(...args),
    credentials: (...args: unknown[]) => credentials(...args),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

// Imported after the mocks above so Configuration picks up the mocked client.
const { Configuration } = await import('./Configuration');

const WEIGHTS: SignalWeights = {
  weights: { technical_setup: 0.2, price_momentum: 0.12 },
  normalised: { technical_setup: 0.625, price_momentum: 0.375 },
  promoted_scoring: null,
};

const AI_CONFIG: AIConfig = {
  provider: 'none',
  model: '',
  anthropic_default_model: 'claude-opus-5',
  openai_default_model: 'gpt-5',
  anthropic_api_key_credential: 'anthropic_api_key',
  openai_api_key_credential: 'openai_api_key',
};

const CREDENTIALS: CredentialsResponse = {
  storage: 'OS credential store or environment',
  credentials: [
    { name: 'reddit_client_id', label: 'Reddit client ID', pipeline: 'sentiment', configured: false, source: null, masked: null },
    { name: 'polygon_api_key', label: 'Polygon.io API key', pipeline: 'stock_price', configured: true, source: 'keyring', masked: '****1234' },
  ],
};

describe('Configuration screen', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('renders the Signal Weightings section first and saves an edited weight', async () => {
    weights.mockResolvedValue(WEIGHTS);
    aiConfig.mockResolvedValue(AI_CONFIG);
    credentials.mockResolvedValue(CREDENTIALS);
    const updated: SignalWeightsUpdateResult = {
      weights: { technical_setup: 0.5, price_momentum: 0.12 },
      normalised: { technical_setup: 0.806, price_momentum: 0.194 },
      persisted: false,
      note: 'Applied immediately for this running session. To make it permanent across restarts, add it to config.toml.',
    };
    updateWeights.mockResolvedValue(updated);

    render(<Configuration />);

    const weightingsHeading = await screen.findByRole('heading', { level: 2, name: 'Signal Weightings' });
    const aiHeading = await screen.findByRole('heading', { level: 2, name: 'AI Analysis' });
    // Weightings renders first on the page -- DOCUMENT_POSITION_FOLLOWING
    // means aiHeading comes after weightingsHeading in document order.
    expect(
      weightingsHeading.compareDocumentPosition(aiHeading) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    const input = await screen.findByLabelText('technical setup');
    fireEvent.change(input, { target: { value: '0.5' } });
    // Several sections have their own "Save" button (weightings, AI
    // analysis, each credential card) -- the weightings section's is the
    // first in document order.
    fireEvent.click(screen.getAllByRole('button', { name: /^save$/i })[0]);

    await waitFor(() => expect(updateWeights).toHaveBeenCalledWith({ technical_setup: 0.5, price_momentum: 0.12 }));
    expect(await screen.findByText(/applied immediately/i)).toBeInTheDocument();
  });

  it('groups credential cards under pipeline subheadings', async () => {
    weights.mockResolvedValue(WEIGHTS);
    aiConfig.mockResolvedValue(AI_CONFIG);
    credentials.mockResolvedValue(CREDENTIALS);

    render(<Configuration />);

    expect(await screen.findByText('Sentiment sources')).toBeInTheDocument();
    expect(screen.getByText('Stock price sources')).toBeInTheDocument();
    expect(screen.getByText('Reddit client ID')).toBeInTheDocument();
    expect(screen.getByText('Polygon.io API key')).toBeInTheDocument();
    expect(screen.getAllByText('Configured').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Not configured').length).toBeGreaterThan(0);
  });
});
