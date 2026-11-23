#!/usr/bin/env python3
"""zotero.py - verify-then-import helper for a running Zotero 10+ desktop app.

Stdlib only. Talks to Zotero on 127.0.0.1:23119:
  * /api/...        local API; reads are open, writes need a per-request approval click in Zotero
  * /connector/...  browser-connector endpoints; `import` uses these (items, child notes, PDFs; no dialog)

Read-only (GET to Zotero on loopback only; enforced at runtime, see GUARD and canonical_host):
    status | libs | colls LIB | ls LIB [COLL] | find LIB QUERY | notes LIB ITEM... | path LIB (ITEM... | --coll COLL)
    | plan SPEC [flags] | where [SPEC] | allowlist | log [-n N] | selftest
Network read, one local file (GET only, to Zotero + Crossref/arXiv/OpenAlex; enforced at runtime, see GUARD):
    verify SPEC        resolves identifiers; writes one output file (location: resolved_for()); never touches
                       Zotero. plan/import/organize take the same SPEC; `where SPEC` prints the location.
    resolve FILE       candidate DOIs/arXiv IDs (Crossref, arXiv) for entries with `claimed` but no `id`; writes a
                       proposal file next to the verify outputs and never edits a spec. Sends titles and an author.
Not read-only (each write is checked against the user's allowlist file and logged; see `allowlist`, `log`):
    import SPEC --confirm HASH
                       writes items/notes (PDFs only with --pdf) through the connector (no dialog), then runs
                       `organize`; HASH is what `plan` printed for the same spec and flags
    organize SPEC (--dry-run | --confirm HASH)
                       regular tags + subcollection placement (local-API writes: one dialog to create
                       subcollections, then one per 50 items); idempotent, also fixes earlier imports
    attach LIB FILE (--item KEY | --new ID)
                       local file -> existing item (local-API upload: 3 approval dialogs) or new item resolved
                       from a DOI/arXiv ID (connector: no dialog), in one call
    note LIB FILE      child notes on existing items (local-API write: one approval dialog); --dry-run, --confirm
    trash LIB KEY...   local-API write (one approval dialog per request)

Design rule: the model never types bibliographic metadata. It supplies identifiers
(DOI / arXiv ID); metadata comes from Crossref / arXiv and is compared with what the
source document claimed before anything is written.
"""
import argparse, difflib, hashlib, html, ipaddress, json, mimetypes, os, re, stat, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

DEFAULT_HOST = "http://127.0.0.1:23119"
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")


def host_problem(value):
    """None if `value` is exactly http://<loopback>:<port>[/], else the reason it is refused.

    Zotero's local API and connector only listen on loopback, and the read-only guard trusts every URL under HOST,
    so a ZOTERO_HOST pointing anywhere else would turn the guard into an allowlist for that host."""
    try:
        u = urllib.parse.urlsplit(value)
        port = u.port
    except ValueError as ex:
        return "cannot parse (%s)" % ex
    if u.scheme != "http":
        return "scheme must be http, not %r" % u.scheme
    if "@" in u.netloc:
        return "userinfo (user@host) is not allowed"
    if (u.hostname or "") not in LOOPBACK_NAMES:
        return "host %r is not a loopback address (%s)" % (u.hostname, ", ".join(LOOPBACK_NAMES))
    if port is None:
        return "an explicit port is required"
    if u.path not in ("", "/") or u.query or u.fragment:
        return "no path, query or fragment allowed"
    return None


def canonical_host(value):
    """Validated, canonical HOST. `localhost` becomes 127.0.0.1 so a hosts-file entry cannot redirect it."""
    err = host_problem(value)
    if err:
        sys.exit("refusing to start: ZOTERO_HOST=%r: %s" % (value, err))
    u = urllib.parse.urlsplit(value)
    name = "[::1]" if u.hostname == "::1" else "127.0.0.1"
    return "http://%s:%d" % (name, u.port)


HOST = canonical_host(os.environ.get("ZOTERO_HOST", DEFAULT_HOST))
UA = {"User-Agent": "zotero-skill/0.1"}
SIM_OK = 0.85


# ---------------------------------------------------------------- http helpers
# GUARD is None (write commands: unguarded) or a tuple of URL prefixes; set in main(). While set, http() allows
# only GET to those prefixes (redirects included) and anything else is fatal.
GUARD = None
LOCAL_ONLY = (HOST + "/",)
REMOTE_READ = LOCAL_ONLY + ("https://api.crossref.org/", "https://export.arxiv.org/", "https://api.openalex.org/")
READ_ONLY_CMDS = {"status", "libs", "colls", "ls", "find", "notes", "path", "plan", "where", "selftest", "allowlist", "log"}  # GUARD = LOCAL_ONLY
VERIFY_CMDS = {"verify", "resolve"}                                                       # GUARD = REMOTE_READ


def read_only_violation(url, data, method, allowed=LOCAL_ONLY):
    """None if (url, data, method) is a GET to one of the `allowed` URL prefixes, else an error string."""
    verb = method or ("POST" if data is not None else "GET")
    if verb != "GET" or not url.startswith(allowed):
        if allowed == LOCAL_ONLY:
            return "BUG: read-only command attempted %s %s" % (verb, url)
        return "BUG: verify attempted %s %s (only GET to Zotero, Crossref, arXiv, OpenAlex is allowed)" % (verb, url)
    return None


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-applies the guard to redirect targets, so an allowed host cannot bounce a request elsewhere."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        err = read_only_violation(newurl, None, "GET", GUARD or LOCAL_ONLY)
        if err:
            sys.exit(err)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def cmd_selftest(a):
    """No Zotero needed. Proves the guard that protects the read-only commands."""
    h = HOST
    cr, ax = "https://api.crossref.org/works/10.1/x", "https://export.arxiv.org/api/query?id_list=1"
    # (label, (url, data, method), must_block, allowed prefixes)
    cases = [("GET local api", (h + "/api/users/0/groups", None, None), False, LOCAL_ONLY),
             ("GET connector ping", (h + "/connector/ping", None, None), False, LOCAL_ONLY),
             ("POST (implicit, data given)", (h + "/api/groups/1/items", b"{}", None), True, LOCAL_ONLY),
             ("PATCH", (h + "/api/groups/1/items/ABC", None, "PATCH"), True, LOCAL_ONLY),
             ("DELETE", (h + "/api/groups/1/items/ABC", None, "DELETE"), True, LOCAL_ONLY),
             ("connector POST", (h + "/connector/saveItems", b"{}", "POST"), True, LOCAL_ONLY),
             ("GET off-host (Crossref)", (cr, None, None), True, LOCAL_ONLY),
             ("GET local userinfo trick", (h + "@evil.example/api/", None, None), True, LOCAL_ONLY),
             ("GET local lookalike port", (h + "0/api/", None, None), True, LOCAL_ONLY),
             ("verify: GET Crossref", (cr, None, None), False, REMOTE_READ),
             ("verify: GET arXiv", (ax, None, None), False, REMOTE_READ),
             ("verify: GET local ping", (h + "/connector/ping", None, None), False, REMOTE_READ),
             ("verify: POST Crossref", (cr, b"{}", "POST"), True, REMOTE_READ),
             ("verify: POST local connector", (h + "/connector/saveItems", b"{}", "POST"), True, REMOTE_READ),
             ("verify: GET other host", ("https://example.org/x", None, None), True, REMOTE_READ),
             ("verify: lookalike host", ("https://api.crossref.org.evil.example/x", None, None), True, REMOTE_READ),
             ("verify: userinfo trick", ("https://api.crossref.org@evil.example/x", None, None), True, REMOTE_READ),
             ("verify: plain http", ("http://api.crossref.org/works/1", None, None), True, REMOTE_READ)]
    bad = 0
    for label, args, must_block, allowed in cases:
        blocked = read_only_violation(*args, allowed=allowed) is not None
        bad += blocked != must_block
        print("%-30s %s %s" % (label, "BLOCKED" if blocked else "allowed", "ok" if blocked == must_block else "FAIL"))
    # a redirect from an allowed host to an off-host URL must be refused
    global GUARD
    saved, GUARD = GUARD, REMOTE_READ
    try:
        _GuardedRedirect().redirect_request(urllib.request.Request(cr), None, 302, "Found", {}, "https://example.org/x")
        print("%-30s allowed FAIL" % "verify: redirect off-host")
        bad += 1
    except SystemExit:
        print("%-30s BLOCKED ok" % "verify: redirect off-host")
    finally:
        GUARD = saved
    # ZOTERO_HOST must be loopback http (checked at startup by canonical_host)
    hosts = [("host: default", DEFAULT_HOST, False),
             ("host: localhost", "http://localhost:23119", False),
             ("host: ipv6 loopback", "http://[::1]:23119", False),
             ("host: remote", "http://example.org:23119", True),
             ("host: remote ip", "http://192.168.1.10:23119", True),
             ("host: lookalike", "http://127.0.0.1.evil.example:23119", True),
             ("host: lookalike localhost", "http://localhost.evil.example:23119", True),
             ("host: userinfo trick", "http://127.0.0.1:23119@evil.example", True),
             ("host: userinfo on loopback", "http://evil.example@127.0.0.1:23119", True),
             ("host: https", "https://127.0.0.1:23119", True),
             ("host: no port", "http://127.0.0.1", True),
             ("host: path", "http://127.0.0.1:23119/x", True),
             ("host: short form 127.1", "http://127.1:23119", True)]
    def check(label, blocked, must_block):
        nonlocal bad
        bad += blocked != must_block
        print("%-30s %s %s" % (label, "BLOCKED" if blocked else "allowed", "ok" if blocked == must_block else "FAIL"))
    for label, value, must_block in hosts:
        check(label, host_problem(value) is not None, must_block)

    # write allowlist: a missing or malformed file allows nothing
    with tempfile.TemporaryDirectory() as td:
        check("allowlist: no file", load_config(os.path.join(td, "config.json"))[1] is not None, True)
    for label, text, must_block in [
            ("allowlist: example", CONFIG_EXAMPLE, False),
            ("allowlist: not JSON", "{", True),
            ("allowlist: library root", '{"write_targets": [{"library": "My Library"}]}', True),
            ("allowlist: empty collection", '{"write_targets": [{"library": "My Library", "collection": " "}]}', True),
            ("allowlist: unknown key", '{"write_targets": [], "allow_all": true}', True),
            ("allowlist: subcolls not bool", '{"write_targets": [{"library": "L", "collection": "C", "subcollections": "yes"}]}', True)]:
        check(label, parse_config(text)[1] is not None, must_block)
    # objects against an allowlisted tree {SKT, SUB}; state: item -> (own collections, effective collections)
    allowed = {"SKT", "SUB"}
    state = {"IN": ({"SKT"}, {"SKT"}), "OUT": ({"OTHER"}, {"OTHER"}), "BOTH": ({"SKT", "OTHER"}, {"SKT", "OTHER"}),
             "NOTE": (set(), {"SKT"})}
    for label, kind, objs, parents_ok, must_block in [
            ("scope: tags on item in tree", "items", [{"key": "IN", "version": 1, "tags": []}], allowed, False),
            ("scope: edit child note in tree", "items", [{"key": "NOTE", "version": 1, "note": "x"}], allowed, False),
            ("scope: trash item outside", "items", [{"key": "OUT", "version": 1, "deleted": 1}], allowed, True),
            ("scope: trash item in tree", "items", [{"key": "IN", "version": 1, "deleted": 1}], allowed, False),
            ("scope: trash item also outside", "items", [{"key": "BOTH", "version": 1, "deleted": 1}], allowed, True),
            ("scope: trash note, parent in", "items", [{"key": "NOTE", "version": 1, "deleted": 1}], allowed, False),
            ("scope: tags on item also out", "items", [{"key": "BOTH", "version": 1, "tags": []}], allowed, False),
            ("scope: unknown item key", "items", [{"key": "NOPE", "version": 1, "deleted": 1}], allowed, True),
            ("scope: move within tree", "items", [{"key": "IN", "version": 1, "collections": ["SUB"]}], allowed, False),
            ("scope: keep outside membership", "items", [{"key": "BOTH", "version": 1, "collections": ["OTHER", "SUB"]}], allowed, False),
            ("scope: add outside collection", "items", [{"key": "IN", "version": 1, "collections": ["SKT", "OTHER"]}], allowed, True),
            ("scope: drop outside membership", "items", [{"key": "BOTH", "version": 1, "collections": ["SKT"]}], allowed, True),
            ("scope: new note, parent in", "items", [{"itemType": "note", "parentItem": "IN", "note": "x"}], allowed, False),
            ("scope: new note, parent out", "items", [{"itemType": "note", "parentItem": "OUT", "note": "x"}], allowed, True),
            ("scope: new item outside", "items", [{"itemType": "book", "collections": ["OTHER"]}], allowed, True),
            ("scope: new item, no collection", "items", [{"itemType": "book"}], allowed, True),
            ("scope: one bad in batch", "items", [{"key": "IN", "version": 1, "tags": []}, {"key": "OUT", "version": 1, "deleted": 1}], allowed, True),
            ("scope: new subcollection", "collections", [{"name": "Block 1", "parentCollection": "SKT"}], allowed, False),
            ("scope: subcoll, not permitted", "collections", [{"name": "Block 1", "parentCollection": "SKT"}], set(), True),
            ("scope: subcoll outside", "collections", [{"name": "X", "parentCollection": "OTHER"}], allowed, True),
            ("scope: rename collection", "collections", [{"key": "SKT", "version": 1, "name": "X"}], allowed, True)]:
        check(label, bool(scope_violations(kind, objs, allowed, parents_ok, state)), must_block)
    # backstop in http(): a non-GET request must be covered by an allowlist check made earlier in the run
    saved_libs, saved_scope = set(AUTHORIZED_LIBS), dict(CONNECTOR_SCOPE)
    sess = lambda s, **kw: json.dumps(dict(sessionID=s, **kw)).encode()
    try:
        AUTHORIZED_LIBS.clear(); CONNECTOR_SCOPE.clear()
        cases = [("backstop: unchecked local write", (h + "/api/users/0/items", b"[]", "POST", None), True),
                 ("backstop: unchecked authorize", (h + "/api/local/authorize", b"{}", "POST", None), True),
                 ("backstop: unchecked saveItems", (h + "/connector/saveItems", sess("S"), "POST", None), True),
                 ("backstop: connector read", (h + "/connector/getSelectedCollection", b"{}", "POST", None), False),
                 ("backstop: POST off loopback", ("https://example.org/x", b"{}", "POST", None), True)]
        for label, args, must_block in cases:
            check(label, write_violation(*args) is not None, must_block)
        AUTHORIZED_LIBS.add("/users/0"); CONNECTOR_SCOPE.update(session="S", target="C1")
        cases = [("backstop: checked library", (h + "/api/users/0/items", b"[]", "POST", None), False),
                 ("backstop: other library", (h + "/api/groups/5/items", b"[]", "POST", None), True),
                 ("backstop: saveItems, session", (h + "/connector/saveItems", sess("S"), "POST", None), False),
                 ("backstop: saveItems, other sess", (h + "/connector/saveItems", sess("X"), "POST", None), True),
                 ("backstop: updateSession target", (h + "/connector/updateSession", sess("S", target="C1"), "POST", None), False),
                 ("backstop: updateSession other", (h + "/connector/updateSession", sess("S", target="C9"), "POST", None), True),
                 ("backstop: saveAttachment", (h + "/connector/saveAttachment", b"%PDF-", "POST", {"X-Metadata": sess("S").decode()}), False),
                 ("backstop: other connector call", (h + "/connector/saveSnapshot", sess("S"), "POST", None), True)]
        for label, args, must_block in cases:
            check(label, write_violation(*args) is not None, must_block)
    finally:
        AUTHORIZED_LIBS.clear(); AUTHORIZED_LIBS.update(saved_libs)
        CONNECTOR_SCOPE.clear(); CONNECTOR_SCOPE.update(saved_scope)

    # plan hash: canonical, and any change to what import would send changes it
    def fake(tag="x", url="https://arxiv.org/pdf/1", sub="Block 1"):
        return ({"library": "My Library", "collection": "Skill-Test"},
                {"coll_key": "SKT", "kind": 0, "n_entries": 1,
                 "items": [{"id": "i1", "itemType": "preprint", "title": "T", "tags": [{"tag": tag, "type": 0}], "notes": []}],
                 "entries": [{"pdf_urls": [url], "subcollection": sub}]})
    base_opts = {"allow_unverified": False, "pdf": True, "allow_spec_pdf": False, "max_pdf_mb": 50}
    def expect(label, cond):
        nonlocal bad
        bad += not cond
        print("%-30s %s" % (label, "ok" if cond else "FAIL"))
    ref = payload_hash(import_payload(*fake(), base_opts))
    # the allowlist file is part of the hash: a plan approved under one allowlist does not run under another
    with tempfile.TemporaryDirectory() as td:
        p_a = os.path.join(td, "config.json")
        with open(p_a, "w", encoding="utf-8") as f:
            f.write(CONFIG_EXAMPLE)
        fp1 = allowlist_fingerprint(p_a)
        with open(p_a, "w", encoding="utf-8") as f:
            f.write(CONFIG_EXAMPLE.replace("Skill-Test", "JoS SDG7 Perspective"))
        fp2 = allowlist_fingerprint(p_a)
        fp0 = allowlist_fingerprint(os.path.join(td, "missing.json"))
    expect("allowlist hash: file read", fp1["sha256"] and len(fp1["sha256"]) == 64)
    expect("allowlist hash: missing file", fp0["sha256"] is None)
    expect("allowlist hash: edit -> differs", fp1["sha256"] != fp2["sha256"])
    expect("plan hash: other allowlist", payload_hash(import_payload(*fake(), base_opts, fp1))
           != payload_hash(import_payload(*fake(), base_opts, fp2)))
    expect("organize hash: other allowlist", payload_hash(organize_payload(fake()[0], {"parent": "P", "create": [], "objs": []}, fp1))
           != payload_hash(organize_payload(fake()[0], {"parent": "P", "create": [], "objs": []}, fp2)))
    expect("hash: key order irrelevant", payload_hash({"a": 1, "b": [1, 2]}) == payload_hash({"b": [1, 2], "a": 1}))
    expect("hash: list order counts", payload_hash([1, 2]) != payload_hash([2, 1]))
    for label, payload in [("hash: one tag changed", import_payload(*fake(tag="y"), base_opts)),
                           ("hash: PDF URL changed", import_payload(*fake(url="https://arxiv.org/pdf/2"), base_opts)),
                           ("hash: subcollection changed", import_payload(*fake(sub="Block 2"), base_opts)),
                           ("hash: PDFs off", import_payload(*fake(), dict(base_opts, pdf=False))),
                           ("hash: --allow-spec-pdf", import_payload(*fake(), dict(base_opts, allow_spec_pdf=True))),
                           ("hash: --max-pdf-mb", import_payload(*fake(), dict(base_opts, max_pdf_mb=10))),
                           ("hash: --allow-unverified", import_payload(*fake(), dict(base_opts, allow_unverified=True)))]:
        expect(label + " -> differs", payload_hash(payload) != ref)
    for label, given, must_block in [("confirm: matching hash", ref, False), ("confirm: upper case", ref.upper(), False),
                                     ("confirm: missing", None, True), ("confirm: other hash", "0" * 64, True)]:
        try:
            confirm_or_exit("import", given, ref, "plan SPEC")
            blocked = False
        except SystemExit as ex:
            blocked = True
            if ref in str(ex.code):
                print("confirm: refusal leaks the expected hash FAIL")
                bad += 1
        check(label, blocked, must_block)

    # PDFs: https to public hosts only (redirects too), real PDFs within the cap, spec URLs only when allowed
    for label, url, must_block in [("pdf: https arXiv", "https://arxiv.org/pdf/2506.08872", False),
                                   ("pdf: plain http", "http://arxiv.org/pdf/1", True),
                                   ("pdf: userinfo trick", "https://arxiv.org@evil.example/x.pdf", True),
                                   ("pdf: loopback", "https://127.0.0.1/x.pdf", True),
                                   ("pdf: localhost", "https://localhost/x.pdf", True),
                                   ("pdf: private address", "https://10.0.0.5/x.pdf", True),
                                   ("pdf: link-local metadata", "https://169.254.169.254/x", True),
                                   ("pdf: file URL", "file:///etc/passwd", True)]:
        check(label, not pdf_url_ok(url), must_block)
    try:
        _HttpsOnlyRedirect().redirect_request(urllib.request.Request("https://arxiv.org/pdf/1"), None, 302, "Found", {},
                                              "http://example.org/x.pdf")
        check("pdf: redirect to http", False, True)
    except urllib.error.URLError:
        check("pdf: redirect to http", True, True)
    check("pdf: real PDF within cap", pdf_body_problem(b"%PDF-1.7 ...", 100) is not None, False)
    check("pdf: HTML error page", pdf_body_problem(b"<html>403</html>", 100) is not None, True)
    check("pdf: over the cap", pdf_body_problem(b"%PDF-" + b"0" * 100, 100) is not None, True)
    items = [{"id": "i1"}]
    entries = [{"pdf": "https://example.org/spec.pdf",  # pdf_urls as an older verify wrote them (spec URL mixed in)
                "pdf_urls": ["https://example.org/spec.pdf", "https://arxiv.org/pdf/1", "http://insecure.example/x.pdf"]}]
    expect("pdf plan: off by default", pdf_plan(items, entries, dict(base_opts, pdf=False)) == {})
    expect("pdf plan: --pdf skips spec URL", pdf_plan(items, entries, base_opts) == {"i1": ["https://arxiv.org/pdf/1"]})
    expect("pdf plan: --allow-spec-pdf", pdf_plan(items, entries, dict(base_opts, allow_spec_pdf=True))
           == {"i1": ["https://example.org/spec.pdf", "https://arxiv.org/pdf/1"]})

    # attach: only files inside an attach_dirs entry, after resolving .. and symlinks
    with tempfile.TemporaryDirectory() as td:
        ok_dir, other = os.path.join(td, "pdfs"), os.path.join(td, "other")
        os.makedirs(ok_dir); os.makedirs(other)
        for p in (os.path.join(ok_dir, "a.pdf"), os.path.join(other, "b.pdf")):
            open(p, "wb").close()
        dirs = [ok_dir]
        check("attach: file in attach dir", attach_file_problem(os.path.join(ok_dir, "a.pdf"), dirs) is not None, False)
        check("attach: file elsewhere", attach_file_problem(os.path.join(other, "b.pdf"), dirs) is not None, True)
        check("attach: .. out of attach dir", attach_file_problem(os.path.join(ok_dir, "..", "other", "b.pdf"), dirs) is not None, True)
        check("attach: the dir itself", attach_file_problem(ok_dir, dirs) is not None, True)
        check("attach: prefix lookalike dir", attach_file_problem(os.path.join(td, "pdfs-evil", "c.pdf"), dirs) is not None, True)
        check("attach: no attach_dirs", attach_file_problem(os.path.join(ok_dir, "a.pdf"), []) is not None, True)
        try:
            os.symlink(os.path.join(other, "b.pdf"), os.path.join(ok_dir, "link.pdf"))
            check("attach: symlink out of dir", attach_file_problem(os.path.join(ok_dir, "link.pdf"), dirs) is not None, True)
        except (OSError, NotImplementedError):
            print("%-30s skipped (cannot create symlinks here)" % "attach: symlink out of dir")

    # write log: one line per request, metadata and a payload hash only
    with tempfile.TemporaryDirectory() as td:
        lp_ = os.path.join(td, "writes.jsonl")
        payload = json.dumps({"sessionID": "S", "items": [{"title": "A Confidential Title", "notes": [{"note": "private note"}]}]}).encode()
        log_write(h + "/connector/saveItems", payload, "POST", 201, path=lp_)
        log_write(h + "/api/local/uploads/SECRETUPLOADKEY", b"%PDF-1.7", "POST", 204, path=lp_)
        with open(lp_, encoding="utf-8") as f:
            text = f.read()
        recs = [json.loads(line) for line in text.splitlines()]
    expect("log: one line per request", len(recs) == 2)
    expect("log: no item content", "Confidential" not in text and "private note" not in text)
    expect("log: payload sha256", recs[0]["sha256"] == hashlib.sha256(payload).hexdigest())
    expect("log: object count", recs[0]["objects"] == 1 and recs[1]["objects"] == 1)
    expect("log: upload key masked", "SECRETUPLOADKEY" not in text)
    expect("log: fields", set(recs[0]) == {"ts", "command", "library", "collection", "method", "endpoint", "objects", "sha256", "status"})
    expect("log: count list payload", count_objects("/api/users/0/items", b'[{"key": "A"}, {"key": "B"}]') == 2)

    # preflight: the row selected in the Zotero window (where saveItems lands first) must be an allowlisted collection
    sel_targets = [{"id": "L1", "name": "My Library", "level": 0}, {"id": "C7", "name": "Skill-Test", "level": 1},
                   {"id": "C8", "name": "Block 2", "level": 2}, {"id": "C9", "name": "Other", "level": 1},
                   {"id": "L2", "name": "A Group", "level": 0}, {"id": "C20", "name": "Skill-Test", "level": 1}]
    ids = library_target_ids({"targets": sel_targets}, "My Library", {"skill-test", "block 2"})
    expect("preflight: ids of allowed colls", ids == {"C7", "C8"})
    def sel(**kw):  # shape of a real Zotero 10.0.8 response: numeric top-level id, "C<n>" ids in targets
        return dict({"libraryID": 1, "libraryName": "Meine Bibliothek", "editable": True, "id": 7, "name": "Skill-Test",
                     "targets": sel_targets}, **kw)
    for label, resp, must_block in [("preflight: allowed selected", sel(), False),
                                    ("preflight: id as tree id", sel(id="C7"), False),
                                    ("preflight: subcoll selected", sel(id=8, name="Block 2"), False),
                                    ("preflight: other coll selected", sel(id=9, name="Other"), True),
                                    ("preflight: same name, group", sel(id=20, libraryID=2), True),
                                    ("preflight: library id L1", sel(id="L1"), True),
                                    ("preflight: id true (bool)", sel(id=True), True),
                                    ("preflight: library root", sel(id=None, name="My Library"), True),
                                    ("preflight: not editable", sel(editable=False), True),
                                    ("preflight: no response", None, True),
                                    ("preflight: malformed", {"targets": []}, True)]:
        check(label, selected_target_problem(resp, ids) is not None, must_block)

    # UTF-8: umlauts survive a round trip, with or without a BOM; a legacy code page is refused, not mangled
    with tempfile.TemporaryDirectory() as td:
        spec_u = {"collection": "JoS SDG7", "entries": [{"claimed": {"title": "Fünf Kategorien: Fröste, Flüsse",
                                                                     "authors": "Bjerregård, Sørensen, Möhrlen"}}]}
        p_plain, p_bom, p_1252 = (os.path.join(td, n) for n in ("plain.json", "bom.json", "cp1252.json"))
        write_json(p_plain, spec_u)
        with open(p_bom, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps(spec_u, ensure_ascii=False).encode("utf-8"))
        with open(p_1252, "wb") as f:
            f.write(json.dumps(spec_u, ensure_ascii=False).encode("cp1252"))
        with open(p_plain, "rb") as f:
            raw = f.read()
        expect("utf-8: written as UTF-8", "Bjerregård".encode("utf-8") in raw and not raw.startswith(b"\xef\xbb\xbf"))
        expect("utf-8: umlauts round trip", read_json(p_plain) == spec_u)
        expect("utf-8: BOM tolerated", read_json(p_bom) == spec_u)
        try:
            read_json(p_1252)
            expect("utf-8: cp1252 file refused", False)
        except SystemExit:
            expect("utf-8: cp1252 file refused", True)
        p_cfg = os.path.join(td, "config.json")
        with open(p_cfg, "wb") as f:
            f.write(b"\xef\xbb\xbf" + CONFIG_EXAMPLE.encode("utf-8"))
        if os.name == "posix":
            os.chmod(p_cfg, 0o600)
        expect("utf-8: allowlist with BOM", load_config(p_cfg)[1] is None)

    # organize keeps tags it does not manage; single-value screening prefixes are replaced, not duplicated
    cur = [{"tag": "D1.3-Ch8", "type": 0}, {"tag": "Physics - Atmospheric and Oceanic Physics", "type": 1},
           {"tag": "needs review", "type": 1}, {"tag": "decision:offen", "type": 0}, {"tag": "prio:sollte", "type": 0}]
    got = sorted((t["tag"], t["type"]) for t in merged_tags(cur, tag_objs(["needs review", "decision:einschliessen", "prio:muss"], 0)))
    expect("tags: foreign tags kept", ("D1.3-Ch8", 0) in got and ("Physics - Atmospheric and Oceanic Physics", 1) in got)
    expect("tags: spec tag made regular", ("needs review", 0) in got and ("needs review", 1) not in got)
    expect("tags: decision replaced", ("decision:einschliessen", 0) in got and ("decision:offen", 0) not in got)
    expect("tags: prio replaced", ("prio:muss", 0) in got and ("prio:sollte", 0) not in got)
    expect("tags: nothing else", len(got) == 5)
    expect("tags: no prefix tag in spec", ("decision:offen", 0) in
           [(t["tag"], t["type"]) for t in merged_tags(cur, tag_objs(["JoS SDG7"], 0))])

    # resolve: input parsing, parsing canned Crossref/arXiv responses, ranking; the guard covers its URLs
    pending = {"needs_id": [{"claimed": {"title": "Energy droughts from variable renewable energy sources in European climates",
                                         "authors": "Raynaud et al.", "year": 2018}, "venue": "Renewable Energy"},
                            {"claimed": {}}, "not an entry"],
               "entries": [{"id": "10.1/x", "claimed": {"title": "ignored"}}]}
    expect("resolve: needs_id read", [e["claimed"]["authors"] for e in resolve_inputs(pending)] == ["Raynaud et al."])
    expect("resolve: spec entries w/o id", [e["claimed"]["title"] for e in resolve_inputs(
        {"entries": [{"id": "10.1/x", "claimed": {"title": "A"}}, {"claimed": {"title": "B"}}]})] == ["B"])
    expect("resolve: surnames", [first_surname(x) for x in ("Wilczak et al.", "Harris, C. R.", "van der Most et al.", "Murphy", None)]
           == ["Wilczak", "Harris", "van der Most", "Murphy", ""])
    cr_body = json.dumps({"message": {"items": [
        {"DOI": "10.1016/j.renene.2018.02.130", "title": ["Energy droughts from variable renewable energy sources in European climates"],
         "author": [{"family": "Raynaud"}], "issued": {"date-parts": [[2018, 10]]}, "container-title": ["Renewable Energy"], "type": "journal-article"},
        {"DOI": "10.9999/other", "title": ["Something else entirely"], "author": [{"family": "Nobody"}],
         "issued": {"date-parts": [[2001]]}, "container-title": ["Elsewhere"]}]}}).encode()
    ax_body = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2501.01234v2</id>'
               b'<title>Energy droughts from variable renewable energy sources in European climates</title>'
               b'<published>2025-01-03T00:00:00Z</published><author><name>A. Person</name></author></entry></feed>')
    cands = crossref_candidates(cr_body) + arxiv_candidates(ax_body)
    expect("resolve: parse Crossref", cands[0]["id"] == "10.1016/j.renene.2018.02.130" and cands[0]["year"] == "2018")
    expect("resolve: parse arXiv", cands[2]["id"] == "arXiv:2501.01234" and cands[2]["first_author"] == "Person")
    top = rank_candidates(pending["needs_id"][0]["claimed"], cands)
    expect("resolve: best is the match", top[0]["id"] == "10.1016/j.renene.2018.02.130" and top[0]["author_match"] is True)
    expect("resolve: same title, other author", top[1]["id"] == "arXiv:2501.01234" and top[1]["author_match"] is False)
    q = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": "Fünf Flüsse Möhrlen 2022", "rows": 3})
    for label, url, must_block in [("resolve: GET Crossref query", q, False),
                                   ("resolve: GET arXiv query", "https://export.arxiv.org/api/query?search_query=ti%3A%22x%22", False),
                                   ("resolve: hint_url not fetched", "https://www.sciencedirect.com/science/article/pii/S0960148122014707", True),
                                   ("resolve: PMC not fetched", "https://pmc.ncbi.nlm.nih.gov/articles/PMC13127558/", True)]:
        check(label, read_only_violation(url, None, None, REMOTE_READ) is not None, must_block)
    expect("resolve: runs under verify guard", "resolve" in VERIFY_CMDS)

    # note: only child notes, only these fields, at most one dialog's worth
    good = {"parent": "ABCD2345", "note": "<h1>Title</h1><p>x</p>", "tags": ["needs review"]}
    for label, entries, must_block in [("note: valid", [good], False),
                                       ("note: empty list", [], True),
                                       ("note: over 50", [good] * 51, True),
                                       ("note: extra field", [dict(good, collections=["X"])], True),
                                       ("note: bad parent key", [dict(good, parent="../items")], True),
                                       ("note: empty text", [dict(good, note="<p> </p>")], True),
                                       ("note: tags not a list", [dict(good, tags="x")], True)]:
        check(label, note_entries_problem(entries) is not None, must_block)
    print("read-only commands:", ", ".join(sorted(READ_ONLY_CMDS)))
    print("network-read commands:", ", ".join(sorted(VERIFY_CMDS)))
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- files: UTF-8 always, a BOM tolerated on input
def read_text(path):
    """Text of a file as UTF-8; a byte-order mark (Notepad, PowerShell's Set-Content -Encoding utf8) is dropped.
    Never the platform default encoding, which on Windows is a legacy code page that mangles umlauts."""
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def read_json(path, what="file"):
    try:
        return json.loads(read_text(path))
    except UnicodeDecodeError as ex:
        sys.exit("%s %s is not UTF-8 (%s); save it as UTF-8" % (what, path, ex))
    except ValueError as ex:
        sys.exit("%s %s is not valid JSON (%s)" % (what, path, ex))
    except OSError as ex:
        sys.exit("cannot read %s %s (%s)" % (what, path, ex.strerror or ex))


def write_json(path, obj):
    """UTF-8, no BOM, non-ASCII kept readable."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- write log (metadata only, never item content)
LOG_CONTEXT = {"command": None, "library": None, "collection": None}  # filled by main() and the write paths


def log_path():
    return os.path.join(user_dir("state"), "writes.jsonl")


def ensure_log(path=None):
    """Make sure the write log can be appended to; called before a write is sent, so no write goes unlogged."""
    path = path or log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a", encoding="utf-8").close()
    except OSError as ex:
        sys.exit("write refused: cannot append to the write log %s (%s)" % (path, ex))


def count_objects(endpoint, data):
    """How many objects a write request carries: list length, connector items, 1 for a file, 0 otherwise."""
    if endpoint.endswith(("/saveAttachment", "/file")) or "/api/local/uploads/" in endpoint:
        return 1
    try:
        body = json.loads(data or b"null")
    except (ValueError, UnicodeDecodeError):
        return 0
    if isinstance(body, list):
        return len(body)
    return len(body.get("items") or []) if isinstance(body, dict) else 0


def log_write(url, data, method, status, path=None):
    """Append one JSON line for one write request: when, which command, where, how many objects, the SHA-256 of
    the payload and the HTTP status (or the exception name). Titles, notes and file contents are never written."""
    endpoint = re.sub(r"(/api/local/uploads/)[^/?]+", r"\1*", urllib.parse.urlsplit(url).path)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "command": LOG_CONTEXT["command"],
           "library": LOG_CONTEXT["library"], "collection": LOG_CONTEXT["collection"],
           "method": method, "endpoint": endpoint, "objects": count_objects(endpoint, data),
           "sha256": hashlib.sha256(data or b"").hexdigest(), "status": status}
    with open(path or log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def cmd_log(a):
    """Read-only: the last N entries of the write log."""
    p = log_path()
    print("write log:", p)
    if not os.path.isfile(p):
        print("(no writes logged yet)")
        return
    lines = read_text(p).splitlines()[-a.n:] if a.n > 0 else []
    for line in lines:
        try:
            r = json.loads(line)
            print("%s  %-9s %-6s %-40s %3s obj  %-5s  %s / %s  sha256 %s" % (
                r.get("ts"), r.get("command"), r.get("method"), r.get("endpoint"), r.get("objects"), r.get("status"),
                r.get("library") or "-", r.get("collection") or "-", (r.get("sha256") or "")[:16]))
        except ValueError:
            print("(unreadable line) " + line.strip()[:80])


def http(url, data=None, headers=None, method=None, timeout=60, raw=False):
    if GUARD:
        err = read_only_violation(url, data, method, GUARD)
        if err:
            sys.exit(err)
    err = write_violation(url, data, method, headers)
    if err:
        sys.exit("write refused: " + err)
    verb = method or ("POST" if data is not None else "GET")
    logged = verb != "GET" and urllib.parse.urlsplit(url).path not in ["/connector/" + e for e in CONNECTOR_READS]
    if logged:
        ensure_log()
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    for attempt in range(4):
        try:
            with (urllib.request.build_opener(_GuardedRedirect).open if GUARD else urllib.request.urlopen)(req, timeout=timeout) as r:
                body = r.read()
                if logged:
                    log_write(url, data, verb, r.status)
                return (body, r.headers) if raw else body
        except urllib.error.HTTPError as e:
            if logged:
                log_write(url, data, verb, e.code)
            if e.code == 429 and attempt < 3:
                time.sleep(4 * (attempt + 1))
                continue
            raise
        except Exception as ex:
            if logged:
                log_write(url, data, verb, type(ex).__name__)
            raise


def zget(path, **params):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    try:
        body, hdr = http(HOST + "/api" + path + q, headers={"Zotero-API-Version": "3"}, raw=True)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            sys.exit("Zotero local API is disabled: Settings > Advanced > 'Allow other applications on this computer to communicate with Zotero'")
        raise
    except urllib.error.URLError:
        sys.exit("Zotero is not running (nothing on %s)" % HOST)
    return json.loads(body), hdr


def connector(endpoint, payload=None, headers=None, data=None):
    h = {"Content-Type": "application/json", "X-Zotero-Connector-API-Version": "3"}
    h.update(headers or {})
    if data is None:
        data = json.dumps(payload if payload is not None else {}).encode()
    if endpoint == "saveItems":
        preflight_selected()  # saveItems lands in the window's selection first; see selected_target_problem
    body = http(HOST + "/connector/" + endpoint, data=data, headers=h, method="POST")
    return json.loads(body) if body.strip()[:1] in (b"{", b"[") else body


# ---------------------------------------------------------------- libraries / collections
def groups():
    out, _ = zget("/users/0/groups", limit=100)
    return {g["data"]["name"]: g["id"] for g in out}


def lib_path_or_none(name):
    if name.lower() in ("my library", "user", "me", "0"):
        return "/users/0"
    g = {n.lower(): i for n, i in groups().items()}
    return "/groups/%s" % g[name.lower()] if name.lower() in g else None


def lib_path(name):
    lp = lib_path_or_none(name)
    if lp is None:
        sys.exit("No library %r. Available: My Library, %s" % (name, ", ".join(sorted(groups()))))
    return lp


def all_collections(lp):
    out, start = [], 0
    while True:
        page, hdr = zget(lp + "/collections", limit=100, start=start)
        out += page
        start += len(page)
        if not page or start >= int(hdr.get("Total-Results", start)):
            return out


def collection_key(lp, name):
    m = [c for c in all_collections(lp) if c["data"]["name"].lower() == name.lower()]
    if len(m) != 1:
        sys.exit("Collection %r: %d matches (collection names must be unique in the library)" % (name, len(m)))
    return m[0]["key"]


def connector_target(library, collection):
    """Map names -> connector target ids ('L21', 'C288') using the connector's tree listing."""
    sel = connector("getSelectedCollection")
    lib_id, in_lib, matches = None, False, []
    for t in sel["targets"]:
        if t["id"].startswith("L"):
            in_lib = t["name"].lower() == library.lower() or (library.lower() in ("my library", "user") and t["id"] == "L1")
            if in_lib:
                lib_id = t["id"]
                if not collection:
                    return lib_id, t["name"]
        elif in_lib and collection and t["name"].lower() == collection.lower():
            matches.append(t)
    if len(matches) == 1:
        return matches[0]["id"], matches[0]["name"]
    sys.exit("Collection %r in library %r: %d matches (names must be unique)" % (collection, library, len(matches)))


def library_target_ids(sel, library, names):
    """Connector target ids ('C288') of the collections of LIBRARY whose (lower-case) names are in NAMES, read from
    a getSelectedCollection response. Library rows ('L1', level 0) start each library's block of collections."""
    ids, in_lib = set(), False
    for t in sel.get("targets") or []:
        tid = str(t.get("id", ""))
        if tid.startswith("L"):
            in_lib = str(t.get("name", "")).lower() == library.lower() or (library.lower() in ("my library", "user") and tid == "L1")
        elif in_lib and str(t.get("name", "")).lower() in names:
            ids.add(tid)
    return ids


def selected_target_problem(sel, allowed_ids):
    """None if the row selected in the Zotero window, as getSelectedCollection reports it, is an editable collection
    whose target id is in ALLOWED_IDS; else why not. saveItems saves into exactly that row (server_connector.js:
    getSaveTarget) before updateSession moves the items, so anything else could leave items outside the allowlist.
    Fails closed: an unexpected response is a refusal."""
    if not isinstance(sel, dict) or "id" not in sel or "editable" not in sel:
        return "could not determine the collection selected in the Zotero window (unexpected connector response)"
    if sel.get("editable") is not True:
        return "the row selected in the Zotero window (%r) is not editable" % sel.get("name")
    if not sel.get("id"):
        return "the library root (%r) is selected in the Zotero window, not a collection" % sel.get("libraryName")
    # the top-level id is the numeric collection.id (3); the rows in `targets` use the tree-view id ("C3")
    sid = sel["id"]
    sid = "C%d" % sid if isinstance(sid, int) and not isinstance(sid, bool) else str(sid)
    if not re.fullmatch(r"C\d+", sid):
        return "could not determine the collection selected in the Zotero window (unexpected id %r)" % (sel["id"],)
    if sid not in allowed_ids:
        return "the collection selected in the Zotero window (%r) is not on the allowlist" % sel.get("name")
    return None


def preflight_selected():
    """Re-read the Zotero window's selection right before a connector save and refuse unless it is allowlisted.
    switchToReadableLibrary=true makes Zotero resolve the target the way saveItems does (a read-only selection
    falls back to My Library there)."""
    try:
        sel = connector("getSelectedCollection", {"switchToReadableLibrary": True})
    except (urllib.error.URLError, ValueError) as ex:
        sel = None
        print("getSelectedCollection failed: %s" % ex, file=sys.stderr)
    why = selected_target_problem(sel, CONNECTOR_SCOPE.get("allowed_ids", set()))
    if why:
        sys.exit("write refused: %s.\nSelect the target collection %r in the Zotero window, then run the command again."
                 % (why, CONNECTOR_SCOPE.get("target_name")))


# ---------------------------------------------------------------- identifier resolution
def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", html.unescape(s or "").lower()).strip()


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\n", " ").strip()


def clean(s):
    return re.sub(r"\s+", " ", strip_tags(s)).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a.startswith(b) or b.startswith(a):  # truncated titles
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 2)


def creators(people, kind="author"):
    out = []
    for p in people or []:
        if p.get("family"):
            out.append({"creatorType": kind, "firstName": p.get("given", ""), "lastName": p["family"]})
        elif p.get("name"):
            out.append({"creatorType": kind, "lastName": p["name"], "fieldMode": 1})
    return out


def date_of(parts):
    parts = (parts or [[]])[0]
    return "-".join("%02d" % p if i else str(p) for i, p in enumerate(parts) if p)


def crossref(doi):
    try:
        m = json.loads(http("https://api.crossref.org/works/" + urllib.parse.quote(doi)))["message"]
    except urllib.error.HTTPError as e:
        return None, "Crossref HTTP %d" % e.code
    title = clean((m.get("title") or [""])[0])
    sub = clean((m.get("subtitle") or [""])[0])
    if sub and norm(sub) not in norm(title):
        title = f"{title}: {sub}"
    t = m.get("type", "")
    cont = clean((m.get("container-title") or [""])[0])
    it = {"title": title, "creators": creators(m.get("author")), "date": date_of((m.get("issued") or {}).get("date-parts")),
          "DOI": m.get("DOI", doi), "abstractNote": clean(m.get("abstract", "")), "libraryCatalog": "Crossref"}
    if t == "journal-article":
        it.update(itemType="journalArticle", publicationTitle=cont, volume=m.get("volume", ""), issue=m.get("issue", ""),
                  pages=m.get("page", ""), ISSN=(m.get("ISSN") or [""])[0])
    elif t == "proceedings-article":
        it.update(itemType="conferencePaper", proceedingsTitle=cont, pages=m.get("page", ""), publisher=m.get("publisher", ""),
                  conferenceName=(m.get("event") or {}).get("name", ""))
    elif t == "book-chapter":
        it.update(itemType="bookSection", bookTitle=cont, pages=m.get("page", ""), publisher=m.get("publisher", ""),
                  ISBN=(m.get("ISBN") or [""])[0])
        it["creators"] += creators(m.get("editor"), "editor")
    elif t == "posted-content":
        it.update(itemType="preprint", repository=m.get("institution", [{}])[0].get("name", m.get("publisher", "")))
    else:
        it.update(itemType="document", publisher=m.get("publisher", ""))
    return {k: v for k, v in it.items() if v not in ("", None, [])}, None


def arxiv(aid):
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    try:
        root = ET.fromstring(http("https://export.arxiv.org/api/query?id_list=" + aid))
    except urllib.error.HTTPError as e:
        return None, "arXiv HTTP %d" % e.code
    e = root.find("a:entry", ns)
    if e is None or e.find("a:title", ns) is None or "Error" in (e.find("a:title", ns).text or ""):
        return None, "arXiv: not found"
    cat = e.find("x:primary_category", ns)
    it = {"itemType": "preprint", "title": clean(e.find("a:title", ns).text),
          "creators": [{"creatorType": "author", **split_name(a.find("a:name", ns).text)} for a in e.findall("a:author", ns)],
          "abstractNote": clean(e.find("a:summary", ns).text), "date": e.find("a:published", ns).text[:10],
          "repository": "arXiv", "archiveID": "arXiv:" + aid, "url": "https://arxiv.org/abs/" + aid,
          "DOI": "10.48550/arXiv." + aid, "libraryCatalog": "arXiv.org",
          "extra": "arXiv:%s [%s]" % (aid, cat.get("term") if cat is not None else "")}
    pub = e.find("x:doi", ns)
    if pub is not None:
        it["extra"] += "\nPublished version DOI: " + pub.text
    return it, None


def split_name(n):
    parts = n.strip().split()
    return {"firstName": " ".join(parts[:-1]), "lastName": parts[-1]} if len(parts) > 1 else {"lastName": n, "fieldMode": 1}


def pdf_candidates(entry_id, doi):
    if entry_id.lower().startswith("arxiv:"):
        return ["https://arxiv.org/pdf/" + entry_id.split(":", 1)[1]]
    if not doi:
        return []
    try:
        w = json.loads(http("https://api.openalex.org/works/https://doi.org/%s?select=best_oa_location,locations" % urllib.parse.quote(doi)))
    except Exception as ex:
        print("warning: OpenAlex lookup failed for %s (%s); no PDF candidates from it" % (doi, ex), file=sys.stderr)
        return []
    urls = []
    for loc in [w.get("best_oa_location") or {}] + (w.get("locations") or []):
        u = loc.get("pdf_url")
        if u and loc.get("is_oa", True) and u not in urls:
            urls.append(u)
    return urls[:3]


def title_matches(claimed, item):
    c = claimed or {}
    notes = []
    s = sim(c.get("title", ""), item["title"]) if c.get("title") else None
    ok = s is None or s >= SIM_OK
    a = c.get("authors") or ""
    if a and not a.lower().startswith("authors unknown"):
        first = norm(a.split(",")[0])
        lasts = [norm(x.get("lastName", "")) for x in item.get("creators", [])]
        if first and not any(first in l or l in first for l in lasts if l):
            ok = False
            notes.append("first author %r not among resolved authors" % a.split(",")[0])
    y = c.get("year")
    ry = (item.get("date") or "")[:4]
    if y and ry.isdigit() and abs(int(y) - int(ry)) > 1:
        notes.append("year %s vs resolved %s" % (y, ry))
    return ok, s, notes


# ---------------------------------------------------------------- commands
def cmd_status(a):
    print("Zotero %s  connector: up" % ZOTERO_VERSION)
    print("local API: up; %d group libraries" % len(groups()))


def cmd_libs(a):
    print("My Library\t/users/0")
    for n, i in sorted(groups().items()):
        print("%s\t/groups/%s" % (n, i))


def cmd_colls(a):
    cs = all_collections(lib_path(a.lib))
    by = {c["key"]: c for c in cs}
    def depth(c):
        d = 0
        while c["data"].get("parentCollection"):
            c = by[c["data"]["parentCollection"]]; d += 1
        return d
    for c in sorted(cs, key=lambda c: c["data"]["name"].lower()):
        print("%s%s  [%s]  %s items" % ("  " * depth(c), c["data"]["name"], c["key"], c["meta"].get("numItems", "?")))


def cmd_ls(a):
    lp = lib_path(a.lib)
    if a.coll:
        lp += "/collections/" + collection_key(lp, a.coll)
    items, _ = zget(lp + "/items/top", limit=100, include="data,meta")
    for i in items:
        d = i["data"]
        print("%s  %-15s %s | %-70s | children=%s" % (i["key"], d.get("itemType"), (d.get("date") or "")[:4],
              (d.get("title") or d.get("note", "")[:60]).replace("\n", " ")[:70], i["meta"].get("numChildren", 0)))
    print("-- %d top-level items" % len(items))


def cmd_find(a):
    items, _ = zget(lib_path(a.lib) + "/items", q=a.query, qmode="everything", limit=25, itemType="-attachment")
    for i in items:
        d = i["data"]
        print("%s  %s | %s | %s" % (i["key"], d.get("itemType"), d.get("title", "")[:70], d.get("DOI", "")))


def cmd_notes(a):
    """Child notes of each ITEM (key, dates, tags, text); an ITEM that is itself a note is shown as is."""
    lp = lib_path(a.lib)
    n = 0
    for k in a.items:
        try:
            item, _ = zget(lp + "/items/" + k, include="data")
            d = item["data"]
            notes = [item] if d.get("itemType") == "note" else zget(lp + "/items/%s/children" % k, itemType="note", include="data")[0]
        except urllib.error.HTTPError as e:
            sys.exit("No item %s in %s (HTTP %d)" % (k, a.lib, e.code))
        if d.get("itemType") != "note":
            print("%s  %s" % (k, (d.get("title") or "")[:70]))
        for i in notes:
            d = i["data"]
            text = clean(d.get("note", ""))
            n += 1
            print("  note %s | parent %s | added %s | modified %s | tags: %s%s" % (
                i["key"], d.get("parentItem", "-"), d.get("dateAdded", "")[:10], d.get("dateModified", "")[:10],
                ", ".join(t["tag"] for t in d.get("tags", [])) or "-", " | in trash" if d.get("deleted") else ""))
            print("    " + (text if a.full else text[:200] + ("..." if len(text) > 200 else "")))
    print("-- %d notes" % n)


def cmd_path(a):
    """Local file path of each attachment of the ITEMs (an ITEM that is itself an attachment counts as one), or of
    every attachment in collection --coll (not recursive, like `ls`).

    Uses /file/view/url (200, URL in the body); /file and /file/view answer 302 to file://, which the guard refuses.
    Zotero returns that URL even when nothing is on disk, so existence is checked here."""
    if bool(a.items) == bool(a.coll):
        sys.exit("usage: path LIB ITEM [ITEM...]  |  path LIB --coll COLL")
    lp = lib_path(a.lib)
    atts = []
    if a.coll:
        start = 0
        while True:  # one list covers child attachments and standalone ones
            page, hdr = zget(lp + "/collections/%s/items" % collection_key(lp, a.coll), itemType="attachment", limit=100, start=start, include="data")
            atts += page
            start += len(page)
            if not page or start >= int(hdr.get("Total-Results", start)):
                break
    for k in a.items:
        try:
            item, _ = zget(lp + "/items/" + k, include="data")
            atts += [item] if item["data"].get("itemType") == "attachment" else \
                zget(lp + "/items/%s/children" % k, itemType="attachment", include="data")[0]
        except urllib.error.HTTPError as e:
            sys.exit("No item %s in %s (HTTP %d)" % (k, a.lib, e.code))
    found = 0
    for i in atts:
        d = i["data"]
        row = [d.get("parentItem", "-"), i["key"], d.get("contentType", "")]
        try:
            url = http(HOST + "/api" + lp + "/items/%s/file/view/url" % i["key"], headers={"Zotero-API-Version": "3"}).decode().strip()
        except urllib.error.HTTPError:  # 400 "Not a file attachment": a web link
            print("\t".join(["nofile"] + row + [d.get("url", "")]))
            continue
        path = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
        ok = os.path.isfile(path)
        found += ok
        print("\t".join(["ok" if ok else "missing"] + row + [path]))
    print("-- %d attachments, %d on disk" % (len(atts), found))


def note_present(html_note, existing_texts, threshold=0.8):
    """True if a (possibly human-edited) top-level note resembling this one already exists."""
    mine = norm(clean(html_note)[:600])
    return any(difflib.SequenceMatcher(None, mine, t).ratio() >= threshold for t in existing_texts)


def library_index(lp):
    """(identifiers, normalized titles, note texts) of top-level, non-trashed items in the library."""
    ids, titles, notes, start = set(), set(), [], 0
    while True:
        page, hdr = zget(lp + "/items/top", limit=100, start=start, include="data")
        for i in page:
            d = i["data"]
            titles.add(norm(d.get("title", "")))
            if d.get("itemType") == "note":
                notes.append(norm(clean(d.get("note", ""))[:600]))
            for v in (d.get("DOI"), d.get("archiveID"), d.get("url")):
                if v:
                    ids.add(v.lower().replace("https://doi.org/", "").replace("https://arxiv.org/abs/", "arxiv:"))
        start += len(page)
        if not page or start >= int(hdr.get("Total-Results", start)):
            return ids, titles, notes


def library_tags(lp):
    names, start = set(), 0
    while True:
        page, hdr = zget(lp + "/tags", limit=100, start=start)
        names |= {t["tag"] for t in page}
        start += len(page)
        if not page or start >= int(hdr.get("Total-Results", start)):
            return names


# ---------------------------------------------------------------- write allowlist (user-owned file, outside the repo)
# Every write is checked twice: local_write() and connector_scope() compare the target with the allowlist, and
# http() refuses any non-GET request that no such check covered (write_violation). No file, no writes.
APP_DIR = "zotero-skill"
CONFIG_KEYS = {"write_targets", "attach_dirs", "_comment"}
CONFIG_EXAMPLE = json.dumps({"write_targets": [{"library": "My Library", "collection": "Skill-Test", "subcollections": True}],
                             "attach_dirs": [os.path.join(os.path.expanduser("~"), "Documents", "zotero-attach")]})
AUTHORIZED_LIBS = set()   # library paths ("/users/0") whose write targets local_write() checked in this run
CONNECTOR_SCOPE = {}      # {"session", "target"} once connector_scope() checked the target in this run
CONNECTOR_READS = ("getSelectedCollection", "ping")  # connector POSTs that read only


def user_dir(kind):
    """Per-user directory under the home directory. kind 'config' holds the allowlist, 'state' the write log.
    Deliberately not read from APPDATA / XDG_* variables, so a command-line prefix cannot swap in another file."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        sub = ("AppData", "Roaming") if kind == "config" else ("AppData", "Local")
    elif sys.platform == "darwin":
        sub = ("Library", "Application Support") if kind == "config" else ("Library", "Logs")
    else:
        sub = (".config",) if kind == "config" else (".local", "state")
    return os.path.join(home, *sub, APP_DIR)


def config_path():
    return os.path.join(user_dir("config"), "config.json")


def parse_config(text):
    """(config, None) or (None, reason). Anything unexpected is an error: the caller then allows nothing."""
    try:
        c = json.loads(text)
    except ValueError as ex:
        return None, "not valid JSON (%s)" % ex
    if not isinstance(c, dict) or set(c) - CONFIG_KEYS:
        return None, "the top level must be an object with only these keys: %s" % ", ".join(sorted(CONFIG_KEYS))
    targets = c.get("write_targets", [])
    if not isinstance(targets, list):
        return None, "write_targets must be a list"
    for t in targets:
        if (not isinstance(t, dict) or set(t) - {"library", "collection", "subcollections"}
                or not all(isinstance(t.get(k), str) and t[k].strip() for k in ("library", "collection"))
                or not isinstance(t.get("subcollections", False), bool)):
            return None, ("each write_targets entry needs non-empty strings library and collection, and optionally "
                          "subcollections: true/false; got %s" % json.dumps(t))
    dirs = c.get("attach_dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(d, str) and os.path.isabs(os.path.expanduser(d)) for d in dirs):
        return None, "attach_dirs must be a list of absolute directory paths"
    c["write_targets"], c["attach_dirs"] = targets, dirs
    return c, None


def attach_file_problem(path, dirs):
    """None if `path`, with symlinks resolved, is a file strictly inside one of `dirs` (also resolved), else why not.
    Resolving first means neither `..` nor a symlink placed inside an allowed directory can reach outside it."""
    real = os.path.realpath(os.path.expanduser(path))
    for d in dirs:
        rd = os.path.realpath(os.path.expanduser(d))
        try:
            inside = os.path.commonpath([os.path.normcase(real), os.path.normcase(rd)]) == os.path.normcase(rd)
        except ValueError:  # different drives
            inside = False
        if inside and os.path.normcase(real) != os.path.normcase(rd):
            return None
    return "%s (resolved: %s) is not inside any attach_dirs entry of %s" % (path, real, config_path())


def load_config(path=None):
    """(allowlist, None), or (None, reason) when there is none; every write path then refuses."""
    path = path or config_path()
    try:
        st = os.stat(path)
        text = read_text(path)
    except UnicodeDecodeError as ex:
        return None, "%s is not UTF-8 (%s)" % (path, ex)
    except FileNotFoundError:
        return None, "no allowlist file at %s" % path
    except OSError as ex:
        return None, "cannot read %s (%s)" % (path, ex)
    if os.name == "posix" and (st.st_mode & 0o022 or st.st_uid != os.getuid()):
        return None, "%s must be owned by you and not writable by group or others" % path
    return parse_config(text)


def need_config():
    c, err = load_config()
    if err:
        sys.exit("write refused: %s.\nOnly the user (never the agent) creates or edits that file. Example content:\n  %s"
                 % (err, CONFIG_EXAMPLE))
    return c


def allowed_keys(lib):
    """(lib path, allowed, parents_ok) for library LIB: the collection keys writes may touch, and the keys under
    which new subcollections may be created (only targets with "subcollections": true). Exits if LIB has none."""
    c = need_config()
    lp = lib_path(lib)
    targets = [t for t in c["write_targets"] if lib_path_or_none(t["library"]) == lp]
    colls = all_collections(lp) if targets else []
    by_name, children = {}, {}
    for co in colls:
        by_name.setdefault(co["data"]["name"].lower(), []).append(co["key"])
        children.setdefault(co["data"].get("parentCollection") or None, []).append(co["key"])
    allowed, parents_ok = set(), set()
    for t in targets:
        keys = by_name.get(t["collection"].lower(), [])
        if len(keys) != 1:
            print("warning: allowlisted collection %r in %r: %d matches, ignored" % (t["collection"], t["library"], len(keys)),
                  file=sys.stderr)
            continue
        tree, stack = {keys[0]}, [keys[0]]
        while t.get("subcollections") and stack:
            for ch in children.get(stack.pop(), []):
                if ch not in tree:
                    tree.add(ch)
                    stack.append(ch)
        allowed |= tree
        if t.get("subcollections"):
            parents_ok |= tree
    if not allowed:
        sys.exit("write refused: no collection of library %r is in the allowlist %s" % (lib, config_path()))
    return lp, allowed, parents_ok


def item_state(lp, keys):
    """{key: (own collections, effective collections)}; a child item's effective collections are its parent's.
    Keys that do not resolve are left out, which scope_violations() treats as outside the allowlist."""
    out = {}
    for k in sorted(keys):
        try:
            d = zget(lp + "/items/" + k, include="data")[0]["data"]
            own = set(d.get("collections", []))
            eff = set(zget(lp + "/items/" + d["parentItem"], include="data")[0]["data"].get("collections", [])) \
                if d.get("parentItem") else own
        except urllib.error.HTTPError:
            continue
        out[k] = (own, eff)
    return out


def scope_violations(kind, objs, allowed, parents_ok, state, names=None):
    """Why the local-API objects would reach outside the allowlist (empty list: they do not).
    Existing items must be in an allowed collection (children: via their parent); collection memberships may only
    be added or removed inside the allowed set; new items need an allowed parent or allowed collections only;
    new collections need a parent in parents_ok; existing collections are never changed; an item is only trashed
    when every collection it is in is allowlisted. NAMES maps collection keys to names for the messages."""
    errs, names = [], names or {}
    for n, o in enumerate(objs):
        what = "object %d%s" % (n, " (%s)" % o["key"] if o.get("key") else "")
        if kind == "collections":
            if o.get("key"):
                errs.append(what + ": changing an existing collection is not allowed")
            elif o.get("parentCollection") not in parents_ok:
                errs.append(what + ": new collection %r would be outside the allowlisted tree" % o.get("name"))
            continue
        if kind != "items":
            errs.append(what + ": unsupported object kind %r" % kind)
            continue
        if o.get("key"):
            own, eff = state.get(o["key"], (set(), set()))
            if not eff & allowed:
                errs.append(what + ": the item is not in an allowlisted collection")
            elif o.get("deleted") and eff - allowed:
                # trashing removes the item from every collection, so all of them must be allowlisted
                errs.append(what + ": trash refused, the item is also in collections outside the allowlist: %s"
                            % ", ".join(repr(names.get(k, k)) for k in sorted(eff - allowed)))
            if "collections" in o and (set(o["collections"]) ^ own) - allowed:
                errs.append(what + ": would add or remove collections outside the allowlist: %s"
                            % sorted((set(o["collections"]) ^ own) - allowed))
        elif not o.get("parentItem") and (not o.get("collections") or set(o["collections"]) - allowed):
            errs.append(what + ": a new top-level item must go into allowlisted collections only")
        if o.get("parentItem") and not state.get(o["parentItem"], (set(), set()))[1] & allowed:
            errs.append(what + ": parent item %s is not in an allowlisted collection" % o["parentItem"])
    return errs


def write_violation(url, data, method, headers=None):
    """Backstop in http() under every write path: None if this request is a GET or is covered by an allowlist
    check made earlier in this run (local_write / connector_scope), else the reason it is refused."""
    verb = method or ("POST" if data is not None else "GET")
    if verb == "GET":
        return None
    if not url.startswith(HOST + "/"):
        return "%s %s refused: writes only go to Zotero on loopback" % (verb, url)
    path = urllib.parse.urlsplit(url).path
    if path.startswith("/connector/"):
        ep = path[len("/connector/"):]
        if ep in CONNECTOR_READS:
            return None
        if ep not in ("saveItems", "updateSession", "saveAttachment"):
            return "connector %s refused: not an endpoint this skill writes through" % ep
        if not CONNECTOR_SCOPE:
            return "connector %s refused: no allowlisted target was checked first" % ep
        try:
            meta = json.loads((headers or {}).get("X-Metadata") or data or b"{}")
        except ValueError:
            meta = None
        if not isinstance(meta, dict) or meta.get("sessionID") != CONNECTOR_SCOPE["session"]:
            return "connector %s refused: not the session opened for the allowlisted target" % ep
        if ep == "updateSession" and meta.get("target") != CONNECTOR_SCOPE["target"]:
            return "connector updateSession refused: target %r is not the allowlisted %r" % (meta.get("target"), CONNECTOR_SCOPE["target"])
        return None
    if path == "/api/local/authorize" or path.startswith("/api/local/uploads/"):
        return None if AUTHORIZED_LIBS else "%s %s refused: no allowlist check was made first" % (verb, path)
    m = re.match(r"/api(/users/0|/groups/\d+)/", path)
    if not m or m.group(1) not in AUTHORIZED_LIBS:
        return "%s %s refused: that library was not checked against the allowlist" % (verb, path)
    return None


def connector_scope(library, collection):
    """Check an import/attach target against the allowlist, then map it to the connector's target id and open the
    one session the connector writes of this run are bound to. Returns (target id, target name, session id)."""
    if not collection:
        sys.exit("write refused: the library root is never a write target; name an allowlisted collection")
    lp, allowed, _ = allowed_keys(library)
    if collection_key(lp, collection) not in allowed:
        sys.exit("write refused: %s / %s is not in the allowlist %s" % (library, collection, config_path()))
    target, tname = connector_target(library, collection)
    names = {c["data"]["name"].lower() for c in all_collections(lp) if c["key"] in allowed}
    allowed_ids = library_target_ids(connector("getSelectedCollection"), library, names)
    CONNECTOR_SCOPE.update(session="zskill-%d" % int(time.time()), target=target, target_name=tname,
                           allowed_ids=allowed_ids)
    LOG_CONTEXT.update(library=library, collection=collection)
    preflight_selected()  # refuse to start at all if the selection is wrong; checked again right before saveItems
    return target, tname, CONNECTOR_SCOPE["session"]


def cmd_allowlist(a):
    """Read-only: print the allowlist file, what it permits and, if Zotero is running, the collection keys."""
    print("allowlist file:", config_path())
    c, err = load_config()
    if err:
        print("NO WRITES ALLOWED: %s\nexample content (the user creates the file):\n  %s" % (err, CONFIG_EXAMPLE))
        return
    if not c["write_targets"]:
        print("write_targets: none, so no writes are allowed")
    for t in c["write_targets"]:
        print("write target: %s / %s  %s" % (t["library"], t["collection"],
              "(and its subcollections)" if t.get("subcollections") else "(this collection only)"))
    if not c["attach_dirs"]:
        print("attach_dirs: none, so `attach` accepts no file")
    for d in c["attach_dirs"]:
        rd = os.path.realpath(os.path.expanduser(d))
        print("attach dir:   %s%s%s" % (d, "" if rd == d else "  (resolved: %s)" % rd, "" if os.path.isdir(rd) else "  [missing]"))
    try:
        require_zotero10()
    except SystemExit:
        print("(Zotero is not running: collection keys not resolved)")
        return
    for t in c["write_targets"]:
        lp = lib_path_or_none(t["library"])
        keys = [co["key"] for co in all_collections(lp) if co["data"]["name"].lower() == t["collection"].lower()] if lp else []
        print("  %s / %s -> %s" % (t["library"], t["collection"],
              "no such library" if not lp else keys[0] if len(keys) == 1 else "%d matches: ignored" % len(keys)))


def local_authorize():
    """Ask the user to approve ONE write request. Returns the headers for it (the key is single-use)."""
    _, hdr = http(HOST + "/api/", headers={"Zotero-API-Version": "3"}, raw=True)
    sid = hdr["Zotero-Server-ID"]
    print("Zotero will show an approval dialog - click Allow in the Zotero window (waiting up to 3 min)...", file=sys.stderr)
    body = http(HOST + "/api/local/authorize", data=json.dumps({"appName": "zotero skill"}).encode(), method="POST", timeout=180,
                headers={"Zotero-API-Version": "3", "Zotero-Server-ID": sid, "Content-Type": "application/json"})
    grant = json.loads(body)
    if grant.get("remember"):
        print("WARNING: this approval was 'Always Allow'. Zotero now keeps a write key that never expires, in plain text "
              "in localAPIKeys.json in the Zotero profile directory, and accepts it without a dialog from anything that "
              "reads it. This skill does not reuse it. To revoke it: quit Zotero and remove the entries with "
              '"remember": true from that file. Next time click Allow.', file=sys.stderr)
    return {"Zotero-API-Version": "3", "Zotero-Server-ID": sid, "Zotero-API-Key": grant["key"]}


def local_write(lib, items, kind="items"):
    """Batch-write objects ([{key, version, ...fields}]) to /items (or /collections) via the local API.

    Zotero asks the user to approve every write request (the key from /api/local/authorize is
    single-use), so always send ONE request holding as many objects as possible (max 50).
    Refuses (before any dialog) unless every object stays inside the allowlist: scope_violations()."""
    lp, allowed, parents_ok = allowed_keys(lib)
    refs = {o[f] for o in items for f in ("key", "parentItem") if o.get(f)} if kind == "items" else set()
    names = {c["key"]: c["data"]["name"] for c in all_collections(lp)}
    errs = scope_violations(kind, items, allowed, parents_ok, item_state(lp, refs), names)
    if errs:
        sys.exit("write refused by the allowlist %s:\n  %s" % (config_path(), "\n  ".join(errs)))
    AUTHORIZED_LIBS.add(lp)
    LOG_CONTEXT["library"] = lib
    h = local_authorize()
    h["Content-Type"] = "application/json"
    out = http(HOST + "/api" + lp + "/" + kind, data=json.dumps(items).encode(), method="POST", headers=h)
    return json.loads(out)


def read_local_file(path, max_mb):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        sys.exit("No such file: %s" % path)
    if os.path.getsize(path) > max_mb * 1e6:
        sys.exit("%s is %.0f MB, over --max-mb %d" % (path, os.path.getsize(path) / 1e6, max_mb))
    with open(path, "rb") as f:
        body = f.read()
    ctype = "application/pdf" if body[:5] == b"%PDF-" else mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.lower().endswith(".pdf") and ctype != "application/pdf":
        sys.exit("%s is named .pdf but is not a PDF (no %%PDF- header)" % path)
    return path, body, ctype


def attach_existing(lib, key, path, body, ctype, title):
    """Store a copy of the file as a new child attachment of item KEY, using the Web API's file-upload protocol that
    Zotero 10 serves locally. Each of the three write requests needs its own approval click:
    (1) create the attachment item, (2) authorize the upload, (3) register it. The bytes go to a staging URL
    that needs no approval."""
    lp = lib_path(lib)
    try:
        parent, _ = zget(lp + "/items/" + key, include="data")
    except urllib.error.HTTPError as e:
        sys.exit("No item %s in %s (HTTP %d)" % (key, lib, e.code))
    pd = parent["data"]
    if pd.get("itemType") in ("attachment", "note"):
        sys.exit("%s is a%s %s; give the parent item's key" % (key, "n" if pd["itemType"] == "attachment" else "", pd["itemType"]))
    if pd.get("deleted"):
        sys.exit("%s is in the trash" % key)
    name = os.path.basename(path)
    md5 = hashlib.md5(body).hexdigest()
    mtime = int(os.path.getmtime(path) * 1000)

    print("1/3 create attachment item under %s (%s)" % (key, (pd.get("title") or "")[:60]), flush=True)
    r = local_write(lib, [{"itemType": "attachment", "linkMode": "imported_file", "parentItem": key, "title": title,
                           "contentType": ctype, "filename": name, "tags": [], "relations": {}}])
    if not r.get("success"):
        sys.exit("could not create the attachment item: %s" % json.dumps(r.get("failed")))
    akey = r["success"]["0"]

    def finish(msg):
        sys.exit("%s\nThe empty attachment item %s remains under %s: re-run, or `trash %s %s`." % (msg, akey, key, lib, akey))

    print("2/3 authorize upload of %s (%.0f KB)" % (name, len(body) / 1024), flush=True)
    h = local_authorize()
    h.update({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
    form = urllib.parse.urlencode({"md5": md5, "filename": name, "filesize": len(body), "mtime": mtime}).encode()
    try:
        auth = json.loads(http(HOST + "/api%s/items/%s/file" % (lp, akey), data=form, method="POST", headers=h))
    except urllib.error.HTTPError as e:
        finish("upload authorization failed (HTTP %d: %s)" % (e.code, e.read().decode(errors="replace")[:200]))
    if auth.get("exists"):
        finish("Zotero reports this file already exists for the new item (unexpected)")
    # prefix + bytes + suffix is the Web API's S3 body convention; locally both are empty
    try:
        http(auth["url"], data=auth.get("prefix", "").encode() + body + auth.get("suffix", "").encode(), method="POST",
             headers={"Content-Type": auth.get("contentType") or ctype}, timeout=180)
    except urllib.error.HTTPError as e:
        finish("upload failed (HTTP %d: %s)" % (e.code, e.read().decode(errors="replace")[:200]))

    print("3/3 register upload", flush=True)
    h = local_authorize()
    h.update({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
    try:
        http(HOST + "/api%s/items/%s/file" % (lp, akey), data=urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode(),
             method="POST", headers=h)
    except urllib.error.HTTPError as e:
        finish("registering the upload failed (HTTP %d: %s)" % (e.code, e.read().decode(errors="replace")[:200]))
    return akey


def attach_new(lib, coll, ident, path, body, ctype, title):
    """Create a top-level item from a DOI/arXiv ID (metadata from Crossref/arXiv, never typed) and attach the local
    file to it, through the connector in one session: no approval dialog."""
    lp = lib_path(lib)
    item, err = arxiv(ident.split(":", 1)[1]) if ident.lower().startswith("arxiv:") else crossref(ident)
    if item is None:
        sys.exit("%s did not resolve: %s" % (ident, err))
    ids, _, _ = library_index(lp)
    for v in (item.get("DOI", "").lower(), ident.lower()):
        if v and v in ids:
            sys.exit("%s is already in %s (`find %s \"%s\"` for its key); use --item KEY to attach the file to it" % (ident, lib, lib, v))
    target, tname, session = connector_scope(lib, coll)
    item = dict(item, id="i1", tags=[], notes=[], attachments=[])
    print("new item: %s | %s" % ((item.get("title") or "")[:70], "; ".join(c.get("lastName", "") for c in item.get("creators", [])[:3])))
    connector("saveItems", {"sessionID": session, "uri": "http://localhost/zotero-skill", "items": [item]})
    connector("updateSession", {"sessionID": session, "target": target, "tags": ""})
    # a bare file name as `url`: Zotero uses it to name the file, and a local path must not end up in the (synced) record
    meta = {"id": "ai1", "url": os.path.basename(path), "contentType": ctype, "parentItemID": "i1",
            "title": title, "sessionID": session}
    connector("saveAttachment", headers={"Content-Type": ctype, "X-Metadata": json.dumps(meta)}, data=body)
    return tname


def cmd_attach(a):
    """Attach a local file (PDF, or e.g. docx) to an existing item, or to a new item created from an identifier."""
    if bool(a.item) == bool(a.new):
        sys.exit("usage: attach LIB FILE --item KEY  |  attach LIB FILE --new DOI_OR_ARXIV_ID [--coll COLL]")
    if a.coll and not a.new:
        sys.exit("--coll only applies to --new (an existing item keeps its collections)")
    LOG_CONTEXT["collection"] = a.coll
    why = attach_file_problem(a.file, need_config()["attach_dirs"])
    if why:
        sys.exit("attach refused: " + why)
    path, body, ctype = read_local_file(os.path.realpath(os.path.expanduser(a.file)), a.max_mb)
    title = a.title or ("Full Text PDF" if ctype == "application/pdf" else os.path.basename(path))
    if a.item:
        k = attach_existing(a.lib, a.item, path, body, ctype, title)
        print("attached %s (%.0f KB) to %s as attachment %s" % (os.path.basename(path), len(body) / 1024, a.item, k))
    else:
        t = attach_new(a.lib, a.coll, a.new, path, body, ctype, title)
        print("created %s and attached %s (%.0f KB) -> %s" % (a.new, os.path.basename(path), len(body) / 1024, t))


NOTE_KEYS = {"parent", "note", "tags"}


def note_entries_problem(entries):
    """None if `entries` is a valid `note` file body: a list of 1-50 {parent, note[, tags]}, else why not."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        return "the file must hold a JSON list of 1 to 50 notes (one approval dialog per run)"
    for n, e in enumerate(entries):
        if not isinstance(e, dict) or set(e) - NOTE_KEYS:
            return "note %d: only the keys %s are allowed" % (n, ", ".join(sorted(NOTE_KEYS)))
        if not isinstance(e.get("parent"), str) or not re.fullmatch(r"[A-Z0-9]{8}", e["parent"]):
            return "note %d: parent must be an 8-character Zotero item key" % n
        if not isinstance(e.get("note"), str) or not clean(e["note"]):
            return "note %d: note must be non-empty text or HTML" % n
        if not isinstance(e.get("tags", []), list) or not all(isinstance(t, str) for t in e.get("tags", [])):
            return "note %d: tags must be a list of strings" % n
    return None


def note_objects(lib, entries):
    """Plan for `note` (GET only): (child-note objects to create, [(parent, reason)] skipped). Parents must be
    regular, non-trashed items; a note resembling one the parent already has (note_present) is skipped."""
    lp = lib_path(lib)
    objs, skipped, seen = [], [], {}
    for e in entries:
        k = e["parent"]
        try:
            d = zget(lp + "/items/" + k, include="data")[0]["data"]
        except urllib.error.HTTPError as ex:
            sys.exit("No item %s in %s (HTTP %d)" % (k, lib, ex.code))
        if d.get("itemType") in ("note", "attachment"):
            sys.exit("%s is a%s %s; notes go on the parent item" % (k, "n" if d["itemType"] == "attachment" else "", d["itemType"]))
        if d.get("deleted"):
            sys.exit("%s is in the trash" % k)
        if k not in seen:
            kids = zget(lp + "/items/%s/children" % k, itemType="note", include="data")[0]
            seen[k] = [norm(clean(c["data"].get("note", ""))[:600]) for c in kids]
        body = note_html(e["note"])
        if note_present(body, seen[k]):
            skipped.append((k, "a similar note is already there"))
            continue
        seen[k].append(norm(clean(body)[:600]))
        objs.append({"itemType": "note", "parentItem": k, "note": body, "tags": tag_objs(e.get("tags", []), 0)})
    return objs, skipped


def cmd_note(a):
    """Add child notes to items already in the library: one local-API request, one approval dialog, bound to
    the hash printed by --dry-run and to the allowlist (every parent must be in an allowlisted collection)."""
    entries = read_json(a.file, "note file")
    err = note_entries_problem(entries)
    if err:
        sys.exit("note refused: " + err)
    objs, skipped = note_objects(a.lib, entries)
    for o in objs:
        print("  add to %s: %s" % (o["parentItem"], clean(o["note"])[:70]))
    for k, why in skipped:
        print("  skip %s: %s" % (k, why))
    if not objs:
        print("nothing to add")
        return
    h = payload_hash({"library": a.lib, "objects": objs, "allowlist": allowlist_fingerprint()})
    if a.dry_run:
        print_target(a.lib, "(parents' collections; each must be allowlisted)")
        print("note hash (note LIB FILE --confirm): %s" % h)
        return
    confirm_or_exit("note", a.confirm, h, "note LIB FILE --dry-run")
    r = local_write(a.lib, objs)
    made = r.get("successful") or {}
    print("added %d notes, failed %d" % (len(made), len(r.get("failed") or {})))
    for i, v in sorted(made.items(), key=lambda kv: int(kv[0])):
        print("  note %s on %s" % (v.get("key"), objs[int(i)]["parentItem"]))
    for k, v in (r.get("failed") or {}).items():
        print("  failed %s: %s" % (k, v))


def cmd_trash(a):
    lp = lib_path(a.lib)
    objs = []
    for k in a.keys:
        it, _ = zget(lp + "/items/" + k)
        objs.append({"key": k, "version": it["version"], "deleted": 1})
    r = local_write(a.lib, objs)
    print("trashed:", sorted(r.get("successful", {}) and [v["key"] for v in r["successful"].values()]), "failed:", r.get("failed"))


def run_dir(create=False):
    """The directory for verify output: <system temp dir>/zotero-skill. The temp dir honours $TMPDIR.
    The directory is private (mode 0700, owned by us, never a symlink), so the fixed name is safe even in a
    shared /tmp. Outputs for different specs are told apart by the spec path (see resolved_for)."""
    d = os.path.join(tempfile.gettempdir(), "zotero-skill")
    if create:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            sys.exit("refusing to use %s: not a directory owned by you" % d)
        os.chmod(d, 0o700)
    return d


def resolved_for(spec_arg, create=False):
    """The single definition of where a spec's verify output lives:
    <run_dir()>/<spec file stem>-<8 hex of the spec's real path>.resolved.json.
    It depends on the spec path alone, so verify, plan, import, organize and where agree without a second
    file name, and two different specs can never share an output file. create=True makes the directory."""
    spec_path = os.path.realpath(spec_arg)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(os.path.basename(spec_path))[0])[:60] or "spec"
    return os.path.join(run_dir(create), "%s-%s.resolved.json" % (stem, hashlib.sha1(spec_path.encode()).hexdigest()[:8]))


def load_resolved(spec_arg):
    p = resolved_for(spec_arg)
    if not os.path.isfile(p):
        sys.exit("no verify output for %s (expected %s); run `verify %s` first" % (spec_arg, p, spec_arg))
    return read_json(p, "verify output")


IMPORTABLE = ("ok", "accepted", "unverified")  # verify statuses that import / organize act on


def importable_entries(spec):
    return [e for e in spec["entries"] if e["status"] in IMPORTABLE]


def cmd_where(a):
    d = run_dir()
    if a.spec:
        p = resolved_for(a.spec)
        print(p)
        sys.exit(0 if os.path.isfile(p) else 1)
    print(d)
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        st = os.stat(os.path.join(d, n))
        print("  %-50s %8d bytes  %s" % (n, st.st_size, time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))))


def cmd_verify(a):
    spec = read_json(a.spec, "spec")
    out_path = resolved_for(a.spec, create=True)
    res = {"library": spec["library"], "collection": spec.get("collection"), "tags": spec.get("tags", []),
           "tag_type": spec.get("tag_type", "manual"),
           "standalone_notes": spec.get("standalone_notes", []), "entries": []}
    for e in spec["entries"]:
        r = dict(e)
        ident = e.get("id") or "(manual) " + (e.get("manual") or {}).get("title", "?")[:24]
        item, err = None, None
        if e.get("manual"):
            # Escape hatch: metadata NOT resolved from Crossref/arXiv. Only with the user's explicit, quoted approval.
            m = e["manual"]
            if not e.get("user_affirmed"):
                r.update(status="refused", detail="manual metadata needs user_affirmed: the user's explicit approval, quoted")
            elif not (m.get("itemType") and m.get("title")):
                r.update(status="refused", detail="manual metadata needs itemType and title")
            else:
                r.update(status="unverified", item=dict(m), detail="UNVERIFIED; user affirmed: %s" % e["user_affirmed"][:80])
        elif not e.get("id"):
            r.update(status="refused", detail="entry has neither id nor manual metadata")
        else:
            try:
                item, err = arxiv(ident.split(":", 1)[1]) if ident.lower().startswith("arxiv:") else crossref(ident)
            except urllib.error.URLError as ex:
                item, err = None, str(ex)
            if item is None:
                r.update(status="notfound", detail=err)
        if not r.get("status"):
            ok, s, notes = title_matches(e.get("claimed"), item)
            r.update(item=item, title_sim=s, detail="; ".join(notes))
            r["status"] = "ok" if ok else ("accepted" if e.get("accept_mismatch") else "mismatch")
            if not a.no_pdf and r["status"] in ("ok", "accepted"):
                # open-access candidates only; the spec's own free-form `pdf` URL stays in the entry as given and is
                # used by import only with --pdf --allow-spec-pdf (pdf_plan)
                r["pdf_urls"] = pdf_candidates(ident, item.get("DOI"))
        res["entries"].append(r)
        flag = {"ok": "  ok  ", "accepted": "accept", "mismatch": "MISMATCH", "notfound": "NOTFOUND",
                "unverified": "UNVERIF", "refused": "REFUSED"}[r["status"]]
        print("%-8s %-34s sim=%-4s pdf=%-3s %s | %s" % (flag, ident, r.get("title_sim"), len(r.get("pdf_urls", [])),
              (r.get("item") or {}).get("title", "")[:48], r.get("detail", "")))
    write_json(out_path, res)
    n = {k: sum(1 for x in res["entries"] if x["status"] == k) for k in ("ok", "accepted", "unverified", "mismatch", "notfound", "refused")}
    print("\n%s\nwrote %s" % (n, out_path))


# ---------------------------------------------------------------- resolve: find candidate IDs for claimed references
def resolve_inputs(doc):
    """Entries to resolve: `needs_id` of a pending file, else the `entries` of a spec that have no `id`, else a bare
    list. Each needs claimed.title. Pure; selftest calls it."""
    if isinstance(doc, dict):
        items = doc.get("needs_id") if isinstance(doc.get("needs_id"), list) else \
            [e for e in doc.get("entries") or [] if isinstance(e, dict) and not e.get("id")]
    else:
        items = doc if isinstance(doc, list) else []
    return [e for e in items if isinstance(e, dict) and isinstance(e.get("claimed"), dict) and e["claimed"].get("title")]


def first_surname(authors):
    """'Wilczak et al.' -> 'Wilczak', 'Harris, C. R.' -> 'Harris', 'van der Most et al.' -> 'van der Most'."""
    return re.split(r",|;|\bet al\b|\band\b|&", str(authors or ""))[0].strip().rstrip(".")


def crossref_candidates(body):
    """Candidates from a Crossref /works?query.bibliographic response (bytes)."""
    out = []
    for m in (json.loads(body).get("message") or {}).get("items") or []:
        au = (m.get("author") or [{}])[0]
        out.append({"source": "crossref", "id": m.get("DOI", ""), "title": clean((m.get("title") or [""])[0]),
                    "year": str(date_of((m.get("issued") or {}).get("date-parts")))[:4],
                    "first_author": au.get("family") or au.get("name") or "",
                    "container": clean((m.get("container-title") or [""])[0]), "type": m.get("type", "")})
    return out


def arxiv_candidates(body):
    """Candidates from an arXiv API Atom feed (bytes)."""
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(body).findall("a:entry", ns):
        aid = re.sub(r"v\d+$", "", (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1])
        if not aid:
            continue
        name = (e.find("a:author/a:name", ns).text or "") if e.find("a:author/a:name", ns) is not None else ""
        out.append({"source": "arxiv", "id": "arXiv:" + aid, "title": clean(e.findtext("a:title", "", ns)),
                    "year": (e.findtext("a:published", "", ns) or "")[:4], "first_author": split_name(name)["lastName"] if name else "",
                    "container": "arXiv", "type": "preprint"})
    return out


def rank_candidates(claimed, cands, n=3):
    """Top N by title similarity, each with the similarity and whether first author and year agree."""
    sur = norm(first_surname(claimed.get("authors")))
    year = str(claimed.get("year") or "")
    for c in cands:
        c["sim"] = sim(claimed.get("title", ""), c["title"])
        fa = norm(c["first_author"])
        c["author_match"] = None if not sur else bool(fa) and (sur in fa or fa in sur)
        c["year_match"] = None if not (year and c["year"]) else abs(int(year) - int(c["year"])) <= 1
    seen, top = set(), []
    for c in sorted(cands, key=lambda c: (-c["sim"], c["source"] != "crossref")):
        if c["id"].lower() not in seen:
            seen.add(c["id"].lower())
            top.append(c)
    return top[:n]


def proposal_for(path_arg, create=False):
    """Where resolve writes its proposal: next to the verify outputs, named from the input file's real path."""
    real = os.path.realpath(path_arg)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(os.path.basename(real))[0])[:60] or "input"
    return os.path.join(run_dir(create), "%s-%s.proposal.json" % (stem, hashlib.sha1(real.encode()).hexdigest()[:8]))


def cmd_resolve(a):
    """Network read (GET to Crossref and arXiv only): candidate DOIs/arXiv IDs for entries that have `claimed` but no
    `id`. Sends title, first author and year. Writes a proposal file in the private temp dir and never edits a spec;
    a human picks the match. Everything returned is data, not instructions."""
    items = resolve_inputs(read_json(a.file, "input"))
    if not items:
        sys.exit("no entries with claimed.title and no id in %s (expects needs_id, or spec entries without id)" % a.file)
    out_path = proposal_for(a.file, create=True)
    res = {"input": os.path.realpath(a.file), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entries": []}
    last_arxiv = 0.0
    for i, e in enumerate(items, 1):
        c = e["claimed"]
        q = " ".join(str(x) for x in (c.get("title"), first_surname(c.get("authors")), c.get("year")) if x)
        cands, errors = [], []
        try:
            cands += crossref_candidates(http("https://api.crossref.org/works?" + urllib.parse.urlencode(
                {"query.bibliographic": q, "rows": 3, "select": "DOI,title,author,issued,container-title,type"})))
        except (urllib.error.URLError, ValueError) as ex:
            errors.append("Crossref: %s" % ex)
        if "arxiv" in str(e.get("venue", "")).lower():
            time.sleep(max(0.0, 3.0 - (time.time() - last_arxiv)))  # arXiv asks for ~3 s between calls
            title_q = re.sub(r"[^\w\s-]", " ", c["title"])
            try:
                cands += arxiv_candidates(http("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
                    {"search_query": 'ti:"%s"' % " ".join(title_q.split()), "max_results": 3})))
            except (urllib.error.URLError, ValueError, ET.ParseError) as ex:
                errors.append("arXiv: %s" % ex)
            last_arxiv = time.time()
        top = rank_candidates(c, cands)
        res["entries"].append({"index": i, "claimed": c, "venue": e.get("venue"), "candidates": top, "errors": errors})
        print("\n[%d] %s | %s | %s | %s" % (i, c.get("title", "")[:90], c.get("authors", "-"), c.get("year", "-"), e.get("venue", "-")))
        for k, cd in enumerate(top, 1):
            flag = lambda v: "?" if v is None else ("yes" if v else "NO")
            print("    %d. sim=%.2f author=%-3s year=%-3s %-28s %s | %s | %s" % (
                k, cd["sim"], flag(cd["author_match"]), flag(cd["year_match"]), cd["id"], cd["year"],
                cd["first_author"][:20], cd["title"][:70]))
            print("       %s" % cd["container"][:90])
        for err in errors:
            print("    (%s)" % err)
        if not top and not errors:
            print("    no candidates found by these queries")
    write_json(out_path, res)
    print("\nwrote %s\nNothing was changed in any spec. Approve matches one by one; only then copy an ID into a spec."
          % out_path)


def pdf_url_ok(url):
    """True for an https URL to a named or public host: no userinfo, no loopback/private/link-local address."""
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    if u.scheme != "https" or not host or "@" in u.netloc or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """PDF downloads: a redirect must satisfy pdf_url_ok too (no downgrade to http, no bounce to a local address)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not pdf_url_ok(newurl):
            raise urllib.error.URLError("redirect to %s refused (https to a public host only)" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def pdf_body_problem(body, cap):
    """None if `body` (read up to cap + 1 bytes) is a PDF within the cap, else why not."""
    if len(body) > cap:
        return "larger than %d MB" % (cap // 1_000_000)
    if body[:5] != b"%PDF-":
        return "not a PDF (no %PDF- header)"
    return None


def fetch_pdf(urls, max_mb):
    """(url, bytes) for the first URL that yields a PDF within the cap, else (None, None). https only, redirects
    included; the body is never read past the cap."""
    cap = int(max_mb * 1_000_000)
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    for u in urls:
        if not pdf_url_ok(u):
            print("  PDF URL refused (https to a public host only): %s" % u)
            continue
        try:
            with opener.open(urllib.request.Request(u, headers=dict(UA, Accept="application/pdf")), timeout=90) as r:
                body = r.read(cap + 1)
        except Exception as ex:
            print("  PDF fetch failed: %s (%s)" % (u, ex))
            continue
        why = pdf_body_problem(body, cap)
        if why:
            print("  PDF skipped, %s: %s" % (why, u))
            continue
        return u, body
    return None, None


def note_html(inner):
    return inner if inner.lstrip().startswith("<") else "<p>%s</p>" % html.escape(inner)


def tag_objs(names, kind):
    """Zotero-native tag objects. type 0 = manual (default: regular tags a human can browse by);
    type 1 = automatic (red icon, hidden when 'Show Automatic' is off, bulk-deletable library-wide).
    NOTE: the connector's save path forces type 1; manual tags are set afterwards by organize()."""
    seen, out = set(), []
    for t in names:
        t = " ".join(str(t).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append({"tag": t, "type": kind})
    return out


SINGLE_VALUE_PREFIXES = ("prio:", "status:", "decision:")  # screening convention: one tag per prefix


def merged_tags(current, wanted):
    """The tag list organize writes: the spec's tags (with the spec's type, which turns the connector's automatic tags
    into regular ones) plus every existing tag the spec does not name, unchanged. A spec tag with a single-value
    prefix (prio:, status:, decision:) replaces existing tags with that prefix, so a changed decision does not leave
    the old one behind. Tags the user or another project added are never dropped. Pure; selftest calls it."""
    names = {t["tag"].lower() for t in wanted}
    owned = {p for p in SINGLE_VALUE_PREFIXES for t in wanted if t["tag"].lower().startswith(p)}
    keep = [{"tag": t["tag"], "type": t.get("type", 0)} for t in current
            if t["tag"].lower() not in names and not any(t["tag"].lower().startswith(p) for p in owned)]
    return list(wanted) + keep


def wanted_names(spec, e):
    names = list(spec.get("tags", [])) + list(e.get("tags", []))
    if e.get("status") == "unverified":
        names.append("unverified metadata")
    return names


def build_import(spec, allow_unverified):
    """Pure planning: reads Zotero (GET only) and returns what `import` would send."""
    lp = lib_path(spec["library"])
    coll_key = collection_key(lp, spec["collection"]) if spec.get("collection") else None
    ids, titles, note_texts = library_index(lp)
    kind = 1 if spec.get("tag_type") == "automatic" else 0
    items, entries, skipped = [], [], []
    for i, e in enumerate(spec["entries"], 1):
        label = e.get("id") or (e.get("manual") or {}).get("title", "?")[:50]
        st = e["status"]
        if st == "unverified" and not allow_unverified:
            skipped.append((label, "unverified metadata: needs --allow-unverified")); continue
        if st not in IMPORTABLE:
            skipped.append((label, st)); continue
        item = e["item"]
        doi = item.get("DOI", "").lower()
        if (doi and doi in ids) or (e.get("id") and e["id"].lower() in ids) or (st == "unverified" and norm(item["title"]) in titles):
            skipped.append((label, "already in library")); continue
        names = wanted_names(spec, e)
        notes = list(e.get("notes", []))
        if st == "unverified":
            notes.append("<h3>Unverified metadata</h3><p>The fields of this record were not resolved from Crossref/arXiv; "
                         "they were supplied by an LLM or copied from a source citation, and imported because the user "
                         "explicitly approved it:</p><blockquote>%s</blockquote><p>Check against the paper, then remove "
                         "the tag &ldquo;unverified metadata&rdquo;.</p>" % html.escape(e["user_affirmed"]))
        it = dict(item)
        it["id"] = "i%d" % i
        it["tags"] = tag_objs(names, kind)
        it["notes"] = [{"note": note_html(n)} for n in notes]
        it["attachments"] = []
        items.append(it)
        entries.append(e)
    n_entries = len(items)
    for n in spec.get("standalone_notes", []):
        if note_present(n, note_texts):
            skipped.append(("standalone note %r" % clean(n)[:40], "already in library"))
            continue
        items.append({"id": "n%d" % len(items), "itemType": "note", "note": note_html(n),
                      "tags": tag_objs(spec.get("tags", []), kind)})
    return {"lp": lp, "coll_key": coll_key, "items": items, "entries": entries, "n_entries": n_entries,
            "skipped": skipped, "kind": kind}


def describe(spec, b, opts):
    kind = "automatic" if b["kind"] == 1 else "regular"
    print("target: %s / %s%s" % (spec["library"], spec.get("collection") or "(library root)",
                                 "  [collection key %s]" % b["coll_key"] if b["coll_key"] else ""))
    items = b["items"]
    print("new items: %d | child notes: %d | standalone notes: %d"
          % (b["n_entries"], sum(len(i.get("notes", [])) for i in items), len(items) - b["n_entries"]))
    if not opts["pdf"]:
        print("PDFs: off (pass --pdf to plan and import to fetch open-access PDFs over https)")
    else:
        pdfs = pdf_plan(items, b["entries"], opts)
        ignored = sum(1 for e in b["entries"] if e.get("pdf")) if not opts["allow_spec_pdf"] else 0
        print("PDFs: %d of %d new items have https candidates (cap %d MB)%s" % (
            len(pdfs), b["n_entries"], opts["max_pdf_mb"],
            "; %d spec `pdf` URLs ignored (needs --allow-spec-pdf)" % ignored if ignored else ""))
        for k, urls in sorted(pdfs.items(), key=lambda kv: int(kv[0][1:])):
            print("  %s: %s" % (k, " | ".join(urls)))
    unv = [e for e in b["entries"] if e["status"] == "unverified"]
    for e in unv:
        print("  UNVERIFIED (user affirmed): %s | %s" % ((e["item"].get("title") or "")[:60], e["user_affirmed"][:80]))
    used = {t["tag"] for i in items for t in i.get("tags", [])}
    have = library_tags(b["lp"])
    print("tags (%s): new %s | already in library %s" % (kind, sorted(used - have), sorted(used & have)))
    if b["kind"] == 0 and items:
        print("note: import will follow with ONE Zotero approval dialog per 50 items to set these as regular tags")
    for s_ in b["skipped"]:
        print("  skip", *s_)


def subcolls_of(e):
    v = e.get("subcollection")
    return [] if not v else ([v] if isinstance(v, str) else list(v))


def organize_plan(spec, entries, note_htmls):
    """Pure planning (GET only): what organize would send to bring items already in the target collection tree to
    the spec's state.

    * tags: set as regular (manual) tags, because the connector forces automatic ones;
    * placement: items with `subcollection` are MOVED out of the parent collection into those
      subcollections (created when missing); memberships outside this tree are preserved.
    Subcollections that do not exist yet appear as "NEW:<name>" in the objects; organize() substitutes the real
    keys after creating them, so what is hashed is what is sent."""
    lp = lib_path(spec["library"])
    parent = collection_key(lp, spec["collection"]) if spec.get("collection") else None
    kind = 1 if spec.get("tag_type") == "automatic" else 0
    colls = all_collections(lp)
    subs = {c["data"]["name"].lower(): c["key"] for c in colls if parent and c["data"].get("parentCollection") == parent}
    want_names = []
    for e in entries:
        for n in subcolls_of(e):
            if n.lower() not in [w.lower() for w in want_names]:
                want_names.append(n)
    missing = sorted(n for n in want_names if n.lower() not in subs)
    for n in missing:
        subs[n.lower()] = "NEW:" + n
    tree_keys = ({parent} if parent else set()) | {k for k in subs.values() if not k.startswith("NEW:")}

    # items that currently live anywhere in the tree (parent + its subcollections)
    recs, start_paths = {}, [lp + ("/collections/" + k if k else "") for k in sorted(tree_keys)] or [lp]
    for base in start_paths:
        start = 0
        while True:
            page, hdr = zget(base + "/items/top", limit=100, start=start, include="data")
            for i in page:
                recs[i["key"]] = i
            start += len(page)
            if not page or start >= int(hdr.get("Total-Results", start)):
                break
    by_id, by_title, notes = {}, {}, []
    for i in recs.values():
        d = i["data"]
        for v in (d.get("DOI"), d.get("archiveID")):
            if v:
                by_id[v.lower()] = i
        if d.get("title"):
            by_title[norm(d["title"])] = i
        if d.get("itemType") == "note":
            notes.append((i, norm(clean(d.get("note", ""))[:600])))

    objs, missing_items, moves = [], [], []
    def plan_change(i, names, subnames, label):
        if i is None:
            missing_items.append(label); return
        d = i["data"]
        want_tags = merged_tags(d.get("tags", []), tag_objs(names, kind))
        tags_differ = sorted((t["tag"], t["type"]) for t in want_tags) != sorted((t["tag"], t.get("type", 0)) for t in d.get("tags", []))
        cur = list(d.get("collections", []))
        new = cur
        if subnames:
            dest = [subs[n.lower()] for n in subnames]
            new = [k for k in cur if k not in tree_keys and not k.startswith("NEW:")] + dest
        elif parent and parent not in cur:
            new = cur + [parent]
        coll_differ = sorted(new) != sorted(cur)
        if tags_differ or coll_differ:
            o = {"key": i["key"], "version": i["version"]}
            if tags_differ:
                o["tags"] = want_tags
            if coll_differ:
                o["collections"] = new
                moves.append((label, subnames))
            objs.append(o)
    for e in entries:
        item = e.get("item") or {}
        i = (by_id.get(item.get("DOI", "").lower()) or by_id.get((e.get("id") or "").lower())
             or by_title.get(norm(item.get("title", ""))))
        plan_change(i, wanted_names(spec, e), subcolls_of(e), e.get("id") or item.get("title", "?")[:40])
    for n in note_htmls:
        mine = norm(clean(n)[:600])
        best = max(notes, key=lambda rt: difflib.SequenceMatcher(None, mine, rt[1]).ratio(), default=None)
        ok = best and difflib.SequenceMatcher(None, mine, best[1]).ratio() >= 0.8
        plan_change(best[0] if ok else None, spec.get("tags", []), [], "standalone note")
    return {"parent": parent, "create": missing, "objs": objs, "moves": moves, "missing_items": missing_items}


def payload_hash(obj):
    """SHA-256 over a canonical serialisation (sorted keys, no whitespace, UTF-8) of what a write would send."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def allowlist_fingerprint(path=None):
    """{path, sha256} of the allowlist file as it is now (sha256 None if it cannot be read). Part of every plan hash,
    so a plan approved under one allowlist cannot be executed under another."""
    path = path or config_path()
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        digest = None
    return {"path": path, "sha256": digest}


def print_target(library, collection, fp=None):
    """The header every dry run prints: where the write would go and which allowlist file decides it."""
    fp = fp or allowlist_fingerprint()
    print("library:    %s\ncollection: %s\nallowlist:  %s\n            sha256 %s" % (
        library, collection or "(library root)", fp["path"], fp["sha256"] or "MISSING (every write will be refused)"))


def organize_payload(spec, p, fp=None):
    return {"library": spec["library"], "collection": spec.get("collection"), "parent_key": p["parent"],
            "create_subcollections": p["create"], "objects": p["objs"], "allowlist": fp or allowlist_fingerprint()}


def confirm_or_exit(cmd, given, expected, how):
    """Refuse unless the user-approved hash matches. The expected hash is deliberately not printed here: it must
    come from the reviewed plan output, not from an error message."""
    if given and given.strip().lower() == expected:
        return
    why = "--confirm HASH is required" if not given else \
        "--confirm %s does not match what %s would send now (the spec, flags or library changed)" % (given, cmd)
    sys.exit("%s refused: %s.\nRun `%s` with the same flags, show the user its output, and use the hash it prints."
             % (cmd, why, how))


def organize(spec, entries, note_htmls, dry=False, confirm=None, bound_by_import=False, show_hash=True):
    """Plan, show, and (unless dry) send organize's local-API writes: one approval dialog to create subcollections,
    then one per 50 items. A standalone run must be confirmed with the hash of the plan (--confirm); after
    `import`, the import hash already covers the intent and the items did not exist when it was computed."""
    p = organize_plan(spec, entries, note_htmls)
    objs = p["objs"]
    if p["create"]:
        print("subcollections to create under %r: %s" % (spec["collection"], p["create"]))
    print("organize: %d items to update (%d tag changes, %d placements), %d not found in the collection tree" %
          (len(objs), sum(1 for o in objs if "tags" in o), len(p["moves"]), len(p["missing_items"])))
    for m in p["missing_items"]:
        print("  not found:", m)
    h = payload_hash(organize_payload(spec, p))
    if dry:
        from collections import Counter
        for name, c in sorted(Counter(n for _, ns in p["moves"] for n in ns).items()):
            print("  -> %-50s %d item(s)" % (name, c))
        if show_hash and (objs or p["create"]):
            print_target(spec["library"], spec.get("collection"))
            print("organize hash (organize SPEC --confirm): %s" % h)
        return h
    if not objs and not p["create"]:
        return h
    if not bound_by_import:
        confirm_or_exit("organize", confirm, h, "organize SPEC --dry-run")
    if p["create"]:
        try:
            r = local_write(spec["library"], [{"name": n, "parentCollection": p["parent"]} for n in p["create"]], kind="collections")
        except TimeoutError:
            sys.exit("subcollections NOT created (no approval within 3 min); run `organize` again")
        made = r.get("success") or {k: v["key"] for k, v in r.get("successful", {}).items()}
        if len(made) != len(p["create"]):
            sys.exit("could not create all subcollections: %s" % json.dumps(r.get("failed")))
        real = {"NEW:" + n: made[str(i)] for i, n in enumerate(p["create"])}
        for o in objs:
            if "collections" in o:
                o["collections"] = [real.get(k, k) for k in o["collections"]]
        print("  created %d subcollections" % len(p["create"]))
    for i in range(0, len(objs), 50):
        try:
            r = local_write(spec["library"], objs[i:i + 50])
            print("  batch %d: updated %d, failed %d" % (i // 50 + 1, len(r.get("successful", {})), len(r.get("failed", {}))))
            for k, v in (r.get("failed") or {}).items():
                print("    failed:", v)
        except (urllib.error.HTTPError, TimeoutError) as ex:  # no click within local_write's 3 min is a TimeoutError
            why = "no approval within 3 min" if isinstance(ex, TimeoutError) else "HTTP %d: approval denied or expired" % ex.code
            print("  batch %d NOT applied (%s); run `organize` again later" % (i // 50 + 1, why))
            break


def cmd_organize(a):
    spec = load_resolved(a.spec)
    LOG_CONTEXT["collection"] = spec.get("collection")
    organize(spec, importable_entries(spec), spec.get("standalone_notes", []), dry=a.dry_run, confirm=a.confirm)


def import_opts(a):
    """The flags plan and import share; they are part of the import hash, so both must be given the same ones."""
    if a.allow_spec_pdf and not a.pdf:
        sys.exit("--allow-spec-pdf only makes sense with --pdf")
    if a.pdf and a.no_pdf:
        sys.exit("--pdf and --no-pdf contradict each other (no PDFs is the default)")
    return {"allow_unverified": bool(a.allow_unverified), "pdf": bool(a.pdf), "allow_spec_pdf": bool(a.allow_spec_pdf),
            "max_pdf_mb": a.max_pdf_mb}


def import_flags(opts):
    return "".join(["--allow-unverified " if opts["allow_unverified"] else "", "--pdf " if opts["pdf"] else "",
                    "--allow-spec-pdf " if opts["allow_spec_pdf"] else "",
                    "--max-pdf-mb %d " % opts["max_pdf_mb"] if opts["max_pdf_mb"] != 50 else ""])


def pdf_plan(items, entries, opts):
    """The PDF decision per new item: {item id: https URLs import will try, in order}. Part of the import hash.
    Nothing without --pdf. The spec's free-form `pdf` URL is tried first, but only with --allow-spec-pdf; otherwise
    it is dropped, also from candidate lists written by older versions of verify, which mixed it in."""
    if not opts["pdf"]:
        return {}
    out = {}
    for it, e in zip(items, entries):
        spec_pdf = e.get("pdf")
        urls = ([spec_pdf] if spec_pdf and opts["allow_spec_pdf"] else []) + [u for u in e.get("pdf_urls") or [] if u != spec_pdf]
        urls = [u for u in dict.fromkeys(urls) if pdf_url_ok(u)]
        if urls:
            out[it["id"]] = urls
    return out


def import_payload(spec, b, opts, fp=None):
    """Exactly what import would send, as one object: target, items (with child notes and tags), standalone
    notes, the follow-up placement, the PDF decisions, and the allowlist file it is checked against. plan prints
    its hash; import recomputes and compares."""
    return {"target": {"library": spec["library"], "collection": spec.get("collection"), "collection_key": b["coll_key"]},
            "items": b["items"], "tag_type": b["kind"],
            "placement": {it["id"]: subcolls_of(e) for it, e in zip(b["items"], b["entries"])},
            "pdf": {"policy": opts, "per_item": pdf_plan(b["items"], b["entries"], opts)},
            "allowlist": fp or allowlist_fingerprint()}


def cmd_plan(a):
    spec = load_resolved(a.spec)
    opts = import_opts(a)
    b = build_import(spec, opts["allow_unverified"])
    print_target(spec["library"], spec.get("collection"))
    describe(spec, b, opts)
    print("-- placement/tags of items already in the collection tree:")
    organize(spec, importable_entries(spec), spec.get("standalone_notes", []), dry=True, show_hash=False)
    if b["items"]:
        print("plan hash (import SPEC %s--confirm): %s" % (import_flags(opts), payload_hash(import_payload(spec, b, opts))))
    else:
        print("nothing to import")


def cmd_import(a):
    spec = load_resolved(a.spec)
    opts = import_opts(a)
    b = build_import(spec, opts["allow_unverified"])
    items, entries = b["items"], b["entries"]
    if not items:
        describe(spec, b, opts)
        print("nothing to import")
        return
    confirm_or_exit("import", a.confirm, payload_hash(import_payload(spec, b, opts)), "plan SPEC")
    target, tname, session = connector_scope(spec["library"], spec.get("collection"))
    describe(spec, b, opts)
    pdfs = pdf_plan(items, entries, opts)
    connector("saveItems", {"sessionID": session, "uri": "http://localhost/zotero-skill", "items": items})
    connector("updateSession", {"sessionID": session, "target": target, "tags": ""})
    got = 0
    for it, e in zip(items, entries):
        if not pdfs.get(it["id"]):
            continue
        url, body = fetch_pdf(pdfs[it["id"]], opts["max_pdf_mb"])
        if not body:
            print("  no PDF retrievable:", e.get("id")); continue
        meta = {"id": "a" + it["id"], "url": url, "contentType": "application/pdf", "parentItemID": it["id"],
                "title": "Full Text PDF", "sessionID": session}
        connector("saveAttachment", headers={"Content-Type": "application/pdf", "X-Metadata": json.dumps(meta)}, data=body)
        got += 1
        print("  PDF attached: %-30s %6.0f KB  %s" % (e.get("id"), len(body) / 1024, url))
    print("imported %d items (+%d standalone notes), %d PDFs -> %s" % (b["n_entries"], len(items) - b["n_entries"], got, tname))
    if (b["kind"] == 0 and any(i.get("tags") for i in items)) or any(subcolls_of(e) for e in entries):
        print("placing items and setting regular tags: Zotero will ask for approval...")
        organize(spec, entries, [i["note"] for i in items if i.get("itemType") == "note"], bound_by_import=True)


ZOTERO_VERSION = None


def require_zotero10():
    """Fail fast unless Zotero 10+ is running (older versions lack local-API writes)."""
    global ZOTERO_VERSION
    try:
        _, hdr = http(HOST + "/connector/ping", raw=True)
    except urllib.error.URLError:
        sys.exit("Zotero is not running (nothing on %s)" % HOST)
    ZOTERO_VERSION = hdr.get("X-Zotero-Version", "0")
    if int(re.match(r"\d+", ZOTERO_VERSION).group()) < 10:
        sys.exit("Requires Zotero 10+ (found %s)" % ZOTERO_VERSION)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status").set_defaults(f=cmd_status)
    sp.add_parser("selftest", help="verify the read-only guard (needs no Zotero)").set_defaults(f=cmd_selftest)
    sp.add_parser("libs").set_defaults(f=cmd_libs)
    sp.add_parser("allowlist", help="print the write allowlist (user-owned file) and what it permits").set_defaults(f=cmd_allowlist)
    s = sp.add_parser("log", help="print the last N entries of the write log (metadata only)")
    s.add_argument("-n", type=int, default=20); s.set_defaults(f=cmd_log)
    s = sp.add_parser("where", help="print the directory for verify output (and its files), or the output path for SPEC")
    s.add_argument("spec", nargs="?"); s.set_defaults(f=cmd_where)
    s = sp.add_parser("colls"); s.add_argument("lib"); s.set_defaults(f=cmd_colls)
    s = sp.add_parser("ls"); s.add_argument("lib"); s.add_argument("coll", nargs="?"); s.set_defaults(f=cmd_ls)
    s = sp.add_parser("find"); s.add_argument("lib"); s.add_argument("query"); s.set_defaults(f=cmd_find)
    s = sp.add_parser("notes", help="child notes of the ITEMs (key, dates, tags, text)")
    s.add_argument("lib"); s.add_argument("items", nargs="+", metavar="ITEM"); s.add_argument("--full", action="store_true", help="whole note text, not 200 chars")
    s.set_defaults(f=cmd_notes)
    s = sp.add_parser("path", help="local file path(s) of attachments: of the ITEMs (parent or attachment keys), or of all in --coll; flags missing files")
    s.add_argument("lib"); s.add_argument("items", nargs="*", metavar="ITEM"); s.add_argument("--coll", help="every attachment in this collection instead of ITEMs")
    s.set_defaults(f=cmd_path)
    s = sp.add_parser("verify", help="resolve IDs via Crossref/arXiv; output goes to a private temp dir (see `where SPEC`)")
    s.add_argument("spec")
    s.add_argument("--no-pdf", action="store_true"); s.set_defaults(f=cmd_verify)
    def import_args(s):
        s.add_argument("spec")
        s.add_argument("--allow-unverified", action="store_true", help="also import user-affirmed entries with unverified (manual) metadata")
        s.add_argument("--pdf", action="store_true", help="fetch open-access PDFs (https only); off by default")
        s.add_argument("--allow-spec-pdf", action="store_true", help="with --pdf: also try the spec's own free-form `pdf` URLs")
        s.add_argument("--no-pdf", action="store_true", help=argparse.SUPPRESS)  # the default now; kept so old commands still parse
        s.add_argument("--max-pdf-mb", type=int, default=50)
    s = sp.add_parser("resolve", help="NETWORK READ: candidate DOIs/arXiv IDs for entries with `claimed` but no `id` (Crossref, arXiv); writes a proposal file, edits no spec")
    s.add_argument("file", help="pending file with needs_id, or a spec whose entries lack id"); s.set_defaults(f=cmd_resolve)
    s = sp.add_parser("plan", help="READ-ONLY: show what import would create and print the hash import --confirm needs; writes nothing")
    import_args(s); s.set_defaults(f=cmd_plan)
    s = sp.add_parser("import", help="write items/notes/PDFs via the connector (no Zotero dialog); needs --confirm HASH from plan")
    import_args(s); s.add_argument("--confirm", metavar="HASH", help="the hash `plan` printed for the same spec and flags")
    s.set_defaults(f=cmd_import)
    s = sp.add_parser("organize", help="set tags as regular tags and place items into subcollections for items already imported (local-API writes)")
    s.add_argument("spec"); s.add_argument("--dry-run", action="store_true", help="show the changes and their hash, write nothing")
    s.add_argument("--confirm", metavar="HASH", help="the hash `organize --dry-run` printed")
    s.set_defaults(f=cmd_organize)
    s = sp.add_parser("attach", help="attach a local file to an existing item (--item KEY; 3 approval dialogs) or to a new item made from a DOI/arXiv ID (--new ID; no dialog)")
    s.add_argument("lib"); s.add_argument("file")
    s.add_argument("--item", help="key of the existing parent item"); s.add_argument("--new", metavar="ID", help="DOI or arXiv:ID of a new item to create")
    s.add_argument("--coll", help="with --new: collection to put it in (default: library root)"); s.add_argument("--title", help="attachment title (default: Full Text PDF / file name)")
    s.add_argument("--max-mb", type=int, default=50); s.set_defaults(f=cmd_attach)
    s = sp.add_parser("note", help="add child notes to existing items from a JSON list of {parent, note, tags} (one approval dialog); --dry-run, then --confirm HASH")
    s.add_argument("lib"); s.add_argument("file")
    s.add_argument("--dry-run", action="store_true", help="show the notes and their hash, write nothing")
    s.add_argument("--confirm", metavar="HASH", help="the hash `note --dry-run` printed")
    s.set_defaults(f=cmd_note)
    s = sp.add_parser("trash", help="move items to Zotero's trash (one approval dialog)"); s.add_argument("lib"); s.add_argument("keys", nargs="+"); s.set_defaults(f=cmd_trash)
    a = p.parse_args()
    for stream in (sys.stdout, sys.stderr):  # a title with e.g. U+2212 must not crash output piped to a cp1252 console
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    LOG_CONTEXT["command"] = a.cmd
    global GUARD
    GUARD = LOCAL_ONLY if a.cmd in READ_ONLY_CMDS else REMOTE_READ if a.cmd in VERIFY_CMDS else None
    if a.cmd in ("selftest", "where", "allowlist", "log", "resolve"):  # need no Zotero (allowlist uses it if running)
        return a.f(a)
    require_zotero10()
    a.f(a)


if __name__ == "__main__":
    main()
