# Representation-aware synchronization

## Layers

1. **Agent skill** collects setup choices and interprets compact reports.
2. **Profile and adoption planner** selects scopes and proposes initial matches.
3. **Bridge policies** project native resources and translate references.
4. **Synchronization engine** compares independent baselines and plans minimal patches.
5. **Allowlisted Lark adapter** authenticates through the official CLI and checks scope.
6. **SQLite and snapshot journal** persist observed state, operations and recovery evidence.

There is no language-model dependency in the engine. A new agent invokes the
same CLI rather than reconstructing decisions from chat history.

## Identity and topology

`ofb_id` identifies a note independently of its filename. Each pair records a
local path, cloud document token, Wiki node token, target scope and both verified
representations. A Wiki node and its document object are distinct identifiers.
Resource identity also includes the target scope: a shared local file is not
silently hosted under a different permissions boundary.

The selected Wiki root acts as a container. A document with children projects
to a folder and an `_index.md` for its own body. Its children are notes or
resource entries under that folder. Existing verified pair paths remain stable
when later child nodes appear. Local folders receive Wiki container nodes.
Names invalid on Windows and duplicate sibling titles receive deterministic
normalization/suffixes; they do not overwrite each other.

Internal note references become cloud URLs after node identity is allocated.
New notes are staged before body writes so mutually referencing notes can
resolve their targets. Local referenced files are hosted once per scoped content
hash. Cloud-only resources project to links. Original opaque cloud objects remain
untouched while supported surrounding note content can synchronize.

## Adoption

There is no shared baseline on first use. The plan records local source hashes,
remote versions and snapshots, scope policy and its fingerprint. A unique
structurally equal match can be paired automatically. Ambiguous equal copies,
same-path differing content and unresolved references do not establish identity.

An accepted plan builds its initial SQLite state privately, then publishes the
completed database. Interrupted adoption cannot activate partially imported
baselines. Endpoint writes happen only during a subsequent authorized sync.
`local` and `cloud` select which unmatched initial items to adopt; paired items
subsequently synchronize in both directions.

## Incremental comparison

Local notes compare to their own baseline bytes. Known cloud objects first use
batched metadata; candidate bodies are fetched as complete DocxXML plus their
reference sidecars. Periodic discovery finds unknown nodes, and a low-frequency
body sweep covers metadata timing limitations. Metadata is a signal, not proof
of content equality.

Markdown and DocxXML are different representations. The engine compares typed
structure and maps changed blocks, preserving code, formulas, links, images and
tables. Incoming cloud changes update corresponding local source spans, so
endpoint-specific unchanged presentation is retained.

## Writes and recovery

Each operation records its source hash, original cloud state, expected revision,
steps and postconditions before mutating. Large insertions use bounded steps;
every step is read back. Baselines advance only after successful verification.
New images are downloaded and byte-hashed; PDF payloads are never downloaded.

CLI image insertion can create an intermediate image-placeholder revision. It
is accepted only when every intervening structure equals the planned tree
except newly declared image slots, followed by a complete final tree and byte
verification. Unrelated intervening content is preserved and paused.

Lost mutation responses are not blindly retried. The same operation identifier
and staged marker are inspected on restart. Unknown results without positive
evidence remain paused. A write may have completed when an error occurs; deleting
state can produce duplicate content and defeats the recovery protocol.

Concurrent differing edits, ambiguous identity, deletions and unsupported native
changes are isolated per item. The engine does not choose a winner by timestamp.
Feishu supplies no strict atomic lock used by this project; optimistic checks,
full postconditions and historical-version preservation are the declared model.
