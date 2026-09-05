# Repository guidance

- Keep the runtime provider-neutral. Provider adapters belong in separate modules.
- Preserve the invariant that tool failures become structured `ToolResult` values.
- Never log tool arguments or outputs by default; events carry metadata only.
- Add or update tests for authorization, retries, timeouts, and idempotency changes.
- Run `pytest` and `ruff check .` before pushing.
