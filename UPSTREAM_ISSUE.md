# Draft issue for glass-bead-labs/zotero-agent-skill

**Title:** Licence question, and an offer of hardening changes as PRs

---

Hi Dav,

thank you for zotero-agent-skill. The README's reasoning (skill + one auditable CLI instead of an MCP server, verify before import, regular tags, subcollections, human-first notes) convinced me to use it for literature screening with a local Zotero 10 library.

**Licence.** The repository has no LICENSE file, so by default nobody else may reuse or redistribute the code. Which licence do you intend it to be under? I have not added one to my fork, since that is your decision.

**Hardening changes.** Before using it I made a few changes to the write side, on a branch in my fork, one commit each. They keep the structure (stdlib only, one CLI file, SKILL.md as the agent's instructions), and `selftest` covers each of them without Zotero. I would be glad to open them as separate PRs if you are interested, or to drop any you would rather not have:

1. **Loopback-only `ZOTERO_HOST`.** The read-only guard trusts every URL under `HOST`, which comes from the environment. The script now refuses to start unless it is `http://` + `127.0.0.1` / `[::1]` / `localhost` + port, and rewrites `localhost` to `127.0.0.1`.
2. **Remove the generic `write` command.** Arbitrary JSON to the local API cannot be bounded by any later check. `local_write()` stays for `organize`, `trash` and `attach`. A narrow `note LIB FILE` (child notes only: `parent`, `note`, `tags`; dedupes) replaces the note use case.
3. **Write allowlist.** A user-owned JSON file outside the repo lists the collections writes may touch, and every write path fails closed without it. It is enforced in `local_write()` (per object, before the dialog), in a connector-target check, and in a backstop in `http()` that refuses any non-GET request no check covered. New read-only command `allowlist`.
4. **Plan hash.** `plan` prints a SHA-256 over a canonical serialisation of exactly what `import` would send. `import` requires `--confirm HASH` and refuses on a mismatch. The same goes for `organize --dry-run` / `--confirm` and for `note`.
5. **PDFs opt-in.** `import --pdf` fetches https only (redirects re-checked; no private or loopback hosts), reads only up to the cap, and still requires `%PDF-`. The spec's `pdf` URL needs `--allow-spec-pdf`. `attach` only reads files inside directories listed in the allowlist file, compared after resolving symlinks.
6. **Write log.** One JSON line per write request (time, command, library, collection, endpoint, object count, payload SHA-256, HTTP status), no content. New read-only command `log`.
7. **Docs.** SKILL.md, read-only-safety.md and gotchas.md match the code. SKILL.md now tells the agent to recommend "Allow", never "Always Allow": per `server_localAPI.js`, "Always Allow" stores a never-expiring key in plain text in `localAPIKeys.json`. The script warns if Zotero reports such a key.

The branch is at: <FORK URL>, branch `hardening`.

Things I could not settle, in case you have views: the connector's `saveItems` first saves into the collection selected in the Zotero window, before `updateSession` moves the items, and I found no way to avoid that. Also, the allowlist file cannot be protected from a process running with the user's own permissions.

Best,
Irene Schicker
