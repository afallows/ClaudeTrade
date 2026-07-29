# ADR-0001: Layered Architecture with Adapter Pattern

**Date**: 2024-01-15  
**Status**: Accepted  
**Participants**: ClaudeTrade Contributors

## Decision

Implement a **layered architecture** with **provider adapters** to cleanly separate concerns:

```
Providers (market, earnings, social, AI)
  ↓
Data Ingestion (ETL, quality checks)
  ↓
Feature Computation (technical, sentiment, regime)
  ↓
Signal Engine (strategies, scoring, lifecycle)
  ↓
Backtesting / Paper Trading / UI
```

Each layer has a clear contract:
- Providers: implement a protocol (MarketDataProvider, etc.)
- Data layer: validated, deduplicated facts
- Features: point-in-time snapshots
- Signals: ranked, scored proposals
- Execution: simulated or live fills

## Alternatives Considered

1. **Monolithic pipeline**: All logic in one class. 
   - **Rejected**: Hard to test, extend, or understand.

2. **Message-queue event bus** (Kafka, RabbitMQ):
   - **Rejected**: Overkill for a single-machine research tool; adds complexity and operational burden.

3. **Direct provider integration**: No adapters; call Stooq, Reddit, etc. directly throughout code.
   - **Rejected**: Tight coupling; cannot swap providers; cannot mock for testing.

## Reason Selected

1. **Testability**: Each layer can be tested independently with fakes/mocks.
2. **Extensibility**: New strategies, providers, or features are isolated changes.
3. **Clarity**: Data flows one direction; dependencies are acyclic.
4. **Reproducibility**: Each layer's output is deterministic and auditable.
5. **Operations**: Graceful degradation: if a provider fails, others continue.

## Risks

1. **Over-abstraction**: Layers can add boilerplate; the adapter protocol must not be too heavy.
   - **Mitigation**: Keep protocols minimal; use Python dataclasses and protocols, not heavy base classes.

2. **Performance**: Extra layer hops could slow down large backtests.
   - **Mitigation**: Measure; add caching at layer boundaries if needed.

3. **Debugging**: Errors can be harder to trace across layers.
   - **Mitigation**: Comprehensive logging at each layer boundary; reproducibility triple enables tracing.

## Reversal / Migration Plan

If the architecture proves inefficient:

1. **Benchmark**: Profile hot paths (feature computation, signal generation, backtest execution).
2. **Collapse if needed**: Merge performance-critical layers (e.g., features + signal engine) while keeping others separate.
3. **Cache aggressively**: Pre-compute features, cache sentiment aggregates.

The protocol-based design makes it easy to inline an adapter if needed; the layer boundaries are contracts, not rigid classes.

## Implementation Notes

- **Protocols, not abstract base classes**: Use `typing.Protocol` so any conforming object works (duck typing).
- **Exceptions, not return codes**: Raise `ProviderError` on failures; let callers handle degradation.
- **Determinism**: Every layer must be deterministic; same input → same output.
- **Point-in-time contexts**: Strategies receive snapshots, not time-series; prevents look-ahead.

## Related ADRs

- ADR-0002: Provider Adapter Pattern (details on how adapters work)
- ADR-0005: Immutable Signal Ledger (enforces audit trail across layers)
