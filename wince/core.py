#!/usr/bin/env python3
"""wince: show where everything in a Claude Code session comes from, for one folder.

Two kinds of evidence:
  disk    CLAUDE.md files, rules, imports, memory, settings, skills, plugins, hooks, MCP config.
  runtime the newest session log for the folder (~/.claude/projects/<slug>/*.jsonl), which records
          what actually reached the model: hook output, the skill listing, MCP instructions, tools.

Usage:
  wince serve                            # the app at http://localhost:8765/, opened in your browser
  wince scan [FOLDER ...] [--analyze]    # a summary; --analyze also saves a conflict check

Scans always run live. ~/.wince/state.json (owner-only) keeps the recent folders and each conflict check,
with the location of every quote rather than its text.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

VERSION = "0.1.0"  # keep in step with VERSION in app/common.js
HOME = os.path.expanduser("~")
DEFAULT_CLAUDE = os.path.join(HOME, ".claude")
# The config folder being scanned. scan() swaps these for a custom folder (what CLAUDE_CONFIG_DIR points at).
CLAUDE = DEFAULT_CLAUDE
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
SCAN_LOCK = threading.Lock()
MANAGED = {"darwin": "/Library/Application Support/ClaudeCode", "win32": r"C:\Program Files\ClaudeCode"}.get(sys.platform, "/etc/claude-code")
HERE = os.path.dirname(os.path.abspath(__file__))
# Scans live outside the install so upgrades keep them. WINCE_HOME moves them.
DATA = os.path.expanduser(os.environ.get("WINCE_HOME") or "~/.wince")
STATE = os.path.join(DATA, "state.json")

LAYERS = [
    {"id": "org", "label": "Organization / managed", "note": "Set by your admin. Outranks everything you write."},
    {"id": "user", "label": "User (~/.claude)", "note": "Your config folder: settings, skills and CLAUDE.md for every project, plus the auto memory and local MCP servers it keeps for this folder."},
    {"id": "project", "label": "Project tree", "note": "CLAUDE.md, rules and .claude/ in this folder and its parents."},
    {"id": "plugin", "label": "Plugins", "note": "Enabled through enabledPlugins in your settings."},
    {"id": "account", "label": "claude.ai account", "note": "Connectors and synced skills attached to your account, not to a file."},
    {"id": "builtin", "label": "Built into Claude Code", "note": "Ships with the app. You can see it, not edit it."},
]
KINDS = [
    {"id": "instructions", "label": "Instructions"},
    {"id": "memory", "label": "Memory"},
    {"id": "skills", "label": "Skill descriptions"},
    {"id": "agents", "label": "Agent descriptions"},
    {"id": "mcp", "label": "MCP instructions & tools"},
    {"id": "harness", "label": "System prompt & tools"},
]


# ---------- small helpers ----------

def tok(s):
    # shortcut: chars / 4 is a rough token estimate; swap in a real tokenizer if exact numbers matter
    return round(len(s) / 4) if s else 0


def tilde(p):
    return "~" + p[len(HOME):] if p and (p == HOME or p.startswith(HOME + os.sep)) else p


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def load_json(p):
    t = read(p)
    if t is None:
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def frontmatter(text):
    # shortcut: flat "key: value" lines only; folded YAML (description: >) comes back as ">"
    m = re.match(r"^---\s*\n(.*?)\n---", text or "", re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and line[:1] not in (" ", "\t"):
                fm[k.strip()] = v.strip().strip("'\"")
    return fm


def excerpt(text, n=1800):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "\n…"


def ancestors(folder):
    out, d = [], folder
    while True:
        out.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            return out
        d = parent


def git_root(folder):
    try:
        r = subprocess.run(["git", "-C", folder, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------- runtime evidence: the newest session log ----------

def project_dir(folder):
    for cand in (folder, git_root(folder)):
        if cand:
            d = os.path.join(CLAUDE, "projects", re.sub(r"[^A-Za-z0-9]", "-", cand))
            if os.path.isdir(d):
                return d
    return os.path.join(CLAUDE, "projects", re.sub(r"[^A-Za-z0-9]", "-", folder))


# Session logs are Claude Code's internal format, not an API. These are the records wince reads,
# checked against Claude Code 2.1.x; anything else in a log is ignored.
# Records every session writes. Others (MCP instructions, tool names, skill listing) depend on what is installed, so their absence says nothing.
RUNTIME_RECORDS = {"prompt_snapshot": "system prompt", "instructions": "instruction file list"}


def instr_hash(sources):
    """Changes only when an instruction or memory text changes, not when wince regroups sources."""
    return hashlib.sha1("".join(x["origin"] + x.get("text", "") for x in sources if x["kind"] in ("instructions", "memory")).encode()).hexdigest()


def read_runtime(pdir):
    """What the newest readable session in this folder loaded before Claude's first reply.
    Returns (runtime or None, a note for the page when the logs couldn't be read as expected)."""
    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:  # a broken symlink
            return 0
    logs = sorted(glob.glob(os.path.join(pdir, "*.jsonl")), key=mtime, reverse=True)
    versions = set()
    for path in logs[:20]:
        rt = {"path": path, "date": None, "version": None, "hooks": [], "skills": None, "mcp": {}, "tools": [], "agents": [],
              "instructions": [], "system": 0, "failed_mcp": [], "auth_mcp": [], "seen": set(), "skipped": 0, "cwd": None}
        found = False
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(o, dict):
                    continue
                if isinstance(o.get("cwd"), str):
                    rt["cwd"] = rt["cwd"] or o["cwd"]
                if o.get("type") == "assistant":
                    break
                rt["version"] = rt["version"] or o.get("version")
                rt["date"] = rt["date"] or o.get("timestamp")
                a = o.get("attachment")
                if not isinstance(a, dict):
                    continue
                t = a.get("type")
                if not isinstance(t, str):
                    continue
                try:
                    read_record(rt, t, a)
                except (AttributeError, TypeError, ValueError):
                    rt["skipped"] += 1  # a record in a shape this version of wince doesn't know
                    continue
                rt["seen"].add(t)
                found = found or bool(t)
        if rt["version"]:
            versions.add(rt["version"])
        if found and (rt["skills"] is not None or rt["mcp"] or rt["system"]):
            missing = [label for t, label in RUNTIME_RECORDS.items() if t not in rt["seen"]]
            note = None
            if missing or rt["skipped"]:
                note = (f"The last session log (Claude Code {rt['version'] or 'unknown version'}) had "
                        + (f"no {', '.join(missing)}" if missing else "")
                        + (" and " if missing and rt["skipped"] else "")
                        + (f"{rt['skipped']} records wince couldn't read" if rt["skipped"] else "")
                        + ", so those pieces may be missing here. Claude Code may have changed its log format.")
            rt["seen"] = sorted(x for x in rt["seen"] if x)
            return rt, note
    if logs:
        return None, (f"Found {len(logs)} session logs for this folder but couldn't read what they loaded"
                      + (f" (Claude Code {', '.join(sorted(versions))})" if versions else "")
                      + ". Claude Code may have changed its log format, so only what's on disk is shown.")
    return None, None


def read_record(rt, t, a):
    """Fold one session-log attachment into rt."""
    if t == "hook_success" and a.get("hookEvent") == "SessionStart" and isinstance(a.get("content"), str) and a["content"]:
        rt["hooks"].append({"name": a.get("hookName"), "command": a.get("command", ""), "content": a["content"]})
    elif t == "skill_listing" and rt["skills"] is None:
        rt["skills"] = a.get("content", "")
        if not isinstance(rt["skills"], str):
            rt["skills"] = None
            raise TypeError("skill listing is not text")
    elif t == "mcp_instructions_delta":
        for block in a.get("addedBlocks", []):
            head, _, body = block.partition("\n")
            rt["mcp"][head.lstrip("# ").strip()] = body
    elif t == "deferred_tools_delta":
        rt["tools"] += [x for x in a.get("addedNames") or [] if isinstance(x, str)]
        rt["failed_mcp"] += [x.get("name", str(x)) if isinstance(x, dict) else str(x) for x in a.get("failedMcpServers") or []]
        rt["auth_mcp"] += [x.get("name", str(x)) if isinstance(x, dict) else str(x) for x in a.get("needsAuthMcpServers") or []]
    elif t == "agent_listing_delta":
        rt["agents"] += [x for x in a.get("addedLines") or [] if isinstance(x, str)]
    elif t == "instructions":
        rt["instructions"] += [{"path": x.get("path"), "type": x.get("type")} for x in a.get("files", [])]
    elif t == "prompt_snapshot":
        rt["system"] = sum(len(x) for x in a.get("systemPrompt", []) if isinstance(x, str))


def parse_listing(text):
    """'- name: description' lines → {name: line}. Names may carry ':' but never ': '."""
    out = {}
    for chunk in re.split(r"\n(?=- )", "\n" + (text or "")):
        chunk = chunk.strip()
        if not chunk.startswith("- "):
            continue
        body = chunk[2:]
        name = body.split(": ", 1)[0].split(" (", 1)[0].strip()
        out[name] = chunk
    return out


# ---------- disk evidence ----------

def settings_layers(folder):
    files = [("org", os.path.join(MANAGED, "managed-settings.json")),
             ("user", os.path.join(CLAUDE, "settings.json")),
             ("user", os.path.join(CLAUDE, "settings.local.json")),
             ("project", os.path.join(folder, ".claude", "settings.json")),
             ("project", os.path.join(folder, ".claude", "settings.local.json"))]
    return [(layer, p, d) for layer, p in files if isinstance(d := load_json(p), dict)]


def merged_enabled_plugins(layers):
    enabled = {}
    order = [x for x in layers if x[0] != "org"] + [x for x in layers if x[0] == "org"]  # managed wins
    for _, _, d in order:
        enabled.update(d.get("enabledPlugins") or {})
    return enabled


def iter_hooks(hooks_obj):
    """Yield (event, matcher, hook) from a settings/plugin hooks object."""
    if isinstance(hooks_obj, dict) and isinstance(hooks_obj.get("hooks"), dict):
        hooks_obj = hooks_obj["hooks"]
    for event, groups in (hooks_obj or {}).items():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                yield event, g.get("matcher", ""), h


def plugins_for(folder, enabled):
    reg = load_json(os.path.join(CLAUDE, "plugins", "installed_plugins.json")) or {}
    active, inactive = [], []
    plugins = reg.get("plugins") if isinstance(reg, dict) else None
    for full, entries in (plugins if isinstance(plugins, dict) else {}).items():
        entries = [e for e in (entries if isinstance(entries, list) else [entries]) if isinstance(e, dict)]  # older files keep one entry, not a list
        here = [e for e in entries if e.get("scope") == "user" or e.get("projectPath") in (folder, git_root(folder))]
        root = (here or entries or [{}])[0].get("installPath")
        info = {"full": full, "name": full.split("@")[0], "root": root}
        if here and enabled.get(full) is True:
            active.append(info)
        elif here:
            inactive.append({**info, "why": "installed, disabled in settings"})
        else:
            where = sorted({tilde(e.get("projectPath", "?")) for e in entries})
            inactive.append({**info, "why": "installed only for " + ", ".join(where)})
    return active, inactive


def plugin_parts(p):
    root = p["root"] or ""
    man = load_json(os.path.join(root, ".claude-plugin", "plugin.json")) or load_json(os.path.join(root, "plugin.json")) or {}
    hook_files = man.get("hooks") or ["./hooks/hooks.json"]
    hook_files = hook_files if isinstance(hook_files, list) else [hook_files]
    hooks = []
    for hf in hook_files:
        d = hf if isinstance(hf, dict) else load_json(os.path.join(root, hf))
        hooks += [(e, m, h) for e, m, h in iter_hooks(d)]
    mcp = (load_json(os.path.join(root, ".mcp.json")) or {}).get("mcpServers") or man.get("mcpServers") or {}
    skills = {}
    for sk in glob.glob(os.path.join(root, "skills", "*", "SKILL.md")):
        fm = frontmatter(read(sk))
        skills[fm.get("name") or os.path.basename(os.path.dirname(sk))] = {"path": sk, **fm}
    return {"hooks": hooks, "mcp": mcp if isinstance(mcp, dict) else {}, "skills": skills}


def skill_dirs(base):
    """Skills folder entries with where each really lives."""
    out = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:  # missing or unreadable
        return out
    for name in names:
        p = os.path.join(base, name)
        if name.startswith(".") or name == "synced" or not (os.path.isdir(p) or os.path.islink(p)):
            continue
        real = os.path.realpath(p)
        md = os.path.join(p, "SKILL.md")
        entry = {"dir": p, "real": real, "link": os.path.islink(p), "broken": os.path.islink(p) and not os.path.exists(real),
                 "has_md": os.path.isfile(md)}
        if entry["has_md"]:
            fm = frontmatter(read(md))
            entry.update(name=fm.get("name") or name, description=fm.get("description", ""),
                         user_only=fm.get("disable-model-invocation", "").lower() == "true")
        if entry["link"]:
            parent = os.path.dirname(real)
            entry["repo"] = os.path.dirname(parent) if os.path.basename(parent) == "skills" else parent
        out[name] = entry
    return out


def imports_of(path, text, seen, depth=0):
    """@path imports in a CLAUDE.md, outside code fences."""
    out = []
    if depth > 4:
        return out
    text = re.sub(r"```.*?```", "", text or "", flags=re.S)
    for m in re.finditer(r"(?<![\w`])@((?:~|\.{1,2})?/?[\w.\-/]+\.\w+)", text):
        ref = m.group(1)
        p = os.path.expanduser(ref) if ref.startswith("~") else os.path.normpath(os.path.join(os.path.dirname(path), ref))
        if p in seen or not os.path.isfile(p):
            continue
        seen.add(p)
        t = read(p) or ""
        out.append((p, t, path))
        out += imports_of(p, t, seen, depth + 1)
    return out


# Words in tool names that say what a tool does, not which product it talks to.
TOOL_WORDS = set("""a add all an and append apply as ask at batch by cancel check clear close content context control copy create
current data delete describe detail details discover edit export fetch file files find for from get guide id ids in info
insert invoke is item items list lookup make me meta metadata modify move my new of on open or post put query read
remove rename resource resources run save search send set share shared show start status stop sync the to unshare
update upload user users using values view with write
assets capabilities certify collaborator collaborators comment comments field fields issue issues link links live
note notes page pages result results role transcript transition transitions type types worklog""".split())


def connector_name(s):
    """A readable name for a claude.ai connector, whose server name is only a UUID. Claude Code doesn't store the
    display name, so use the product the instructions name, else the product words its tool names share."""
    m = re.match(r"([A-Z][\w .]{2,20}):", s["body"])
    if m:
        return m.group(1)
    counts = {}
    for t in s["tools"]:
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", t.replace("_", " "))
        for wd in {x.lower() for x in words}:
            if len(wd) > 2 and wd not in TOOL_WORDS:
                counts[wd] = counts.get(wd, 0) + 1
    top = sorted((n, -i, wd) for i, (wd, n) in enumerate(counts.items()) if n >= 2)[::-1][:2]
    if top:
        return " / ".join(wd.capitalize() for _, _, wd in top)
    first = s["body"].split(".")[0].strip()
    return first[:40] if first else s["name"][:8]


# ---------- the scan ----------

def config_dirs():
    """Claude config folders in your home folder: ~/.claude plus any ~/.claude-* used with CLAUDE_CONFIG_DIR."""
    found = [DEFAULT_CLAUDE] + sorted(glob.glob(os.path.join(HOME, ".claude-*")))
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        found.append(os.path.abspath(os.path.expanduser(env)))
    keep, real = [], set()
    for d in found:  # one entry per real folder, keeping the name you use for it
        if os.path.realpath(d) not in real:
            real.add(os.path.realpath(d))
            keep.append(d)
    keep = [d for d in keep if os.path.isdir(d) and (os.path.exists(os.path.join(d, "settings.json")) or os.path.isdir(os.path.join(d, "projects")))]
    return keep or [DEFAULT_CLAUDE]


def scan(folder, config=None):
    """Scan a folder as a session would see it with this config folder (default ~/.claude)."""
    global CLAUDE, CLAUDE_JSON
    cfg = os.path.abspath(os.path.expanduser(config)) if config else DEFAULT_CLAUDE
    if os.path.realpath(cfg) == os.path.realpath(DEFAULT_CLAUDE):
        cfg = DEFAULT_CLAUDE  # ~/.claude may itself be a symlink
    # ~/.claude keeps its global config beside it in ~/.claude.json; a CLAUDE_CONFIG_DIR folder keeps it inside.
    cj = os.path.join(HOME, ".claude.json") if cfg == DEFAULT_CLAUDE else os.path.join(cfg, ".claude.json")
    with SCAN_LOCK:  # the server scans on several threads and the config is module state
        CLAUDE, CLAUDE_JSON = cfg, cj
        try:
            sc = _scan(folder)
        finally:
            CLAUDE, CLAUDE_JSON = DEFAULT_CLAUDE, os.path.join(HOME, ".claude.json")
    sc["config"] = tilde(cfg)
    sc["configs"] = [tilde(d) for d in config_dirs()]
    sc["layers"] = [dict(l, label=f"User ({tilde(cfg)})") if l["id"] == "user" else l for l in sc["layers"]]
    return sc


def _scan(folder):
    folder = os.path.realpath(os.path.expanduser(folder))
    sources, zero, not_loaded, notes = [], [], [], []
    pdir = project_dir(folder)
    rt, rt_note = read_runtime(pdir)
    if rt_note:
        notes.append(rt_note)
    plugin_roots = {}

    def file_of(origin):
        """The file or folder behind an origin label, for the page's open-in-editor action."""
        o = re.sub(r"^symlinks into ", "", origin).split(" › ")[0]
        if o.startswith("plugin "):
            o = plugin_roots.get(o[7:]) or ""
        o = os.path.expanduser(o)
        return tilde(o) if o and os.path.exists(o) else None

    def add(layer, kind, label, origin, text="", tokens=None, items=None, **kw):
        sid, n = slug(f"{layer}-{kind}-{origin}"), 2
        while any(x["id"] == sid for x in sources):  # two hooks with the same command, say
            sid, n = slug(f"{layer}-{kind}-{origin}") + f"-{n}", n + 1
        s = {"id": sid, "layer": layer, "kind": kind, "label": label, "origin": tilde(origin),
             "tokens": tok(text) if tokens is None else tokens}
        if text:
            s["excerpt"] = excerpt(text)
            s["text"] = text
        if items:
            s["items"] = items
            s["count"] = len(items)
        s.update(kw)
        path = file_of(origin)
        if path:
            s["path"] = path
        sources.append(s)
        return s

    # Instructions: managed, user, ancestors, imports, rules.
    seen = set()
    inst_files = []
    cands = [("org", os.path.join(MANAGED, "CLAUDE.md")), ("user", os.path.join(CLAUDE, "CLAUDE.md"))]
    cands += [("user", p) for p in sorted(glob.glob(os.path.join(CLAUDE, "rules", "**", "*.md"), recursive=True))]
    for d in reversed(ancestors(folder)):
        for rel in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md")):
            cands.append(("project", os.path.join(d, rel)))
    cands += [("project", p) for p in sorted(glob.glob(os.path.join(folder, ".claude", "rules", "**", "*.md"), recursive=True))]
    for layer, p in cands:
        rp = os.path.realpath(p)
        if rp in seen or not os.path.isfile(p):
            continue
        seen.add(rp)
        t = read(p) or ""
        inst_files.append(rp)
        add(layer, "instructions", tilde(p), p, t)
        for ip, it, by in imports_of(p, t, seen):
            inst_files.append(os.path.realpath(ip))
            add(layer, "instructions", tilde(ip) + " (imported)", ip, it, imported_by=tilde(by))
    for d in ancestors(folder):
        a = os.path.join(d, "AGENTS.md")
        if os.path.isfile(a) and os.path.realpath(a) not in seen:
            not_loaded.append({"label": tilde(a), "origin": tilde(d), "detail": "Claude Code doesn't read AGENTS.md unless a CLAUDE.md imports it with @AGENTS.md."})
    for p in sorted(glob.glob(os.path.join(folder, "*", "CLAUDE.md")) + glob.glob(os.path.join(folder, "*", "*", "CLAUDE.md"))):
        if "node_modules" not in p and os.path.realpath(p) not in seen:
            not_loaded.append({"label": tilde(p), "origin": "subfolder", "detail": "Loads only when Claude reads files in that folder."})

    # Auto memory.
    mem_dir = os.path.join(pdir, "memory")
    mem_index = os.path.join(mem_dir, "MEMORY.md")
    mem_files = sorted(f for f in glob.glob(os.path.join(mem_dir, "*.md")) if not f.endswith("MEMORY.md"))
    if os.path.isfile(mem_index):
        t = "\n".join((read(mem_index) or "").splitlines()[:200])
        items = [{"name": os.path.basename(f)[:-3], "tokens": tok(read(f)), "note": "loads when relevant"} for f in mem_files]
        add("user", "memory", "Auto memory", mem_index, t, items=items or None)
    else:
        zero.append({"label": "Auto memory: empty", "origin": tilde(mem_dir), "detail": "No MEMORY.md for this folder yet."})

    # Settings, plugins, hooks.
    layers = settings_layers(folder)
    enabled = merged_enabled_plugins(layers)
    active, inactive = plugins_for(folder, enabled)
    parts = {p["name"]: plugin_parts(p) for p in active}
    plugin_roots.update({p["name"]: p["root"] for p in active if p["root"]})
    for layer, p, d in layers:
        hooks = list(iter_hooks(d.get("hooks")))
        if hooks:
            zero.append({"label": f"{len(hooks)} hook{'s' * (len(hooks) != 1)}", "origin": tilde(p) + " › hooks",
                         "detail": "; ".join(f"{e}{'(' + m + ')' if m else ''} → {h.get('command', h.get('type'))}" for e, m, h in hooks)})
        perms = d.get("permissions") or {}
        n = sum(len(perms.get(k) or []) for k in ("allow", "deny", "ask"))
        if n:
            zero.append({"label": f"{n} permission rules", "origin": tilde(p) + " › permissions", "detail": ", ".join(f"{k}: {len(perms.get(k) or [])}" for k in ("allow", "deny", "ask") if perms.get(k))})
        if isinstance(d.get("statusLine"), dict):
            zero.append({"label": "Status line", "origin": tilde(p) + " › statusLine", "detail": str(d["statusLine"].get("command", ""))})
    for name, pp in parts.items():
        other = [(e, m, h) for e, m, h in pp["hooks"] if e != "SessionStart"]
        if other:
            zero.append({"label": f"{name}: {len(other)} hooks", "origin": f"plugin {name}", "detail": ", ".join(sorted({e for e, _, _ in other}))})
    for p in inactive:
        not_loaded.append({"label": p["full"], "origin": tilde(p["root"] or os.path.join(CLAUDE, "plugins")), "detail": p["why"].capitalize() + "."})
    for d in ancestors(os.path.dirname(folder)):
        for f in ("settings.json", "settings.local.json"):
            p = os.path.join(d, ".claude", f)
            if os.path.isfile(p) and os.path.dirname(os.path.dirname(p)) != HOME:
                not_loaded.append({"label": tilde(p), "origin": "parent folder", "detail": "Project settings are read only from the folder a session starts in."})
    if os.path.isfile(os.path.join(CLAUDE, "mcp.json")):
        names = list(((load_json(os.path.join(CLAUDE, "mcp.json")) or {}).get("mcpServers") or {}).keys())
        not_loaded.append({"label": tilde(os.path.join(CLAUDE, "mcp.json")), "origin": tilde(CLAUDE), "detail": f"Not a file Claude Code reads. Defines: {', '.join(names) or 'nothing'}. User servers belong in ~/.claude.json (claude mcp add).", "dead": True, "servers": names})

    # SessionStart hook output (runtime only).
    for h in (rt or {}).get("hooks", []):
        owner = next((n for n, pp in parts.items() for e, _, hk in pp["hooks"]
                      if e == "SessionStart" and h["command"] in (hk.get("command"), hk.get("statusMessage"))), None)
        if owner:
            add("plugin", "instructions", f"{owner} · SessionStart hook", f"plugin {owner} › SessionStart hook", h["content"])
        else:
            add("user", "instructions", "SessionStart hook", f"settings › SessionStart › {h['command'][:60]}", h["content"])

    # Skills.
    user_sk = skill_dirs(os.path.join(CLAUDE, "skills"))
    proj_sk = skill_dirs(os.path.join(folder, ".claude", "skills"))
    by_name_user = {e.get("name", k): e for k, e in user_sk.items()}
    by_name_proj = {e.get("name", k): e for k, e in proj_sk.items()}
    synced = {}
    for man in glob.glob(os.path.join(CLAUDE, "skills", "synced", "*", "*", "SKILL.md")):
        fm = frontmatter(read(man))
        synced[fm.get("name") or os.path.basename(os.path.dirname(man))] = man
    listing = parse_listing(rt["skills"]) if rt and rt.get("skills") is not None else None
    groups = {}  # (layer, label, origin) -> items

    def put(layer, label, origin, item):
        groups.setdefault((layer, label, origin), []).append(item)

    def user_group(e):
        if e.get("repo"):
            return f"{tilde(CLAUDE)}/skills ← {os.path.basename(e['repo'])}", f"symlinks into {tilde(e['repo'])}"
        return f"{tilde(CLAUDE)}/skills (local)", os.path.join(CLAUDE, "skills")

    if listing is not None:
        for name, line in listing.items():
            item = {"name": name, "tokens": tok(line), "desc": line.split(": ", 1)[1] if ": " in line else ""}
            if ":" in name:
                pre, base = name.split(":", 1)
                if pre == "anthropic-skills":
                    put("account", "anthropic-skills (synced)", os.path.join(CLAUDE, "skills", "synced"), item)
                else:
                    put("plugin", f"{pre} skills", f"plugin {pre}", item)
            elif name in by_name_proj:
                put("project", ".claude/skills", os.path.join(folder, ".claude", "skills"), {**item, "path": tilde(by_name_proj[name]["dir"])})
            elif name in by_name_user:
                label, origin = user_group(by_name_user[name])
                put("user", label, origin, {**item, "path": tilde(by_name_user[name]["dir"])})
            else:
                put("builtin", "Built-in skills", "Claude Code", item)
        visible = set(listing)
    else:
        if not rt_note:  # logs that exist but can't be read have their own note
            notes.append("No session log for this folder yet, so hook output, MCP instructions, built-in skills and the system prompt are missing. Start one session here and scan again.")
        for name, e in by_name_proj.items():
            if e.get("has_md") and not e.get("user_only"):
                line = f"- {name}: {e.get('description', '')}"
                put("project", ".claude/skills", os.path.join(folder, ".claude", "skills"), {"name": name, "tokens": tok(line), "desc": e.get("description", ""), "path": tilde(e["dir"])})
        for name, e in by_name_user.items():
            if e.get("has_md") and not e.get("user_only"):
                label, origin = user_group(e)
                line = f"- {name}: {e.get('description', '')}"
                put("user", label, origin, {"name": name, "tokens": tok(line), "desc": e.get("description", ""), "path": tilde(e["dir"])})
        for pn, pp in parts.items():
            for sn, fm in pp["skills"].items():
                if fm.get("disable-model-invocation", "").lower() != "true":
                    line = f"- {pn}:{sn}: {fm.get('description', '')}"
                    put("plugin", f"{pn} skills", f"plugin {pn}", {"name": f"{pn}:{sn}", "tokens": tok(line), "desc": fm.get("description", "")})
        for sn in synced:
            put("account", "anthropic-skills (synced)", os.path.join(CLAUDE, "skills", "synced"), {"name": "anthropic-skills:" + sn, "tokens": 25, "desc": ""})
        visible = {i["name"] for v in groups.values() for i in v}
    for (layer, label, origin), items in groups.items():
        add(layer, "skills", label, origin, tokens=sum(i["tokens"] for i in items), items=sorted(items, key=lambda i: i["name"]))
    user_only = [{"name": e.get("name", k), "tokens": 0, "note": "user-only (/" + e.get("name", k) + ")", "path": tilde(e["dir"])}
                 for k, e in list(user_sk.items()) + list(proj_sk.items()) if e.get("user_only")]
    if user_only:
        zero.append({"label": f"{len(user_only)} user-only skills", "origin": "disable-model-invocation: true",
                     "detail": "Slash commands only; Claude never sees their descriptions: " + ", ".join(i["name"] for i in user_only)})
    for k, e in list(user_sk.items()) + list(proj_sk.items()):
        if e["broken"]:
            not_loaded.append({"label": f"skill {k}", "origin": tilde(e["dir"]), "detail": f"Broken symlink to {tilde(e['real'])}.", "dead": True})
        elif not e["has_md"]:
            not_loaded.append({"label": f"skill {k}", "origin": tilde(e["dir"]), "detail": "No SKILL.md, so it never loads.", "dead": True})
    linked = {os.path.realpath(e["dir"]) for e in user_sk.values()}
    configs = {os.path.realpath(d) for d in config_dirs()}
    for repo in sorted({e["repo"] for e in user_sk.values() if e.get("repo")}):
        if os.path.realpath(repo) in configs:
            continue  # links into another config folder's skills: those load there, they aren't orphans
        for sk in sorted(glob.glob(os.path.join(repo, "skills", "*", "SKILL.md"))):
            if os.path.realpath(os.path.dirname(sk)) not in linked:
                not_loaded.append({"label": f"skill {os.path.basename(os.path.dirname(sk))}", "origin": tilde(os.path.dirname(sk)),
                                   "detail": f"In {os.path.basename(repo)}/skills but not linked into {tilde(CLAUDE)}/skills, so it doesn't load here.", "dead": True})

    # Agents (runtime listing).
    if rt and rt["agents"]:
        user_agents = {os.path.basename(p)[:-3] for p in glob.glob(os.path.join(CLAUDE, "agents", "*.md"))}
        proj_agents = {os.path.basename(p)[:-3] for p in glob.glob(os.path.join(folder, ".claude", "agents", "*.md"))}
        ag = {}
        for line in rt["agents"]:
            name = line.lstrip("- ").split(":", 1)[0].strip()
            key = ("project", ".claude/agents", os.path.join(folder, ".claude", "agents")) if name in proj_agents else \
                  ("user", f"{tilde(CLAUDE)}/agents", os.path.join(CLAUDE, "agents")) if name in user_agents else \
                  ("builtin", "Built-in agents", "Claude Code")
            ag.setdefault(key, []).append({"name": name, "tokens": tok(line), "desc": line.split(": ", 1)[-1]})
        for (layer, label, origin), items in ag.items():
            add(layer, "agents", label, origin, tokens=sum(i["tokens"] for i in items), items=items)

    # MCP servers.
    def servers_in(d, *path):
        for k in path:
            d = d.get(k) if isinstance(d, dict) else None
        return set(d) if isinstance(d, dict) else set()
    claude_json = load_json(CLAUDE_JSON)
    user_mcp = servers_in(claude_json, "mcpServers")
    local_mcp = servers_in(claude_json, "projects", folder, "mcpServers")
    proj_mcp = servers_in(load_json(os.path.join(folder, ".mcp.json")), "mcpServers")
    norm = lambda s: re.sub(r"[^A-Za-z0-9-]", "_", s)
    # A server known only from tool names (mcp__<name>__tool) has its name normalized, so match on that too.
    in_set = lambda server, names: server in names or norm(server) in {norm(x) for x in names}

    def classify(server):
        if server.startswith("plugin:") or server.startswith("plugin_"):
            pn = re.split(r"[:_]", server, 2)[1]
            return "plugin", f"{pn} · MCP", f"plugin {pn}"
        if in_set(server, user_mcp):
            return "user", f"{server} (MCP)", CLAUDE_JSON + " › mcpServers"
        if in_set(server, local_mcp):
            return "user", f"{server} (MCP, local)", CLAUDE_JSON + f" › projects › {tilde(folder)}"
        if in_set(server, proj_mcp):
            return "project", f"{server} (MCP)", os.path.join(folder, ".mcp.json")
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", server):
            return "account", "Connector", "claude.ai › Connectors"
        return "builtin", "App-provided tools", "Claude Code desktop app"

    if rt:
        servers = {}
        for name, body in rt["mcp"].items():
            servers.setdefault(norm(name), {"name": name, "body": body, "tools": []})
        harness_tools = []
        for t in rt["tools"]:
            m = re.match(r"mcp__(.+?)__(.+)", t)
            if m:
                servers.setdefault(m.group(1), {"name": m.group(1), "body": "", "tools": []})["tools"].append(m.group(2))
            else:
                harness_tools.append(t)
        app = {"tokens": 0, "items": []}
        for key, s in servers.items():
            layer, label, origin = classify(s["name"])
            tokens = tok(s["body"]) + tok(" ".join(s["tools"])) + 3 * len(s["tools"])
            if layer == "builtin":
                app["tokens"] += tokens
                app["items"].append({"name": s["name"], "tokens": tokens, "note": f"{len(s['tools'])} tools"})
                continue
            if label == "Connector":
                label = "Connector: " + connector_name(s)
            add(layer, "mcp", label, f"{origin} › {s['name']}", s["body"], tokens=tokens,
                items=[{"name": t, "tokens": 0} for t in s["tools"]] or None, server=s["name"])
        if app["items"]:
            add("builtin", "mcp", "App-provided MCP tools", "Claude Code desktop app", tokens=app["tokens"], items=app["items"])
        add("builtin", "harness", "Claude Code system prompt", "Claude Code", tokens=round(rt["system"] / 4) + tok(" ".join(harness_tools)))
        for s in set(rt["failed_mcp"]) | set(rt["auth_mcp"]):
            not_loaded.append({"label": f"MCP {s}", "origin": "session log", "detail": "Failed to connect or needs authentication in the last session."})
        # Drift: instruction files the last session saw that aren't there now, or the reverse.
        then = {os.path.realpath(i["path"]) for i in rt["instructions"] if i.get("path") and i.get("type") != "AutoMem"}
        now = set(inst_files)
        # A folder that was moved or renamed after the last session: its files show up as gone at the old path and new at
        # the new one. Say that once instead of once per file.
        old = os.path.realpath(rt["cwd"]) if rt["cwd"] and not os.path.exists(rt["cwd"]) else None
        if old and old != folder:
            moved = {p for p in then - now if p.startswith(old + os.sep) and folder + p[len(old):] in now}
            if moved:
                d = datetime.date.fromisoformat(rt["date"][:10]) if rt["date"] else None
                notes.append(f"Your last session here ({f'{d:%b} {d.day}, {d.year}' if d else 'date unknown'}) ran before this folder moved from "
                             f"{tilde(old)}. Start a Claude Code session here to refresh what's shown.")
                then -= moved
                now -= {folder + p[len(old):] for p in moved}
        for p in sorted(then - now):
            notes.append(f"The last session loaded {tilde(p)}, which no longer resolves for this folder.")
        for p in sorted(now - then):
            notes.append(f"{tilde(p)} is new since the last session; it will load next time.")
    else:
        for server in sorted(user_mcp | local_mcp | proj_mcp):
            layer, label, origin = classify(server)
            add(layer, "mcp", label, f"{origin} › {server}", tokens=300, estimated=True, server=server)  # shortcut: a guess per server, until a session log has the real size
        for pn, pp in parts.items():
            for server in sorted(pp["mcp"]):
                add("plugin", "mcp", f"{pn} · MCP", f"plugin {pn} › {server}", tokens=300, estimated=True, server=server)

    run = {"log": tilde(rt["path"]), "date": rt["date"], "claudeCode": rt["version"], "records": rt["seen"]} if rt else None
    scan_ = {"folder": tilde(folder), "wince": VERSION, "scannedAt": datetime.date.today().isoformat(), "runtime": run,
             "layers": LAYERS, "kinds": KINDS, "sources": sources, "zeroToken": zero, "notLoaded": not_loaded, "notes": notes,
             "blind": ["Organization instructions set on claude.ai (not stored on disk or in session logs).",
                       "Full tool schemas Claude Code sends; only deferred tool names are logged.",
                       "Tokens are estimates (characters ÷ 4)."]}
    scan_["findings"] = find(scan_)
    scan_["instrHash"] = instr_hash(sources)
    return scan_


# ---------- deterministic findings ----------

def norm_phrase(p):
    p = re.sub(r"\b[A-Z]{2,}-(?:\d+|X+)\b", "KEY", p)
    return re.sub(r"[^a-z0-9 ]+", "", p.lower().replace("key", "KEY")).strip()


def find(sc):
    out = []
    src = {s["id"]: s for s in sc["sources"]}
    skills = [(s, i) for s in sc["sources"] if s["kind"] == "skills" for i in s.get("items", [])]

    def fid(*parts):
        return slug("-".join(parts))[:80]

    # Overlapping trigger phrases.
    phrase_map = {}
    for s, i in skills:
        for p in re.findall(r"[\"“]([^\"”]{4,60})[\"”]", i.get("desc", "")):
            n = norm_phrase(p)
            if len(n.split()) >= 2:
                phrase_map.setdefault(n, {})[i["name"]] = (s, i, p)
    merged = {}
    for n, hits in phrase_map.items():
        if len(hits) >= 2:
            key = tuple(sorted(hits))
            merged.setdefault(key, {"phrases": [], "hits": hits})["phrases"].append(n)
    for names, m in merged.items():
        hits = m["hits"]
        phrases = ", ".join(f"\"{p}\"" for p in m["phrases"][:3])
        repo_scoped = [hits[n][1] for n in names if "←" in hits[n][0]["label"]]
        options = []
        if repo_scoped:
            options.append({"title": "Scope the project-specific skill to its project", "recommended": True,
                            "how": f"{', '.join(i['name'] for i in repo_scoped)} is linked into {tilde(CLAUDE)}/skills from another repo. Link it into that project's .claude/skills instead, so it only competes there.",
                            "tradeoff": "Removes the overlap everywhere else; inside that project you may still want to tighten wording."})
        options.append({"title": "Tighten the descriptions", "recommended": not repo_scoped,
                        "how": "Give each skill a trigger the others don't have (a ticket-key prefix, a folder, a tool), and add \"not for X; use Y\" to the broader one.",
                        "tradeoff": "Keeps everything global; relies on wording."})
        options.append({"title": "Merge them", "how": "Fold the smaller skill into the other as a mode or flag.", "tradeoff": "One description to maintain; bigger skill."})
        out.append({"id": fid("overlap", *names), "type": "overlap", "title": f"{len(names)} skills trigger on {phrases}",
                    "sources": sorted({hits[n][0]["id"] for n in names}),
                    "body": f"{', '.join(names)} all list {phrases} as a trigger. Which one fires depends on how well each description matches.",
                    "detail": {"pieces": [{"source": hits[n][0]["id"], "path": hits[n][1].get("path", hits[n][0]["origin"]) + f" › {n}", "quote": hits[n][1]["desc"][:400]} for n in names],
                               "ranking": "None is ranked. Claude reads every description and picks the best match.",
                               "effect": "The same request can start a different skill depending on phrasing, and you can't tell which from the outside.",
                               "options": options}})

    # Same skill name from two places.
    base = {}
    for s, i in skills:
        base.setdefault(i["name"].split(":")[-1], []).append((s, i))
    for b, hits in base.items():
        descs = [set(re.findall(r"[a-z]{3,}", i.get("desc", "").lower())) for _, i in hits]
        alike = any(not d for d in descs) or len(set.intersection(*descs)) / max(1, len(set.union(*descs))) >= 0.4
        if len({s["id"] for s, _ in hits}) >= 2 and alike:
            out.append({"id": fid("dup", b), "type": "redundant", "title": f"\"{b}\" is loaded {len(hits)} times",
                        "sources": sorted({s["id"] for s, _ in hits}),
                        "body": "Same skill name from " + " and ".join(f"{s['label']} ({i['name']})" for s, i in hits) + ".",
                        "detail": {"pieces": [{"source": s["id"], "path": s["origin"], "quote": i.get("desc", "")[:300] or "(no description)"} for s, i in hits],
                                   "ranking": "Both are offered; Claude may pick either.", "effect": "Two copies can drift apart, and both cost tokens every turn.",
                                   "options": [{"title": "Disable one copy", "recommended": True, "how": "Keep the one that follows you to other machines (account or plugin) and remove or disable the other.", "tradeoff": "Check they are really the same version first."}]}})

    # Skills that load everywhere because other folders link them in.
    linked = [x for x in sc["sources"] if x["kind"] == "skills" and "←" in x["label"]]
    if linked:
        n = sum(x["count"] for x in linked)
        out.append({"id": "provenance-linked-skills", "type": "provenance", "title": f"{n} of your user skills live in {len(linked)} other folders",
                    "sources": [x["id"] for x in linked],
                    "body": f"They're symlinked into {tilde(CLAUDE)}/skills, so they load in every session on this machine, wherever their repo is. " +
                            "; ".join(f"{x['origin'].replace('symlinks into ', '')}: {x['count']}" for x in linked) + ".",
                    "detail": {"pieces": [{"source": x["id"], "path": x["origin"], "quote": ", ".join(i["name"] for i in x["items"])} for x in linked],
                               "ranking": "User skills load in every session on this machine.",
                               "effect": "Skills built for one project compete with your general ones in unrelated folders and cost tokens there. Often fine for your own tools; worth a look for client or project repos.",
                               "options": [{"title": "Link project skills into their project instead", "recommended": True, "how": "For a repo whose skills only make sense inside it, move its links from " + tilde(CLAUDE) + "/skills to <repo>/.claude/skills (if an installer made them, point it there).", "tradeoff": "They stop loading in other folders."},
                                           {"title": "Keep them global", "how": "Leave the links in place for repos you really use in every folder.", "tradeoff": "Tokens and possible overlap stay."}]}})

    # Same connector connected more than once.
    conns = {}
    for x in sc["sources"]:
        if x["layer"] == "account" and x["kind"] == "mcp":
            conns.setdefault(x["label"], []).append(x)
    for label, xs in conns.items():
        if len(xs) >= 2:
            name = label.replace("Connector: ", "")
            out.append({"id": fid("conn", label), "type": "redundant", "title": f"{name} is connected {len(xs)} times",
                        "sources": [x["id"] for x in xs], "body": f"{len(xs)} separate {name} connectors load their instructions and tools into every session.",
                        "detail": {"pieces": [{"source": x["id"], "path": x["origin"], "quote": (x.get("excerpt") or "")[:240] or f"{x.get('count', 0)} tools"} for x in xs],
                                   "ranking": "All are offered; Claude picks a tool from whichever it finds first.",
                                   "effect": f"About {sum(x['tokens'] for x in xs):,} tokens a turn for one service, and duplicate tools to choose between.",
                                   "options": [{"title": "Keep one, disconnect the rest", "recommended": True, "how": "claude.ai › Settings › Connectors (https://claude.ai/settings/connectors): keep the current official connector and remove the others.", "tradeoff": "Check which one your workflows use first."}]}})

    # Dead config.
    for z in sc["notLoaded"]:
        if z.get("dead"):
            out.append({"id": fid("dead", z["label"]), "type": "dead", "title": z["label"], "sources": [], "body": z["detail"],
                        "detail": {"pieces": [{"source": None, "path": z["origin"], "quote": z["detail"]}],
                                   "ranking": "Never loads.", "effect": "Whatever it was meant to do isn't happening.",
                                   "options": [{"title": "Fix or remove it", "recommended": True, "how": "Move it to a location Claude Code reads, or delete it if it's obsolete. Look before deleting.", "tradeoff": ""}]}})

    # Heavy sources.
    mine = [s for s in sc["sources"] if s["kind"] != "harness"]
    total = sum(s["tokens"] for s in mine) or 1
    for s in mine:
        if s["tokens"] >= 2000 and s["tokens"] / total >= 0.12:
            out.append({"id": fid("heavy", s["id"]), "type": "heavy", "title": f"{s['label']} is {round(100 * s['tokens'] / total)}% of your context",
                        "sources": [s["id"]], "body": f"About {s['tokens']:,} tokens every turn, out of {total:,} that come from sources you control.",
                        "detail": {"pieces": [{"source": s["id"], "path": s["origin"], "quote": (s.get("excerpt") or "")[:300] or f"{s.get('count', 0)} items"}],
                                   "ranking": "Size alone isn't a problem; unused size is.", "effect": "Less room for your conversation, and more for the model to weigh.",
                                   "options": [{"title": "Trim or scope it", "recommended": True, "how": "Disable what you don't use here, or move it to the projects that need it.", "tradeoff": ""}]}})
    return out


# ---------- semantic analysis (optional, uses claude -p) ----------

ANALYZE_PROMPT = """You are auditing the instructions a Claude Code session receives in one folder.
Below are the instruction sources, each with an id. Find real problems only:
- conflict: two sources ask for incompatible behavior in the same situation
- redundant: two sources say the same thing
- overlap: two sources cover the same ground in different words, so it's unclear which governs
Skip anything minor. Quote exactly: every quote must be copied verbatim from its source (a contiguous substring).

Reply with JSON only, no prose: {"findings": [{"type": "conflict|redundant|overlap", "title": "...", "body": "one or two sentences",
"pieces": [{"source": "<id>", "quote": "..."}], "ranking": "which one wins today, if any", "effect": "what the user would notice",
"options": [{"title": "...", "how": "the concrete edit", "tradeoff": "...", "recommended": true|false}]}]}

SOURCES
"""


def analyze(sc):
    srcs = [s for s in sc["sources"] if s["kind"] in ("instructions", "memory") and s.get("text")]
    if len(srcs) < 1:
        return []
    body = "".join(f"\n=== id: {s['id']} | {s['label']} ===\n{s['text'][:12000]}\n" for s in srcs)
    try:
        r = subprocess.run(["claude", "-p", ANALYZE_PROMPT + body, "--output-format", "json"], capture_output=True, text=True,
                           timeout=600, cwd=tempfile.gettempdir())
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  analysis skipped: {e}", file=sys.stderr)
        return None
    try:
        text = json.loads(r.stdout).get("result", "")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"  analysis output unreadable: {e}", file=sys.stderr)
        return None
    by_id = {s["id"]: s for s in srcs}
    out = []
    for i, f in enumerate(data.get("findings", [])):
        pieces = [p for p in f.get("pieces", []) if p.get("source") in by_id and p.get("quote") and p["quote"] in by_id[p["source"]]["text"]]
        if not pieces or len(pieces) < len(f.get("pieces", [])):
            continue  # a quote didn't check out against its source: drop the finding rather than show a made-up one
        for p in pieces:
            p["path"] = by_id[p["source"]]["origin"]
        # An id from the content, so Compare matches the same finding across two folders and never two different ones.
        aid = "ai-" + hashlib.sha1((f.get("title", "") + "".join(p["quote"] for p in pieces)).encode()).hexdigest()[:12]
        out.append({"id": aid, "type": f.get("type", "conflict"), "title": f.get("title", ""), "body": f.get("body", ""),
                    "sources": sorted({p["source"] for p in pieces}), "analyzed": True,
                    "detail": {"pieces": pieces, "ranking": f.get("ranking", ""), "effect": f.get("effect", ""), "options": f.get("options", [])}})
    return out


# ---------- saved state ----------
# Scans run live. Only the recent-folders list and conflict checks are saved, and a check keeps where each quote sits
# (offset, length, hash) rather than the quote: the page reads the text from the file, so a deleted line leaves wince too.

STATE_LOCK = threading.Lock()
RECENT_MAX = 30


def load_state(path=None):
    """The saved state; empty when there is none. Raises ValueError when the file can't be read."""
    path = path or STATE
    t = read(path)
    if not t or not t.strip():
        return {"recent": [], "checks": {}}
    try:
        st = json.loads(t)
        assert isinstance(st, dict)
    except (json.JSONDecodeError, AssertionError) as e:
        raise ValueError(f"{tilde(path)} isn't a wince state file; move it aside and start again") from e
    st.setdefault("recent", []); st.setdefault("checks", {})
    return st


def update_state(change, path=None):
    """Read, change and write the state under a lock, atomically and readable only by you."""
    path = path or STATE
    with STATE_LOCK:  # the server writes from several threads; read-modify-write must not interleave
        st = load_state(path)
        change(st)
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.chmod(tmp, 0o600)  # O_CREAT's mode doesn't apply to a leftover tmp file
        os.replace(tmp, path)  # readers never see a half-written file
        return st


def scan_key(sc):
    return f"{sc['folder']}|{sc['config']}"


def remember(sc, path=None):
    """Put this folder and config folder first in the recent list."""
    entry = {"folder": sc["folder"], "config": sc["config"]}
    update_state(lambda st: st.__setitem__("recent", [entry] + [r for r in st["recent"] if r != entry][:RECENT_MAX - 1]), path)


def quote_hash(q):
    return hashlib.sha1(q.encode()).hexdigest()[:16]


def locate(findings, sources):
    """Findings to save: each quote becomes the file it's in, its offset, length and a hash. No text is kept."""
    texts = {s["id"]: s for s in sources}
    out = []
    for f in findings:
        f = json.loads(json.dumps(f))
        for p in f["detail"]["pieces"]:
            src = texts[p["source"]]
            q = p.pop("quote")
            p.update(path=src["origin"], at=src["text"].find(q), len=len(q), sha=quote_hash(q))
        out.append(f)
    return out


def resolve(findings, sources):
    """Saved findings for the page: quotes read back from the live texts; a piece whose text moved or changed says so."""
    by_origin = {s["origin"]: s for s in sources if s.get("text")}
    out = []
    for f in findings:
        f = json.loads(json.dumps(f))
        for p in f["detail"]["pieces"]:
            src = by_origin.get(p["path"])
            q = src["text"][p["at"]:p["at"] + p["len"]] if src and p.get("at", -1) >= 0 else ""
            if src and quote_hash(q) != p["sha"]:  # edited above the quote: look for it by length and hash
                q = next((src["text"][i:i + p["len"]] for i in range(len(src["text"]) - p["len"] + 1)
                          if quote_hash(src["text"][i:i + p["len"]]) == p["sha"]), "") if p["len"] <= 2000 else ""
            p["quote"], p["source"] = (q, src["id"]) if q else (None, src["id"] if src else None)
            for k in ("at", "len", "sha"):
                p.pop(k, None)
        f["sources"] = sorted({p["source"] for p in f["detail"]["pieces"] if p["source"]})
        out.append(f)
    return out


def with_check(sc, path=None):
    """Attach the saved conflict check, if any, to a live scan."""
    chk = load_state(path)["checks"].get(scan_key(sc))
    if chk:
        sc["findings"] = resolve(chk["findings"], sc["sources"]) + sc["findings"]
        sc["check"] = {"checkedAt": chk["checkedAt"], "stale": chk["instrHash"] != sc["instrHash"]}
    return sc


def run_check(sc, path=None):
    """Run the conflict check and save it. False if it failed, so an earlier check is kept rather than replaced by nothing."""
    found = analyze(sc)
    if found is None:
        return False
    chk = {"checkedAt": datetime.date.today().isoformat(), "instrHash": sc["instrHash"], "findings": locate(found, sc["sources"])}
    update_state(lambda st: st["checks"].__setitem__(scan_key(sc), chk), path)
    return True


def forget_check(sc, path=None):
    update_state(lambda st: st["checks"].pop(scan_key(sc), None), path)


# ---------- canary for Claude Code updates ----------
# Session logs and loading rules are Claude Code internals. When a folder's newest log comes from a new Claude Code
# version, compare it with what the previous version produced there, and warn about anything that stopped showing up.

RECORD_LABELS = {"hook_success": "hook output", "skill_listing": "skill listing", "mcp_instructions_delta": "MCP instructions",
                 "deferred_tools_delta": "tool names", "agent_listing_delta": "agent listing", "instructions": "instruction file list",
                 "prompt_snapshot": "system prompt"}


def fingerprint(sc):
    """What a scan saw: the log's record types, and how many sources each layer and kind produced."""
    counts = {}
    for s in sc["sources"]:
        if s["kind"] != "harness":
            counts[f"{s['layer']}/{s['kind']}"] = counts.get(f"{s['layer']}/{s['kind']}", 0) + 1
    return {"version": sc["runtime"]["claudeCode"], "records": sorted(set(sc["runtime"]["records"]) & set(RECORD_LABELS)), "counts": counts}


def canary(sc, path=None):
    """Add a note when something stopped showing up after a Claude Code update. Keeps warning until it's back."""
    if not (sc.get("runtime") and sc["runtime"].get("claudeCode") and "records" in sc["runtime"]):
        return sc
    now, key, note = fingerprint(sc), scan_key(sc), []

    def change(st):
        base = st.setdefault("baselines", {}).get(key)
        if base and base["version"] != now["version"]:
            lost = {"records": [r for r in base["records"] if r not in now["records"]],
                    "counts": [k for k, n in base["counts"].items() if n and not now["counts"].get(k)]}
            st["baselines"][key] = {**now, "since": base["version"], "lost": lost}
        elif base:
            lost = base.get("lost") or {"records": [], "counts": []}  # same version: keep warning about what's still missing
            lost = {"records": [r for r in lost["records"] if r not in now["records"]],
                    "counts": [k for k in lost["counts"] if not now["counts"].get(k)]}
            st["baselines"][key] = {**base, "lost": lost}
        else:
            st["baselines"][key] = now
        b = st["baselines"][key]
        if b.get("lost") and (b["lost"]["records"] or b["lost"]["counts"]):
            layer = {l["id"]: l["label"] for l in sc["layers"]}
            kind = {k["id"]: k["label"] for k in KINDS}
            gone = [RECORD_LABELS[r] for r in b["lost"]["records"]] + [
                f"{kind.get(k.split('/')[1], k)} from {layer.get(k.split('/')[0], k)}" for k in b["lost"]["counts"]]
            note.append(f"Since Claude Code {now['version']} (this folder last showed {b['since']}), wince no longer finds: "
                        f"{', '.join(gone)}. Claude Code may have changed how it logs or loads these, so wince could be missing "
                        f"them here. If you removed them yourself, ignore this.")
    update_state(change, path)
    sc["notes"] = sc["notes"] + note
    return sc


def note_id(text):
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def dismiss_note(key, nid, path=None):
    """Hide one note for this folder and config folder. A note with different wording (a new date, say) shows again."""
    def change(st):
        d = st.setdefault("dismissed", {}).setdefault(key, [])
        if nid not in d:
            d.append(nid)
            del d[:-100]
    update_state(change, path)


def for_page(sc, path=None):
    """A scan as the page gets it: full texts stay on the server, the page shows excerpts, and dismissed notes are left out."""
    hidden = set(load_state(path).get("dismissed", {}).get(scan_key(sc), []))
    notes = [{"id": note_id(n), "text": n} for n in sc["notes"] if note_id(n) not in hidden]
    return {**sc, "notes": notes, "sources": [{k: v for k, v in s.items() if k != "text"} for s in sc["sources"]]}


# Runs inside osascript itself, so it needs no permission to control another app; "activate" brings the dialog to the front.
PROMPT = "Scan which folder with wince?"
TK_PICK = ("import tkinter, tkinter.filedialog as f; r = tkinter.Tk(); r.withdraw(); r.attributes('-topmost', True); "
           f"print(f.askdirectory(title={PROMPT!r}) or '')")


def pick_folder():
    """A native folder picker. Returns the path, "" if cancelled, or None if this system has none."""
    if sys.platform == "darwin":
        cmds = [["osascript", "-e", "activate", "-e", f'POSIX path of (choose folder with prompt "{PROMPT}")']]
    else:
        cmds = [["zenity", "--file-selection", "--directory", f"--title={PROMPT}"], ["kdialog", "--getexistingdirectory", HOME, "--title", PROMPT]]
    cmds.append([sys.executable, "-c", TK_PICK])  # Windows, and Linux without zenity or kdialog
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if cmd[0] == sys.executable and r.returncode != 0:
            continue  # no tkinter, or no display
        return r.stdout.strip() if r.returncode == 0 else ""
    return None


def open_path(path):
    """Open a file in the default text editor, or a folder in the file manager."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: the only opener Windows has
    elif sys.platform == "darwin":
        subprocess.run(["open", path] if os.path.isdir(path) else ["open", "-t", path], check=True, timeout=20)
    else:
        subprocess.run(["xdg-open", path], check=True, timeout=20)


def make_server(port, state=None):
    """The app on localhost, plus a small JSON API the pages call. Every /api/ call needs the X-Wince header."""
    import functools
    import http.server
    import urllib.parse
    last = {}  # the newest live scan per folder and config folder, for /api/open

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def local(self):
            # A page on another site that rebinds its DNS to 127.0.0.1 still sends its own Host, so this stops it.
            port = self.server.server_address[1]
            if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return True
            self.send_error(403, "wince only answers on localhost")
            return False

        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def api(self):
            # The custom header forces a CORS preflight, which this server never answers, so other sites can't call the API.
            if self.headers.get("X-Wince") != "1":
                self.reply(403, {"error": "forbidden"})
                return False
            return True

        def live_scan(self, req, record=False):
            """Scan req's folder with req's config folder. Returns the scan, or None after replying with the error."""
            folder = os.path.expanduser(req.get("folder") or "")
            if not os.path.isdir(folder):
                return self.reply(400, {"error": f"not a folder: {req.get('folder')}"})
            config = req.get("config") or None
            if config and os.path.realpath(os.path.expanduser(config)) not in map(os.path.realpath, config_dirs()):
                return self.reply(400, {"error": f"not a Claude config folder: {config}"})
            try:
                sc = scan(folder, config)
                if record:
                    remember(sc, state)
                last[scan_key(sc)] = sc
                return sc
            except ValueError as e:  # an unreadable state file
                return self.reply(500, {"error": str(e)})
            except Exception as e:  # report it on the page instead of dropping the connection
                return self.reply(500, {"error": f"The scan failed: {e}"})

        def do_GET(self):
            if not self.local():
                return
            url = urllib.parse.urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/app/flow.html")
                return self.end_headers()
            if not url.path.startswith("/api/"):
                return super().do_GET()
            if not self.api():
                return
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                recent = load_state(state)["recent"]
            except ValueError as e:
                return self.reply(500, {"error": str(e)})
            if url.path == "/api/recent":
                return self.reply(200, {"recent": recent})
            if url.path != "/api/scan":
                return self.reply(404, {"error": "no such endpoint"})
            sc = self.live_scan(q, record=True)
            if sc:
                try:
                    canary(sc, state)
                except ValueError as e:
                    return self.reply(500, {"error": str(e)})
                self.reply(200, {"scan": for_page(with_check(sc, state), state), "recent": load_state(state)["recent"]})

        def do_POST(self):
            if not self.local() or not self.api():
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self.reply(400, {"error": "bad request"})
            if not isinstance(req, dict):
                return self.reply(400, {"error": "bad request"})
            if self.path == "/api/pick":
                folder = pick_folder()
                if folder is None:
                    return self.reply(501, {"error": "no folder picker on this system"})
                return self.reply(200, {"folder": tilde(folder.rstrip(os.sep) or folder)} if folder else {"canceled": True})
            if self.path == "/api/open":
                return self.open_source(req)
            if self.path == "/api/dismiss":
                if not (isinstance(req.get("folder"), str) and isinstance(req.get("note"), str)):
                    return self.reply(400, {"error": "bad request"})
                try:
                    dismiss_note(f"{req['folder']}|{req.get('config') or '~/.claude'}", req["note"], state)
                except ValueError as e:
                    return self.reply(500, {"error": str(e)})
                return self.reply(200, {"dismissed": req["note"]})
            if self.path not in ("/api/check", "/api/forget"):
                return self.reply(404, {"error": "no such endpoint"})
            sc = self.live_scan(req)
            if not sc:
                return
            try:
                if self.path == "/api/forget":
                    forget_check(sc, state)
                elif not run_check(sc, state):
                    return self.reply(502, {"error": "The conflict check didn't finish (is the claude command installed and signed in?). The earlier check, if any, is kept."})
            except ValueError as e:
                return self.reply(500, {"error": str(e)})
            self.reply(200, {"folder": sc["folder"], "config": sc["config"]})

        def open_source(self, req):
            # Only paths a scan recorded can be opened, never one the page sends.
            sc = last.get(f"{req.get('folder')}|{req.get('config') or '~/.claude'}")
            src = next((x for x in (sc or {}).get("sources", []) if x["id"] == req.get("source")), None)
            path = os.path.expanduser(src.get("path") or "") if src else ""
            if not path or not os.path.exists(path):
                return self.reply(404, {"error": "This piece has no file on disk to open."})
            try:
                open_path(path)
            except (OSError, subprocess.SubprocessError):
                return self.reply(501, {"error": f"Couldn't open {tilde(path)} on this system."})
            self.reply(200, {"opened": tilde(path)})

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=HERE))


def serve(port, browser=True):
    try:
        srv = make_server(port)
    except OSError as e:
        sys.exit(f"Port {port} is busy ({e.strerror}). wince may already be running: open http://localhost:{port}/ "
                 f"or start another copy with --port {port + 1}.")
    print(f"wince at http://localhost:{port}/ (Ctrl+C to stop)", file=sys.stderr)
    if browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, [f"http://localhost:{port}/"]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"wince {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan folders and print a summary (the page always scans live)")
    s.add_argument("folders", nargs="*", default=["."])
    s.add_argument("--config", help="Claude config folder to scan with, like CLAUDE_CONFIG_DIR (default ~/.claude)")
    s.add_argument("--analyze", action="store_true", help="also ask Claude (claude -p) to find conflicts, and save the result")
    v = sub.add_parser("serve", help="serve the app on localhost and open it in your browser")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        return serve(a.port, not a.no_browser)
    for folder in a.folders:
        if not os.path.isdir(os.path.expanduser(folder)):
            ap.error(f"not a folder: {folder}")
        sc = scan(folder, a.config)
        remember(sc)
        if a.analyze:
            print(f"analyzing {sc['folder']} with claude -p …", file=sys.stderr)
            if not run_check(sc):
                print("  conflict check failed; an earlier one, if any, is kept", file=sys.stderr)
        sc = canary(with_check(sc))
        for n in sc["notes"]:
            if n.startswith("Since Claude Code"):
                print(f"  note: {n}", file=sys.stderr)
        print(f"{sc['folder']}: {len(sc['sources'])} sources, {len(sc['findings'])} findings, "
              f"runtime from {(sc['runtime']['date'] or 'an unknown date')[:10] if sc['runtime'] else 'no session log'}")


if __name__ == "__main__":
    main()
