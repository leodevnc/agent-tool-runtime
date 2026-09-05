# Learning notes

## 2026-09-05 - Why tool execution is a backend boundary

The important design shift is to treat a model tool call as an untrusted request, not as a normal
function call. A reliable runtime needs the same controls as a public service endpoint: input
validation, authorization, deadlines, retry classification, idempotency, and observability.

The most subtle initial implementation problem was duplicate concurrency. Looking up a completed
result is insufficient because two identical requests can arrive before either finishes. The
single-flight claim stores a shared future before the handler starts, so concurrent duplicates wait
for the owner rather than repeating a side effect.

Retries at the runtime boundary do not eliminate the need for domain idempotency. A process can
crash after an external side effect succeeds but before the runtime records completion. Durable
adapters therefore need atomic state transitions and handlers should forward an idempotency key to
downstream systems whenever possible.
