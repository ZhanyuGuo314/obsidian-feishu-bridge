# 0.1.0 — initial alpha

Obsidian ↔ Feishu Bridge pairs a user-selected Vault with Feishu Wiki subtrees
through the official Lark CLI. First adoption supports local, cloud or both
endpoints already containing notes. Subsequent synchronization uses independent
baselines, verified block updates and persistent recovery records.

The release includes the `ofb` CLI, installable `obsidian-feishu-sync` Agent
skill, native-resource link projections, translated internal references,
verified images, referenced-file hosting and opt-in code-only monitoring.
Routine comparisons do not invoke a model API.

Supported writes were exercised against real Feishu APIs using a separately
installed wheel, generated notes and attachments, separate test Vaults and
disposable Wiki scopes. See [validation](validation.md) for the eight cases,
the current 70-test synthetic suite and the limits of this evidence.

This alpha pauses ambiguous/conflicting edits, unknown structures and deletion
requests independently. It does not provide full Sheets, Base or whiteboard
editing, nor a server-side atomic write lock. See the README capability table
before choosing a synchronization scope.

Install from the source archive with `python -m pip install .`, or install the
wheel directly. Follow the README for official CLI login and first adoption.
Authentication, Vaults and runtime profiles are not included in release assets.
