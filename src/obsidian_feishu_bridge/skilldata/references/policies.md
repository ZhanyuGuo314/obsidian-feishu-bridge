# Representation and ownership policies

The endpoints are peers for supported note content. Native resources may have a
single hosting endpoint with a local projection; this is explicit policy, not
silent loss of unsupported features.

| Type | Behavior |
|---|---|
| Note paragraphs, code, equations, ordinary tables | Deterministic conversion and minimal verified changes |
| Images | Native embeddings, byte hashes, transfer on changes |
| Cloud PDFs and ordinary attachments | Local link note; no PDF payload download |
| Local referenced PDFs/files | Upload once per content hash within the selected scope; translate the reference to a hosted URL |
| Sheets and Base | Native cloud object with a local link; do not pretend records/views are Markdown tables |
| Whiteboards and known native components | Cloud block remains native; local projection links to that block |
| Unknown native content | Pause that item, preserving snapshots |

File content changes create a separately hosted version and do not delete the
old object. Shared references use content identity within one target scope;
the same data is not silently hosted in a different scope with different permissions.

The Wiki root is a container. A document with children becomes a local folder
with an `_index.md` for its body and child notes underneath. Generated local
folders use Wiki container nodes. Duplicate titles and names invalid on Windows
receive stable suffixes; an initial collision with an existing local note is a
review item. Nothing automatically reorganizes the user's knowledge base.

First-adoption modes:

- `local`: pair uniquely equal items, register unmatched local notes, leave
  unrelated existing cloud objects untouched.
- `cloud`: pair uniquely equal items, register unmatched cloud objects, preserve
  unrelated local notes without automatically pushing them.
- `both`: pair unique equal content; register distinct notes on both sides;
  pause same-path differing content unless explicitly told to keep both.

No mode is a destructive "master wins" policy. Once paired, either endpoint
can change supported note content. Concurrent differing edits pause the note;
neither endpoint is chosen by wall-clock time.

Profiles and SQLite databases live in the user's application data directory,
or under explicit `--data-home` / `OFB_DATA_HOME`. They contain private note
snapshots. Keep them outside the repository and retain them across restarts.
