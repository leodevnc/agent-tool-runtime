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

- The in-memory store demonstrates semantics but is not durable across processes or restarts.
- Retry delays use exponential backoff without jitter in v0.1 for deterministic testing.
- Events are an internal contract; an OpenTelemetry adapter is planned.
- Authorization uses scopes only. Policy engines and resource-level checks belong in adapters.
