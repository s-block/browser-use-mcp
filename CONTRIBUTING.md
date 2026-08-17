# Contributing

Thank you for contributing to `browser-use-mcp`. Changes should keep browser
automation typed, asynchronous, bounded, minimal, and safe with sensitive data.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening an issue

- Search existing issues first.
- Use public test pages or sanitized fixtures.
- Never include credentials, cookies, browser profiles, private URLs, form
  values, or private page content.
- Report suspected vulnerabilities through the private process in
  [`SECURITY.md`](SECURITY.md), not through a public issue.

## Development setup

The project supports Python 3.12 through 3.14 and uses
[uv](https://docs.astral.sh/uv/) for dependency management. The complete checks
also require Node.js for Markdown validation and Docker for container
validation.

```bash
git clone https://github.com/s-block/browser-use-mcp.git
cd browser-use-mcp
uv sync --dev --frozen
uv run pre-commit install
```

## Making changes

- Keep changes focused and preserve existing public contracts unless a change
  is necessary and documented.
- Fully type new and changed Python.
- Keep network and browser-control paths asynchronous and preserve cancellation.
- Add focused tests for new behavior and failure paths.
- Update documentation when behavior or configuration changes.
- Do not add real credentials, sessions, or private browsing data to fixtures,
  logs, screenshots, documentation, commits, or issue discussions.
- Pin Docker base images by version and digest and GitHub Actions by full commit
  SHA.

## Validation

Run the complete local checks:

```bash
make check
```

For Docker, packaging, dependency, entrypoint, or container-security changes,
also run:

```bash
make docker-check
```

State which checks were run in the pull request. Explain any check that could
not be run.

## Pull requests

Pull requests should:

- explain the problem and the chosen solution;
- identify security, privacy, compatibility, or operational effects;
- include relevant tests and documentation;
- keep generated and unrelated changes out of the diff; and
- pass all required checks.
