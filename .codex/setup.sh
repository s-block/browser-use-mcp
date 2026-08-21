#!/usr/bin/env bash
set -euo pipefail

required_uv_version="0.10.4"
installed_uv_version="$(uv --version 2>/dev/null || true)"
if [[ "${installed_uv_version}" != "uv ${required_uv_version}"* ]]; then
    curl -LsSf "https://astral.sh/uv/${required_uv_version}/install.sh" | sh
    export PATH="${HOME}/.local/bin:${PATH}"
    hash -r
fi

uv python install 3.12
uv sync --python 3.12 --dev --frozen
uv run --frozen pre-commit install-hooks
npx --yes markdownlint-cli2@0.19.0 --version >/dev/null
