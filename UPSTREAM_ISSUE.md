# Draft issue for glass-bead-labs/zotero-agent-skill

**Title:** "Always Allow" in Zotero 10's write dialog creates a permanent key; plus a licence question and hardening PRs

---

Hi Dav,

thank you for zotero-agent-skill. The README's reasoning (a skill plus one CLI you can audit instead of an MCP server, verify before import, regular tags, subcollections, notes written for humans) convinced me to use it for literature screening with a local Zotero 10 library. Three things, most important first.

## 1. "Always Allow" should never be recommended

Zotero's local-API approval dialog has three buttons: Allow, Always Allow and Deny. Zotero 10's `chrome/content/zotero/xpcom/server/server_localAPI.js` shows what the middle one does:

- `addLocalAPIKey(appName, remember)` stores the key with `remember: true` in `localAPIKeys.json` in the Zotero profile directory, in plain text.
- `consumeLocalAPIKey()` deletes single-use keys on first use, but returns remembered keys without deleting them. Such a key never expires.
- Any program that reads that file can then write to the library without a dialog.

The skill asks for a fresh key per request anyway, so "Always Allow" gains it nothing and costs the user the review gate. I suggest SKILL.md tell the agent to recommend "Allow", never "Always Allow". I'd also suggest `local_authorize()` warn when the response has `"remember": true`, saying how to revoke the key: quit Zotero, remove those entries from `localAPIKeys.json`, restart. My fork does both.

## 2. Licence

The repository has no LICENSE file, so by default nobody else may reuse or redistribute the code. Which licence do you intend it to be under? I have not added one to my fork, since that is your decision.

## 3. Hardening changes, offered as PRs

Each change is one commit on branch `hardening` in my fork (<https://github.com/ischicker/zotero-agent-skill/tree/hardening>). They keep the structure: stdlib only, one CLI file, SKILL.md as the agent's instructions. Each is covered by `selftest` without Zotero, and the write paths were also exercised end to end against a stub of the local API. Take any subset; I'd open them as separate PRs in this order.

**Bug fixes (small, independent):**

| PR | Commit(s) | What |
| --- | --- | --- |
| A | `b0f784b` | `ZOTERO_HOST` must be loopback `http://` with a port (no userinfo, no path); `localhost` becomes `127.0.0.1`. The read guard trusts everything under `HOST`, so a remote value widened it. |
| B | `f4a2550`, BOM part of `d0188ad` | Every file is read and written as UTF-8, with a BOM tolerated on input. `json.load(open(spec))` used the Windows code page and mangled umlauts and Nordic letters on their way into Zotero. stdout uses `errors="replace"`. |
| B2 | `e792675` | `organize` replaced an item's whole tag list with the spec's tags. On an existing paper dragged into the collection, that deleted tags from other projects. It now adds the spec's tags and keeps all others; only `prio:`/`status:`/`decision:` are single-valued (only relevant with the screening convention, so that part can be dropped). |
| B3 | `e15a6d9` | Wait 10 min instead of 3 for the approval dialog, and ring the terminal bell. Live, Zotero's dialog often opened behind other windows, and a missed one leaves an import half-done. |
| B4 | `1f41726` | `verify` crashed with a traceback when arXiv answered a burst with HTTP 429 and a read then timed out (`TimeoutError` is not a `URLError`). arXiv calls are now 3 s apart, every `OSError` is caught per entry, and transient failures get status `error` (re-run) instead of `notfound`. |
| B5 | `746fa4c` | Author comparison in `verify` and `resolve` folds accents (NFKD plus a mapping for ø, ł, ß, đ, ...). `norm()` deleted non-ASCII letters, so a claimed "Daví-Arderius" or "Gonçalves" did not match Crossref's "Davi-Arderius"/"Goncalves" and correct papers came out as `MISMATCH`. Titles are compared as before. |

`d0188ad` also contains an unrelated one-line output change for `plan`, so PR B needs a split cherry-pick. I kept the history as it is.

**Design changes (each builds on the previous ones):**

| PR | Commit(s) | What |
| --- | --- | --- |
| C | `0b886d6`, `ad98a25` | Remove the generic `write` command (arbitrary JSON to the local API). Add a narrow `note LIB FILE` instead: child notes only, with dedupe. |
| D | `f33ae35`, `e54b9ce` | Write allowlist in a user-owned file outside the repo; fails closed. It is checked per object in `local_write()` and per target for the connector, with a backstop in `http()` that refuses any write no check covered. `trash` refuses items that are also in a non-allowlisted collection. |
| E | `422b564`, `568360c` | Plan hash. `plan` prints a SHA-256 of exactly what `import` would send, including the allowlist file's fingerprint; `import --confirm HASH` refuses on a mismatch. The same applies to `organize` and `note`. Dry runs print library, collection and allowlist. |
| F | `03d3314` | Selected-collection preflight. `saveItems` saves into the row selected in the Zotero window (`getSaveTarget()` in `server_connector.js`) before `updateSession` moves the items. `import` and `attach --new` now refuse unless `getSelectedCollection` reports an allowlisted collection, checked again right before every `saveItems`. |
| G | `0ddb02c` | PDFs off by default (`--pdf`): https only, redirects re-checked, download stopped at the cap. The spec `pdf` URL needs `--allow-spec-pdf`. `attach` only reads files from directories listed in the allowlist file. |
| H | `97b5668` | Write log: one JSON line per write request (metadata and payload SHA-256, no content), plus a `log` command. |
| I | `1d34ae9` | `resolve`: candidate DOIs/arXiv IDs for entries with `claimed` but no `id`, from Crossref `query.bibliographic` and arXiv. It runs under verify's guard and writes a proposal file without editing a spec. |
| K | `a8e38ec` | Read-only `export`: a collection with its subcollections as CSV, or as BibTeX through Zotero's own translator (`format=bibtex`). The `annote` fields (child notes) are dropped unless `--keep-notes`. `--out` only creates new files. |
| L | `245eeb0` | `references/screening.md`: a convention for screening one paper's literature (blocks, `prio:`/`status:`/`decision:` tags). Optional, and maybe too specific for upstream. |
| M | `01fc1be` | `file LIB KEY... --coll COLL`: add an existing paper (filed under another project, or unfiled) to an allowlisted collection. Membership only; the one narrow exception to the allowlist for items outside it. |
| N | `45760dc` | `bundle`: copy a collection's PDFs plus `manifest.csv` into a new folder inside an `export_dirs` entry of the allowlist file, e.g. for a notebook tool. Zotero is only read; only files from Zotero's own storage are copied, never linked files. |
| J | `080447e`, `dcd9fbc` plus later doc edits | Docs: SKILL.md, read-only-safety.md and gotchas.md match the code. The README gets a "Based on" section (only relevant to the fork). |

## Open points I could not settle

- A selection change in Zotero in the moment between the preflight and `saveItems` cannot be caught from outside Zotero.
- The allowlist file can't be protected from a process running with the user's own permissions. In Claude Code, deny rules on `Read`/`Edit` stop the file tools and shell commands that name the file, but not a script that opens it, and the OS sandbox is not available on native Windows.

Best,
Irene Schicker
