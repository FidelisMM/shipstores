# Contributing

Thanks for helping! Store consoles change every few months, so real-world fixes are the most valuable contributions.

## Setup

```bash
uv sync
uv run python -c "import asyncio; from shipstores.server import mcp; print(len(asyncio.run(mcp.list_tools())), 'tools')"
```

You don't need store credentials to work on most of the code. To try tools against real stores, see "Credentials" in the README and use a test app.

## What we love to merge

- **A quirk you hit in production**: a rejection reason, an undocumented API rule, a console change. Add the fix *and* a line in the README's "Hard-won lessons".
- **New tools** for steps you still do by hand (open an issue first with the store endpoint or console flow).
- **Console labels in other languages**: console automation matches UI text literally (`pt-BR` and `en-US` today). Adding your console language is a great first PR.
- Docs, examples, workflows for other stacks (Flutter, native Xcode/Gradle, Capacitor).

## Guidelines

- One tool = one store operation. Tools that publish or submit must say so in the first line of their docstring ("External action").
- Tool docstrings are what the agent reads: be precise about preconditions, side effects and the next step.
- Never commit credentials, account ids, bundle ids of real apps or screenshots with personal data.
- Keep console automation in its own module (`apple_console.py`, `apple_review.py`, `play_console.py`) so breakage stays contained.
- Run before opening a PR (the same checks CI runs):
  ```bash
  uv run --with ruff ruff check src tests --select E9,F
  uv run python -m unittest discover -s tests -v
  uv run python -W error -c "import pathlib; [compile(p.read_text(), str(p), 'exec') for p in pathlib.Path('src/shipstores').glob('*.py')]"
  ```

## Reporting a broken console flow

Open a "Console flow broke" issue with the tool name, the store, your console language and the error. Screenshots help — blur account names.
