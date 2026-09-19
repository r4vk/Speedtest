# project wiki — 2026-09-19

This wiki is auto-managed by the karpathy-wiki plugin. Most of the time
you should not edit files directly — captures, ingestion, and page
writes happen through the plugin's tooling.

## Where to drop files

**`inbox/`** — the universal drop zone for ingestion. Put anything
that should become a wiki page here:

- Obsidian Web Clipper exports (configure your template's "Note
  location" to `inbox` and "Vault" to this wiki — see "Web Clipper
  setup" below).
- Research reports from subagents (the agent typically moves these
  for you).
- Manual file drops: downloaded articles, PDF→markdown exports,
  meeting notes.

After you drop a file, ingestion happens on the next agent session
start (silent), or immediately if you run `wiki ingest-now <this-wiki-path>`.

**Do NOT put files in `raw/`.** That directory is the ingester's
archive — every file there has a manifest entry tracking origin,
sha256, and which wiki pages reference it. If you put a file there
by accident, the next ingest will move it to `inbox/` and process
it normally (a WARN gets logged, but no data is lost).

## Web Clipper setup

In Obsidian Web Clipper settings → Templates → (your template) →
Location:

- **Note name:** `{{title}}` (or whatever filename pattern you prefer)
- **Note location:** `inbox`
- **Vault:** select this wiki's directory

That's it. Clip a page; the file lands in `inbox/`; next agent session
ingests it.

## Other top-level directories

- `concepts/`, `entities/`, `queries/`, `ideas/` — wiki content,
  written by the ingester. Editing pages directly is allowed but the
  ingester will eventually re-rate / re-link them.
- `archive/` — old content the ingester decided to retire.
- `raw/` — the ingester's archive (see above).
- `.wiki-pending/` — pending captures (transient).
- `.locks/` — concurrency primitives (transient).
- `.raw-staging/` — ingester staging area (transient).
- `.manifest.json`, `.ingest.log`, `.ingest-issues.jsonl` — machine
  state.

## Schema

See `schema.md` for the live category list, tag taxonomy, and
thresholds.
