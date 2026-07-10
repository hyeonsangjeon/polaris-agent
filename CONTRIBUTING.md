# Contributing

Thank you for helping make Polaris safer and easier to operate.

## Before you start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md) and
  [security policy](SECURITY.md).
- Use an issue for a bug or behavior proposal. Report vulnerabilities privately.
- Keep changes focused. Durability claims must identify crash windows and must not
  claim arbitrary exactly-once execution.
- Preserve attribution recorded in `THIRD_PARTY_NOTICES.md` and provenance data.

## Development setup

Prerequisites: Python 3.11+, uv, Node.js 20+, npm, stable Rust, and platform
dependencies required by Tauri.

```bash
git clone https://github.com/hyeonsangjeon/polaris-agent.git
cd polaris-agent
uv sync --dev
cd apps/desktop && npm ci && cd ../..
```

For Entra-specific development:

```bash
uv sync --dev --extra azure
```

## Exact checks

Python:

```bash
uv run ruff check .
uv run mypy
uv run pytest --cov=polaris --cov-report=term-missing --cov-report=xml
```

Frontend:

```bash
cd apps/desktop
npm ci
npm test
npm run build
```

Rust:

```bash
cd apps/desktop/src-tauri
cargo fmt --all -- --check
cargo test --locked
cargo check --locked
```

Container configuration:

```bash
docker compose config --quiet
docker compose build
```

Documentation/example validation:

```bash
uv run python -m json.tool examples/config.ollama.json >/dev/null
uv run python -m json.tool examples/config.foundry-router-key.json >/dev/null
uv run python -m json.tool examples/config.foundry-router-entra.json >/dev/null
uv run python scripts/demo/create_fixture.py --output .demo-check
rm -rf .demo-check
```

## Pull requests

Fill every section of the pull request template. Add tests for changed behavior,
describe recovery implications, and include screenshots only when the UI changes.
Never include secrets or unredacted run artifacts. Maintainers may ask for a
smaller change if review would otherwise obscure safety semantics.
