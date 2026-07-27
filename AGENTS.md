# Repository Guidelines

## Project Structure & Module Organization

Quota Glass is a macOS-only, single-user quota dashboard. The FastAPI backend lives in `app/`; provider parsers and live-source caching are under `app/providers/`. The React/Vite frontend is in `frontend/src/`, with `App.tsx` containing the main UI and `styles.css` its styling. Python tests live in `tests/`, and representative provider records belong in `tests/fixtures/`. Runtime SQLite data is written to `data/usage.db` and should not be committed. Design notes and implementation plans are kept in `docs/superpowers/`.

## Build, Test, and Development Commands

- `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`: create the Python 3.9 environment and install backend/test dependencies.
- `cd frontend && npm install`: install pinned frontend dependencies.
- `./run.sh`: start FastAPI on `127.0.0.1:8000` and Vite on `127.0.0.1:5173`.
- `.venv/bin/python -m pytest`: run the complete backend test suite. Use `tests/test_providers.py::test_name` to target one test.
- `cd frontend && npx tsc --noEmit && npm run build`: type-check and build the production frontend.

## Coding Style & Naming Conventions

Use four-space indentation and `snake_case` for Python functions/modules; classes use `PascalCase`. Retain Python 3.9 compatibility: prefer `typing.Optional`, `List`, and `Dict` over `X | None`. Keep `run.sh` compatible with macOS Bash 3.2. TypeScript uses two-space indentation, semicolons, double quotes, `camelCase` values, and `PascalCase` components. No formatter or linter is configured, so match adjacent code.

## Testing Guidelines

Pytest 8 and `pytest-asyncio` are used. Name files `test_*.py` and tests `test_<behavior>`. Mark async tests explicitly with `@pytest.mark.asyncio`. Tests must use `tmp_path`, fixtures, and injected settings—not real usage directories, Keychain, network services, or desktop notifications. Update frontend types whenever Pydantic response models change.

## Commit & Pull Request Guidelines

History uses short, imperative, sentence-case subjects such as `Improve README usability and accuracy`. Keep commits focused. Pull requests should explain behavior and motivation, link relevant issues, list verification commands, and include screenshots for visible UI changes. Call out configuration, provider-source, schema, or alert-lifecycle changes explicitly; preserve local fallbacks and validate live payloads before caching.

## Security & Configuration

Keep live provider integrations opt-in. Never commit OAuth tokens, SMTP passwords, local session records, or `data/usage.db`. Document new environment variables in `README.md` and make them injectable through `Settings` for testing.
