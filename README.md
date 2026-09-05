# agent-tool-runtime

A small, provider-neutral Python runtime that turns model-selected tool calls into controlled,
observable backend operations.

The project focuses on a production problem that is easy to hide in agent demos: the boundary
between a probabilistic planner and deterministic systems with permissions, deadlines, failures,
and side effects.

## What v0.1 demonstrates

- JSON Schema validation before handler execution
- Explicit registry allowlist
- Per-tool scope authorization
- Deadlines and bounded exponential retries
- Idempotent replay and concurrent single-flight execution
- Stable error translation without leaking internal exceptions
- Metadata-only lifecycle events for observability
- Async and sync handler support

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

## Example

```python
import asyncio

from agent_tool_runtime import (
    ExecutionContext,
    RetryPolicy,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
)


async def reserve_capacity(arguments, context):
    return {
        "site": arguments["site"],
        "reserved_mw": arguments["mw"],
        "requested_by": context.principal,
    }


registry = ToolRegistry()
registry.register(
    ToolDefinition(
        name="reserve_capacity",
        handler=reserve_capacity,
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "minLength": 1},
                "mw": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["site", "mw"],
            "additionalProperties": False,
        },
        required_scopes=frozenset({"capacity:reserve"}),
        timeout_seconds=2,
        retry_policy=RetryPolicy(max_attempts=3),
    )
)

executor = ToolExecutor(registry)
result = asyncio.run(
    executor.execute(
        ToolCall("call-42", "reserve_capacity", {"site": "bess-east", "mw": 4.5}),
        ExecutionContext("operator-17", frozenset({"capacity:reserve"}), "trace-9"),
    )
)
print(result.status, result.output)
```

## Design

The runtime deliberately does not call an LLM. It accepts a normalized `ToolCall`, making the core
portable across model providers and orchestration frameworks. See [architecture](docs/architecture.md)
for the request flow, trust boundaries, and invariants.

The [roadmap](docs/roadmap.md) builds toward durable Redis-backed idempotency, OpenTelemetry, an
HTTP orchestration example, and end-to-end agent evaluations. [Learning notes](docs/learning-notes.md)
capture the engineering reasoning behind each milestone.

## Study focus

This project explores backend and systems concerns that appear in production agent platforms:
safe tool execution, identity and permissions, observability, reliability, and evaluation. The
implementation stays intentionally compact so each invariant can be examined and tested in
isolation.

## License

MIT
