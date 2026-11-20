"""Self-check: build a fake home with every kind of source, scan it, assert what comes back.
Run: python3 test_wince.py
"""
import importlib
import json
import os
import re
import sys
import tempfile
import urllib.parse


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def build(home):
    c = os.path.join(home, ".claude")
    proj = os.path.join(home, "code", "proj")
    repo = os.path.join(home, "code", "client-repo")
    plug = os.path.join(c, "plugins", "cache", "mk", "tool", "1.0")
    w(os.path.join(c, "CLAUDE.md"), "Keep replies short.")
    w(os.path.join(proj, "CLAUDE.md"), "Project rules.\nSee @extra.md for more.")
    w(os.path.join(proj, "extra.md"), "Imported text.")
    w(os.path.join(proj, "AGENTS.md"), "Not read.")
    w(os.path.join(c, "skills", "local", "SKILL.md"), '---\nname: local\ndescription: Use for "start a ticket" requests.\n---\n')
    w(os.path.join(c, "skills", "quiet", "SKILL.md"), "---\nname: quiet\ndescription: x\ndisable-model-invocation: true\n---\n")
    os.makedirs(os.path.join(c, "skills", "empty"))
    w(os.path.join(repo, "skills", "linked", "SKILL.md"), '---\nname: linked\ndescription: Use for "start a ticket" in the client repo.\n---\n')
    w(os.path.join(repo, "skills", "unlinked", "SKILL.md"), "---\nname: unlinked\ndescription: y\n---\n")
    os.symlink(os.path.join(repo, "skills", "linked"), os.path.join(c, "skills", "linked"))
    os.symlink(os.path.join(home, "nowhere"), os.path.join(c, "skills", "broken"))
    w(os.path.join(c, "mcp.json"), json.dumps({"mcpServers": {"stray": {}}}))
    w(os.path.join(c, "settings.json"), json.dumps({"enabledPlugins": {"tool@mk": True, "off@mk": False},
                                                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))
    w(os.path.join(c, "plugins", "installed_plugins.json"), json.dumps({"plugins": {
        "tool@mk": [{"scope": "user", "installPath": plug}], "off@mk": [{"scope": "user", "installPath": plug + "x"}]}}))
    w(os.path.join(plug, ".claude-plugin", "plugin.json"), json.dumps({"name": "tool"}))
    w(os.path.join(plug, "hooks", "hooks.json"), json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x", "statusMessage": "Loading tool"}]}]}}))
    w(os.path.join(home, ".claude.json"), json.dumps({"mcpServers": {"usr": {}}}))
    pdir = os.path.join(c, "projects", re.sub(r"[^A-Za-z0-9]", "-", proj))
    w(os.path.join(pdir, "memory", "MEMORY.md"), "- [A](a.md) — a")
    log = [
        {"type": "attachment", "version": "9.9.9", "attachment": {"type": "skill_listing", "content": 123}},  # a shape wince doesn't know
        {"type": "attachment", "timestamp": "2026-11-01T00:00:00Z", "attachment": {"type": "hook_success", "hookEvent": "SessionStart", "command": "Loading tool", "content": "TOOL MODE ON"}},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "- local: Use for \"start a ticket\" requests.\n- linked: Use for \"start a ticket\" in the client repo.\n- tool:helper: helps\n- builtin-thing: built in"}},
        {"type": "attachment", "attachment": {"type": "mcp_instructions_delta", "addedBlocks": ["## usr\nUser server notes.", "## 0123abcd-0000-0000-0000-000000000000\nAcme: does things."]}},
        {"type": "attachment", "attachment": {"type": "deferred_tools_delta", "addedNames": ["mcp__usr__one", "Read"]}},
        {"type": "attachment", "attachment": {"type": "instructions", "files": [{"path": os.path.join(c, "CLAUDE.md"), "type": "User"}, {"path": os.path.join(home, "gone.md"), "type": "Project"}]}},
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["x" * 400]}},
        {"type": "assistant"},
    ]
    w(os.path.join(pdir, "s.jsonl"), "\n".join(json.dumps(x) for x in log))
    return proj


def main():
    with tempfile.TemporaryDirectory() as home:
        home = os.path.realpath(home)
        proj = build(home)
        os.environ["HOME"] = home
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        wh = importlib.reload(importlib.import_module("wince.core"))
        sc = wh.scan(proj)
        labels = {s["label"]: s for s in sc["sources"]}
        nl = " | ".join(z["label"] + " " + z["detail"] for z in sc["notLoaded"])
        titles = [f["title"] for f in sc["findings"]]

        assert "~/code/proj/CLAUDE.md" in labels and "~/code/proj/extra.md (imported)" in labels, labels.keys()
        assert labels["Auto memory"]["kind"] == "memory" and labels["Auto memory"]["layer"] == "user", "memory lives in the config folder"
        assert labels["tool · SessionStart hook"]["layer"] == "plugin", "hook output attributed to its plugin"
        assert labels["~/.claude/skills ← client-repo"]["count"] == 1
        assert labels["tool skills"]["layer"] == "plugin" and labels["Built-in skills"]["layer"] == "builtin"
        assert labels["usr (MCP)"]["layer"] == "user" and labels["Connector: Acme"]["layer"] == "account"
        assert labels["Claude Code system prompt"]["tokens"] == 100 + 1
        assert "AGENTS.md" in nl and "off@mk" in nl and "~/.claude/mcp.json" in nl
        assert "skill empty" in nl and "skill broken" in nl and "skill unlinked" in nl
        assert any("user-only" in z["label"] for z in sc["zeroToken"])
        assert any("start a ticket" in t for t in titles), titles
        assert any("other folders" in t for t in titles), titles
        assert any("gone.md" in n for n in sc["notes"]) and any("extra.md" in n for n in sc["notes"]), sc["notes"]
        assert wh.parse_listing("- a:b: x: y\n- c")["a:b"] == "- a:b: x: y"

        # Connectors are named from the product words their tools share, since only a UUID is stored.
        jc = {"name": "uuid", "body": "", "tools": ["getJiraIssue", "createJiraIssue", "getConfluencePage", "searchConfluenceUsingCql", "fetch"]}
        assert wh.connector_name(jc) == "Jira / Confluence", wh.connector_name(jc)

        # tilde() only shortens whole path components.
        assert wh.tilde(home + "x/y") == home + "x/y" and wh.tilde(home + "/y") == "~/y"
        # Odd shapes in config files don't crash a scan.
        w(os.path.join(home, ".claude", "settings.local.json"), "[]")
        assert wh.scan(proj)["sources"]

        # Session logs are an internal format: odd records are skipped and reported, not fatal.
        assert sc["runtime"]["claudeCode"] == "9.9.9" and labels["Built-in skills"]["count"] >= 1
        assert any("1 records wince couldn't read" in n for n in sc["notes"]), sc["notes"]
        odd = os.path.join(home, "code", "odd")
        os.makedirs(odd)
        w(os.path.join(wh.project_dir(odd), "x.jsonl"), json.dumps({"type": "attachment", "attachment": {"type": "something_new"}}))
        so = wh.scan(odd)
        assert so["runtime"] is None and any("Found 1 session logs" in n for n in so["notes"]) and not any("No session log" in n for n in so["notes"]), so["notes"]

        # A second config folder (CLAUDE_CONFIG_DIR) has its own CLAUDE.md, settings and .claude.json.
        alt = os.path.join(home, ".claude-alt")
        w(os.path.join(alt, "CLAUDE.md"), "Alt rules.")
        w(os.path.join(alt, "settings.json"), "{}")
        w(os.path.join(alt, ".claude.json"), json.dumps({"mcpServers": {"altsrv": {"command": "x"}}}))
        os.makedirs(os.path.join(alt, "skills"))
        os.symlink(os.path.join(home, ".claude", "skills", "local"), os.path.join(alt, "skills", "local"))
        sa = wh.scan(proj, "~/.claude-alt")
        # Linking a skill from another config folder doesn't make that folder's other skills orphans.
        assert not any("skill quiet" == z["label"] for z in sa["notLoaded"]), [z["label"] for z in sa["notLoaded"]]
        al = {x["label"]: x for x in sa["sources"]}
        # ~/.claude/CLAUDE.md still loads, as a project file found walking up to ~ (seen in real session logs).
        assert al["~/.claude-alt/CLAUDE.md"]["layer"] == "user" and al["~/.claude/CLAUDE.md"]["layer"] == "project", al.keys()
        assert "altsrv (MCP)" in al and "usr (MCP)" not in al, al.keys()
        assert sa["config"] == "~/.claude-alt" and "~/.claude-alt" in sa["configs"] and wh.CLAUDE == wh.DEFAULT_CLAUDE
        assert any(l["label"] == "User (~/.claude-alt)" for l in sa["layers"])

        # The server: every /api/ call needs the header; scans run live and only recent folders and checks are saved.
        import stat, threading, urllib.request, urllib.error
        st_path = os.path.join(home, "wince", "state.json")
        srv = wh.make_server(0, st_path)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body=None, headers={"X-Wince": "1"}):
            req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read()
                return e.code, json.loads(body) if body.startswith(b"{") else {}
        q = "/api/scan?folder=" + urllib.parse.quote(proj)
        assert call(q, headers={})[0] == 403
        assert call("/api/scan?folder=/nope")[0] == 400
        assert call(q + "&config=/tmp")[0] == 400
        code, r = call(q)
        assert code == 200 and r["scan"]["config"] == "~/.claude" and "check" not in r["scan"], (code, r)
        assert not any("text" in x for x in r["scan"]["sources"]), "full texts stay on the server"
        assert r["recent"] == [{"folder": "~/code/proj", "config": "~/.claude"}], r["recent"]
        assert stat.S_IMODE(os.stat(st_path).st_mode) == 0o600, oct(os.stat(st_path).st_mode)
        assert call(q + "&config=~/.claude-alt")[1]["recent"][0]["config"] == "~/.claude-alt"

        # A conflict check is saved as locations, not quotes, and read back from the live files.
        real = wh.analyze
        quote = "Imported text."
        wh.analyze = lambda sc: [{"id": "ai-1", "type": "conflict", "title": "t", "body": "b", "sources": [], "analyzed": True,
                                  "detail": {"pieces": [{"source": next(x["id"] for x in sc["sources"] if x["origin"] == "~/code/proj/extra.md"), "quote": quote, "path": "~/code/proj/extra.md"}], "options": []}}]
        assert call("/api/check", {"folder": "~/code/proj"})[0] == 200
        assert quote not in open(st_path).read(), "a saved check must not hold the quoted text"
        f = call(q)[1]["scan"]
        piece = f["findings"][0]["detail"]["pieces"][0]
        assert f["check"]["stale"] is False and piece["quote"] == quote and f["findings"][0]["sources"], f["findings"][0]
        w(os.path.join(proj, "extra.md"), "New first line.\nImported text.")  # moved, not changed
        f = call(q)[1]["scan"]
        assert f["check"]["stale"] is True and f["findings"][0]["detail"]["pieces"][0]["quote"] == quote
        w(os.path.join(proj, "extra.md"), "Rewritten.")  # gone
        assert call(q)[1]["scan"]["findings"][0]["detail"]["pieces"][0]["quote"] is None
        # A failed check keeps the earlier one instead of replacing it with nothing.
        wh.analyze = lambda sc: None
        assert call("/api/check", {"folder": "~/code/proj"})[0] == 502 and call(q)[1]["scan"]["check"]
        wh.analyze = real
        assert call("/api/forget", {"folder": "~/code/proj"})[0] == 200 and "check" not in call(q)[1]["scan"]

        # The open endpoint opens only paths a live scan recorded; it never takes a path from the page.
        assert call("/api/open", {"folder": "~/code/proj", "source": "nope"})[0] == 404
        assert call("/api/open", {"folder": "~/code/proj", "path": "/etc/hosts"})[0] == 404
        # Requests with another Host (DNS rebinding) are refused, even with the header.
        assert call("/api/recent", headers={"X-Wince": "1", "Host": "evil.example:80"})[0] == 403
        # An unreadable state file is refused, not silently replaced.
        bad = os.path.join(home, "bad.json"); w(bad, "garbage")
        try:
            wh.remember({"folder": "x", "config": "y"}, bad); raise AssertionError("overwrote an unreadable state file")
        except ValueError:
            assert open(bad).read() == "garbage"
        srv.shutdown()

        # Canary: a new Claude Code version whose log lacks something the old one had gets a note until it's back.
        cpath = os.path.join(home, "canary.json")
        assert not any(n.startswith("Since Claude Code") for n in wh.canary(wh.scan(proj), cpath)["notes"]), "first scan only sets a baseline"
        logf = os.path.join(wh.project_dir(proj), "s.jsonl")
        orig = open(logf).read()
        w(logf, orig.replace('"9.9.9"', '"10.0.0"').replace("hook_success", "hook_renamed"))
        n = [x for x in wh.canary(wh.scan(proj), cpath)["notes"] if x.startswith("Since Claude Code")]
        assert n and "hook output" in n[0] and "10.0.0" in n[0] and "9.9.9" in n[0], n
        assert any(x.startswith("Since Claude Code") for x in wh.canary(wh.scan(proj), cpath)["notes"]), "keeps warning on the same version"
        w(logf, orig.replace('"9.9.9"', '"10.0.0"'))  # wince learns the new shape, or the hook comes back
        assert not any(x.startswith("Since Claude Code") for x in wh.canary(wh.scan(proj), cpath)["notes"]), "clears once it's back"
        w(logf, orig)

        # One version for the scanner and the app.
        js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wince", "app", "common.js")).read()
        assert f'const VERSION = "{wh.VERSION}"' in js and sc["wince"] == wh.VERSION
        src = [{"id": "project-memory-x", "origin": "~/m", "kind": "memory", "text": "t"}]
        assert wh.instr_hash(src) == wh.instr_hash([dict(src[0], id="user-memory-x")]), "regrouping sources keeps the conflict check"
        print("ok")


if __name__ == "__main__":
    main()
