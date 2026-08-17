# Browser Use MCP Project Guide

Keep this browser automation integration typed, async, bounded, minimal, and
secret-safe.

## Architecture

- `src/browser_use_mcp/` owns the MCP server and its browser integration.
- Mirror repository-owned boundaries under `tests/` as implementation is added.
- Keep MCP-facing models smaller than upstream browser or page representations.
- Keep CLI and transport composition thin; put browser lifecycle and automation
  behavior in focused services.
- Reuse process-lifetime browser and HTTP resources where their ownership and
  isolation boundaries permit it.

## Security Invariants

- Treat tool arguments, URLs, page content, downloads, and browser output as
  untrusted data.
- Never log or return credentials, cookies, authorization headers, browser
  profiles, form secrets, or unrelated page content.
- Require explicit authorization boundaries for navigation, downloads, file
  access, browser profiles, and private-network access before exposing them.
- Bound navigation time, task duration, retries, response size, downloads, and
  concurrency.
- Preserve browser-session isolation between callers. Do not persist profiles or
  session data without an explicit architecture and threat-model change.
- Keep destructive or externally visible actions explicit and fail closed when
  required confirmation is unavailable.
- Do not add a database, shared durable state, or unrestricted host filesystem
  access without an explicit architecture change.
- Keep the container non-root and compatible with a read-only root filesystem,
  dropped capabilities, and `no-new-privileges`.

## Python And Async Rules

- Support Python 3.12 through 3.14.
- Fully type new and changed functions; prefer Pydantic models at trust
  boundaries over loose dictionaries.
- Keep network and browser control paths non-blocking and preserve cancellation.
- Move unavoidable filesystem or blocking browser work off the event loop.
- Use bounded timeouts and retries. Retry mutations only when replay is safe.
- Preserve typed, secret-safe exceptions and validation at external boundaries.

## Dependencies And Packaging

- Use `uv` and keep `uv.lock` committed.
- Use public PyPI sources only; never commit private package-index URLs.
- Add runtime dependencies only when production code imports them.
- Pin Docker base images by version and digest; Dependabot owns routine updates.
- Pin GitHub Actions to full commit SHAs with readable release comments.

## Commands

Read `Makefile` before adding or changing commands.

```bash
uv sync --dev --frozen
make check
make docker-check
```

`make check` runs formatting, linting, strict typing, public-PyPI-only lockfile
validation, reviewed-baseline secret detection, documentation checks, tests, and
package builds. `make docker-check` builds the production-stage image, verifies
its non-root package runtime, and scans the Dockerfile and image.

## Change Validation

- Add focused tests for changed behavior and failure paths.
- Run relevant targeted tests while iterating.
- Run `make check` after shared, public, dependency, or infrastructure changes.
- Run `make docker-check` after Docker, packaging, dependency, entrypoint, or
  container-security changes.
- Review for blocking async work, resource leaks, broad exception swallowing,
  leaked browsing data, stale copied configuration, private registry URLs,
  secrets, and unneeded dependencies before finishing.
