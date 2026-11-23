# Literature screening convention

A convention for using the skill to screen literature for one paper. Nothing in the script depends on these exact names except the single-value tag prefixes (see Tags); the rest is a shared vocabulary so that specs, the library and exports agree.

## Structure: one collection per paper, subcollections per block

```
<Paper collection>            e.g. "JoS SDG7 Perspective" (allowlisted, "subcollections": true)
  Block 1                     the paper's first argument block
  Block 2
  Block 3
  Abgrenzung                  closest neighbours the paper delimits itself from
```

- The paper collection itself holds only the standalone screening note (provenance, date, what the tags mean). Every reference goes into exactly one block subcollection via the spec's `"subcollection"`; `organize` moves it out of the parent.
- Blocks are numbered because the paper's order matters here (an exception to "don't number subcollections"). Keep the names stable; renaming a block in Zotero means updating the spec too.
- `organize` creates missing block subcollections, which needs `"subcollections": true` on the paper's allowlist entry.

## Tags: three single-value prefixes, plus the usual ones

| Prefix | Values | Meaning |
| --- | --- | --- |
| `prio:` | `muss`, `sollte`, `optional` | how much the paper needs it |
| `status:` | `abstract`, `volltext`, `gelesen`, `unbestaetigt` | how far it has been read; `unbestaetigt`: existence or content not yet confirmed |
| `decision:` | `offen`, `einschliessen`, `ausschliessen` | screening outcome |

- Exactly one tag per prefix and item. `organize` enforces this for `prio:`, `status:` and `decision:`: a spec tag with one of these prefixes replaces an existing tag with the same prefix (`SINGLE_VALUE_PREFIXES`). Every other tag on the item is kept, including tags from other projects.
- Also used: a paper tag on every item (e.g. `JoS SDG7`), `needs review` on anything holding LLM text (the human removes it after checking), `caveat: preprint` when only a preprint version was found, `unverified metadata` (set by the script for `manual` entries).
- No tag repeats a block name: the block is the subcollection.

## Spec entry

```json
{
  "id": "10.1002/met.2089",
  "claimed": {"title": "Characterizing renewable energy compound events across Europe ...", "authors": "Otero et al.", "year": 2022},
  "tags": ["prio:muss", "status:abstract", "decision:offen"],
  "subcollection": ["Block 1"],
  "notes": ["<h1>Claude literature screening: relevance note (LLM-generated, unreviewed)</h1><p>...</p><p><small>Screening list entry: ... ID source: ...</small></p>"]
}
```

The spec's top-level `tags` (e.g. `["JoS SDG7", "needs review"]`) go on every entry. Keep one note per entry; its footer says where the entry and its identifier came from.

## Workflow

1. Entries without an identifier go into a pending file (`needs_id`). `resolve pending.json`, show the user a table (claimed title | best candidate | similarity | ID), and copy only approved IDs into a copy of the spec. A candidate that is only a preprint: check Crossref's `is-preprint-of` relation for the journal version before settling for the preprint.
2. `verify spec.json`, report every mismatch and every entry without claimed authors (title-only check). Never set `accept_mismatch` without the user.
3. `plan spec.json`, show it, the user approves `import ... --confirm HASH` (with the paper collection selected in Zotero).
4. Changing a screening decision: edit the entry's tags in the spec (e.g. `decision:offen` to `decision:einschliessen`, `status:abstract` to `status:gelesen`), `verify`, then `organize spec.json --dry-run` and `organize ... --confirm HASH`. Editing tags by hand in Zotero works too, but the spec then no longer describes the library and the next `organize` will set the spec's values again.
5. A paper already in the library elsewhere is skipped by `import`. `file LIB KEY --coll "<Paper collection>" --dry-run`, then `--confirm HASH`, adds it to the paper collection (or the user drags it there in Zotero); then `organize` adds the spec's tags (keeping its others) and moves it into its block, and `note` adds the relevance note. Duplicates in the library have to be merged in Zotero first; the local API has no merge.

## Exports

- Table for screening: `export "My Library" "<Paper collection>" --out screening.csv`, then filter the `tags` column, e.g. for `decision:einschliessen`; the `collections` column shows the block.
- Bibliography: `export "My Library" "<Paper collection>" --format bibtex --out refs.bib`. The child notes (`annote`) are left out unless `--keep-notes`; keep it that way for anything that leaves your machine.
