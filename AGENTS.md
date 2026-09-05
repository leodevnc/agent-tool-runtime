# Repository guidance

- Keep the runtime provider-neutral. Provider adapters belong in separate modules.
- Preserve the invariant that tool failures become structured `ToolResult` values.
- Never log tool arguments or outputs by default; events carry metadata only.
- Add or update tests for authorization, retries, timeouts, and idempotency changes.
- Run `pytest` and `ruff check .` before pushing.
- Write documentation as a natural engineering study: focus on questions, experiments, tradeoffs,
  evidence, and remaining uncertainty.
- Do not include meta-commentary about the author's career goals, assessment context, or intended
  audience in repository content or commit messages.
