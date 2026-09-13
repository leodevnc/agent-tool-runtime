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

## 2026-09-06 - Distributed claims need leases and fencing

Replacing the in-memory result cache with Redis is not just a storage change. A local future lets
duplicate callers await one owner inside a process, but it cannot notify a worker on another host.
The Redis adapter therefore represents calls as pending or completed records. Followers poll a
pending record until they can replay its result.

A pending record without expiry can block a call forever after its owner crashes. A lease makes the
record recoverable, but creates another race: the old owner can finish after a new owner acquires the
expired call. Each owner now receives a random fencing token, and the completion script accepts a
result only when both the fingerprint and current token match.

Fencing protects the idempotency record, not the external system invoked by a handler. A durable
design should pass the call ID or fencing token downstream so that a late worker cannot repeat or
overwrite a side effect after losing its lease.

## 2026-09-13 - Jitter is a coordination mechanism

Exponential backoff limits how frequently one caller retries, but identical workers can still wake
at the same times and recreate the overload that caused the failure. Jitter spreads those retries
across a window. Full jitter samples from zero to the exponential cap and favors faster recovery;
equal jitter keeps at least half of the cap and trades some recovery speed for less clustering near
zero.

The runtime accepts a random function rather than reading global random state inside the policy.
This keeps production behavior independent between calls while allowing tests to reproduce an exact
delay and verify the value emitted to observability.

Generating large attempt counts also exposed a numerical edge in the original expression
`base * 2 ** exponent`: it can construct an enormous intermediate value before applying the cap.
The revised calculation determines whether the cap has already been reached using logarithms, then
uses `ldexp` only while the uncapped value is representable.

Example-based tests remain useful for event sequences and concrete failures. Property tests cover
the wider invariants: delays remain finite and inside the selected jitter window, repeated calls
execute once, and a reused call ID never runs different arguments.
