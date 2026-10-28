# Security and private runtime data

The bridge acts as the user authenticated in the official Lark CLI. It does not
read or store access tokens itself. Protect the CLI's native credential store.

Profiles contain note text, cloud identifiers, baselines and conflict snapshots.
Keep them outside the checkout. Do not attach a profile database, setup plan,
authorization response, QR code or unredacted live-test output to a public issue.
Use a synthetic reproduction and remove tenant/Vault identifiers first.

The mutation adapter validates current ancestry against the configured Wiki
root. It accepts only the narrowly required document/media commands. Note
contents are never shell commands or model instructions. The engine never calls
a model API. Wiki permissions and account access remain enforced by Feishu.

Feishu revisions are optimistic preconditions, not a server-side atomic lock.
The bridge reads back the full planned structure and preserves unexpected
intervening versions. If recovery cannot prove the result, it pauses the item.
Retain these records; do not delete SQLite to force an apparently clean run.

This alpha does not automatically propagate deletion or opaque native edits.
Continuous mode is opt-in and does not install an operating-system service.

For a suspected vulnerability, use the repository's private security reporting
facility when available. Do not post credentials or private note content in a
public report. Until a maintainer has enabled private reporting, withhold
sensitive details and request a private contact channel.
