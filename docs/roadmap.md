# Roadmap

## v0.1 - Reliable in-process execution (complete)

- Strict JSON Schema validation
- Registry allowlist and scope authorization
- Per-tool deadlines and bounded retries
- Single-flight idempotency with conflict detection
- Structured results and metadata-only lifecycle events
- Unit and concurrency tests

## v0.2 - Durable distributed execution

- Redis idempotency adapter with TTL and atomic claim/complete operations (complete)
- Retry jitter and injectable random source
- Abandoned-claim recovery through expiring leases and fencing tokens (complete)
- Explicit cancellation behavior
- Property tests for idempotency invariants

## v0.3 - Production observability

- OpenTelemetry spans and metrics adapter
- Latency, retry, timeout, denial, and replay dashboards
- Trace correlation example with an HTTP orchestration service
- Failure-budget evaluation suite

## v0.4 - Agent integration

- OpenAI Responses API adapter kept outside the core runtime
- Parallel tool-call orchestration example
- Human approval policy for high-impact tools
- End-to-end evals for correctness, latency, and duplicate side effects
