#!/usr/bin/env bash
set -euo pipefail

uv python install 3.12
uv sync --python 3.12 --dev --frozen
uv run pre-commit install-hooks
npx --yes markdownlint-cli2@0.19.0 --version >/dev/null
