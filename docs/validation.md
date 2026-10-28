# Version 0.1.0 validation

Validated on 2026-10-28 with Windows, Python 3.14.8 and official Lark CLI
1.0.97. This is an alpha release; the supported boundaries are in the README.

## Installed distribution

- Built a wheel and source archive, then scanned both public manifests.
- Installed the wheel in a second, noneditable virtual environment outside the
  checkout. Imports resolved to that environment's `site-packages`.
- Ran the CLI from an unrelated directory and exported the embedded Agent
  skill to a temporary directory.
- Extracted the source archive and ran the original 64 synthetic tests against the
  installed wheel. All passed. Lint, formatting, skill validation and the
  canonical/embedded skill consistency check also passed.
- Subsequent local privacy review added six regression tests, bringing the
  current synthetic suite to 70 passing tests. These cover release-data guards
  and importing cloud images into the generic attachment directory.
- Reinstalled the revised wheel without an editable source link and reran all
  70 tests from the extracted source archive. All passed. Its package source
  and embedded skill matched the reviewed checkout byte for byte.

## Real Feishu API acceptance with synthetic data

The installed wheel exercised actual Feishu APIs through an authenticated
official Lark CLI. Test content consisted of generated Markdown notes, small
PNG images and a synthetic one-page PDF. The tests used separate local Vaults,
new disposable Wiki child scopes and dedicated profile databases.

The harness kept cloud identifiers, raw logs, note bodies and recovery records
outside the public repository. Authentication remained in the official CLI's
credential store. The reviewed aggregate is [live-result.json](live-result.json).

| Case | Observed result |
|---|---|
| Local start | Two notes, nested folders and cyclic references synchronized; repeat wrote nothing |
| Cloud start | Note imported, native Sheet linked and a later cloud edit pulled |
| Both endpoints populated | Unique equal content paired; differing same-name content paused; independent notes synchronized |
| Attachments | Local PDF hosted without downloading PDF payloads; image-only byte changes synchronized; repeat wrote nothing |
| Scoped writes | Ordinary mutations succeeded through current ancestry checks; repeats wrote nothing |
| Large note | 130 paragraphs preserved in order through bounded patches; repeat wrote nothing |
| Keep both | Paused initial collision resolved into two notes without resetting other baselines; repeat wrote nothing |
| Native cloud image | Ordinary image filename imported with exact bytes into `_attachments/feishu/`; later local text edit synchronized while preserving the image; repeats wrote nothing |

The synthetic scope tests additionally verify denial of missing parents,
out-of-scope nodes, root-body writes and nodes moved after earlier validation.
The engine made zero model API calls. This does not count the agent's work
developing or interpreting the tests.

The first seven live cases preceded the privacy cleanup. The eighth case was
run afterward against the revised, independently installed wheel, using a new
synthetic fixture below the disposable parent. It verifies the native-image
import and subsequent local edit through the real CLI. Checkpointed earlier
cases were not counted as having been rerun in that follow-up.

## Limits of this evidence

GitHub Actions runs the synthetic suite on Windows, Linux and macOS with Python
3.11 and 3.14. Consult the [workflow results](https://github.com/ZhanyuGuo314/obsidian-feishu-bridge/actions/workflows/ci.yml)
for commit-specific outcomes. The live run used one Feishu account and CLI
version. It does not establish behavior for every tenant, permission setup or
native component. Opaque content and conflicting edits remain per-item review
boundaries; deletion propagation is not implemented.

The privacy scanner is an export guard. Its passing result supplements a
review of the file manifest and does not prove arbitrary prose is nonpersonal.
