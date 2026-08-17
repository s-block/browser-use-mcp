# Changelog

## Unreleased

### Changed

- Product documentation now leads with persistent authenticated profiles,
  semantic and deterministic controls, a short setup path, safe example tasks,
  and a profile-persistence demo guide.
- Package metadata now includes browser-agent discovery keywords and complete
  project documentation, source, issue, changelog, and security links.

### Added

- Initial Python package, test, and distribution scaffold for
  `browser-use-mcp`.
- Fully async MCP 2 Streamable HTTP server for Stagehand 4 browser automation
  on Steel Chromium sessions.
- Optional stdio transport with a long-lived Docker MCP Gateway server entry.
- Temporary and named persistent browser sessions with bounded concurrency,
  cross-process one-writer leases, and provider upload readiness checks.
- Optional bearer client-secret authentication with stable configured tenant
  identities that remain unchanged during credential rotation.
- Vaultlet-backed encrypted SQLite profile storage with bounded key enumeration
  and independent multi-process access.
- Generic request-scoped OpenAI-compatible Chat Completions integration with
  environment defaults and dedicated override headers.
- Semantic navigation, action, observation, and extraction tools plus a
  constrained lower-level selector control tool.
- Focused tests for authentication, encryption, tamper detection, persistence,
  locking, client isolation, async session serialization, and LLM precedence.
- Strict Ruff and mypy checks, secret detection, Markdown validation, and
  public-PyPI-only lockfile enforcement.
- Python 3.12 through 3.14 CI, dependency review, CodeQL analysis, and pinned
  dependency update configuration.
- Minimal non-root Alpine service image with a health check, persistent data
  volume, package checks, and Dockerfile and image vulnerability scans.
- Contributor guidance, a security policy, structured issue forms, and a pull
  request checklist for browser automation changes.
- Explicit headful desktop Steel Cloud identities with coherent fingerprinting,
  bounded direct-control timing, and saved-profile dimension reuse.
- Secret-safe dedicated proxy configuration with persistent profiles bound to a
  stable named network identity.
- Optional automatic CAPTCHA solving with bounded status polling and explicit
  failure handling below the Stagehand control layer.
- CDP request interception for initial navigations, redirects, subresources,
  frames, and workers, with fail-closed public-egress configuration.
- Exact allowlisting for request-scoped model provider origins and HTTPS-only
  provider endpoints outside loopback by default.
- TLS-termination enforcement for non-loopback MCP HTTP binds and deployment
  guidance for private application networks and public reverse proxies.
- Secret-safe authentication, session, browser-network, model-egress, and
  extension-cleanup audit events.
- A content-addressed, process-owned Stagehand extension upload with a pinned
  Chrome extension identity instead of name-only provider artifact reuse.
