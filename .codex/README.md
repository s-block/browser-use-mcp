# Codex cloud environment

Create the Codex cloud environment for `s-block/browser-use-mcp` with the
default `universal` image and these settings:

- Python: `3.12`
- uv: `0.10.4` (installed by the setup script)
- Node.js: `22`
- Setup script: `bash .codex/setup.sh`
- Maintenance script: `bash .codex/setup.sh`
- Environment variables and secrets: none required for validation

The script installs the locked Python development environment, prepares the
pre-commit hooks, and caches the pinned Markdown linter while setup-phase
internet access is available. Normal unit and package checks can keep agent
internet access disabled. Provider-backed browser sessions still require the
relevant task-specific credentials and network policy.

See the [Codex cloud environment documentation](https://developers.openai.com/codex/cloud/environments)
for the environment lifecycle and cache behavior.
