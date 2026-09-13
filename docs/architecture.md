# Architecture

## Problem

An LLM can propose a tool call, but it should not directly invoke production systems. Model
outputs are untrusted, retries can duplicate side effects, and failures need stable semantics for
the orchestration loop. This runtime is the boundary between probabilistic planning and
deterministic backend execution.

## Request flow

```text
ToolCall + ExecutionContext
          |
          v
  canonical fingerprint
          |
          v
 idempotency / single-flight ----> cached replay
          |
          v
 registry allowlist -> JSON Schema -> scope authorization
          |
          v
 deadline + bounded retry policy
          |
          v
 structured ToolResult + metadata-only events
```

## Invariants

1. A tool must be explicitly registered before it can run.
2. Arguments must satisfy the tool's schema before authorization or execution.
3. The caller must have every scope required by the selected tool.
4. A call ID identifies one canonical `(principal, tool, arguments)` request.
5. Concurrent duplicates execute once and share the same result.
6. Exceptions and timeouts cross the boundary as structured results.
7. Observability failures never change tool execution outcomes.
8. Arguments and outputs are not included in default events.

## Trust boundaries

- **Untrusted:** model-selected tool name and arguments.
- **Authenticated but constrained:** principal and scopes supplied by the hosting service.
- **Trusted configuration:** registry definitions, schemas, handlers, deadlines, and retry rules.
- **External side effects:** handlers. They still need domain-level idempotency when talking to
  payment, messaging, or infrastructure APIs.

## Current tradeoffs

- The in-memory store demonstrates single-process semantics. The Redis adapter uses atomic Lua
  transitions, expiring owner leases, and fencing tokens for coordination across processes.
- Redis replay requires JSON-serializable tool results. Serialization failure leaves the original
  result available to its caller but emits an `idempotency_degraded` event because it cannot be
  replayed safely.
- Lease expiry can allow a replacement owner while the previous handler is still running. Fencing
  prevents the old owner from overwriting Redis state, but side-effecting downstream systems should
  also receive an idempotency or fencing key.
- Retry delays support no jitter, full jitter over `[0, cap]`, or equal jitter over
  `[cap / 2, cap]`. The executor accepts an injected random source so tests can assert the chosen
  delay without relying on global random state.
- Events are an internal contract; an OpenTelemetry adapter is planned.
- Authorization uses scopes only. Policy engines and resource-level checks belong in adapters.

## Verification layers

- Deterministic unit tests model Redis records, TTL expiry, stale owners, and cross-executor replay.
- Hypothesis generates retry parameters and call sequences to check delay bounds, overflow safety,
  replay consistency, and call-ID conflict behavior beyond hand-picked examples.
- CI runs the same claim and completion scripts against a Redis 7 service on Python 3.11, 3.12,
  and 3.13. This catches Lua or client-protocol mistakes that a script-level fake cannot detect.
