# Contributing

Use Python 3.11+ and an isolated environment:

```sh
python -m pip install -e '.[dev]'
python -m ruff check --select F,I src tests scripts
python -m ruff format --check src tests scripts
python -m unittest discover -s tests -v
python scripts/sync_skill.py --check
python scripts/check_public.py
```

Keep tests synthetic. Add behavior-based regressions for matching, conflicting
edits, references, crash recovery and API response differences. Do not add
assertions that merely match a success message. A simulated write does not
prove live API behavior.

Live tests are opt-in and require an explicitly disposable Wiki parent:

```sh
# Set these in the current process, never commit their values:
# OFB_LIVE_ROOT: disposable Wiki parent token
# OFB_LIVE_CLI: absolute path to the authenticated official CLI
# OFB_LIVE_OUTPUT: private directory outside this repository
python tests/live_acceptance.py
```

The harness creates identifiable child scopes and synthetic notes. It keeps
evidence outside the repository and does not delete arbitrary cloud objects.
Only publish the sanitized aggregate result after reviewing its keys. Routine
CI has no Feishu credentials and must never run live writes on pull requests.

The canonical agent instructions are in `skills/obsidian-feishu-sync`. Run
`python scripts/sync_skill.py` after editing them; this updates the copy embedded
in the Python wheel. Keep the capability table accurate and do not silently
expand native-resource ownership or mutation permissions.

Before a release, build the wheel and source archive, scan both with
`scripts/check_public.py --archive PATH`, and install the wheel in a second
environment outside the checkout. Review the Git manifest as well as the scan.
The privacy checker cannot infer whether arbitrary prose is personal.

Tagging `v` followed by the package version triggers the alpha release workflow.
It requires successful main-branch CI for that exact commit, scans the built
archives, verifies an isolated wheel installation, and creates a draft release.
Assets are downloaded and compared before the draft becomes a prerelease.
An existing draft is resumed only when it carries the same workflow/commit
marker; unrelated or already published releases are preserved.
