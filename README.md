# wince

Shows everything that reaches Claude Code in a session for one folder (CLAUDE.md files, memory, skills, plugins, hooks, MCP servers, connectors, the system prompt), where each piece comes from, and where pieces conflict or repeat.

## Install

Needs Python 3.9+ and [pipx](https://pipx.pypa.io/).

```bash
pipx install git+https://github.com/soliantconsulting/wince.git
```

## Use

```bash
wince serve
```

This opens http://localhost:8765/.

From the terminal: `wince scan FOLDER [--config DIR] [--analyze]`.

## How it works

Every page load scans the folder live: files on disk, plus the newest session log, which records what actually reached the model. Session logs are an internal Claude Code format, so after a Claude Code update wince warns about anything that stopped showing up.

It can't see claude.ai organization instructions or full tool schemas, and token counts are estimates.

## Privacy

The server listens on localhost only. Only Find conflicts sends text out, to `claude -p` on your account. wince saves just `~/.wince/state.json` (owner-only; `WINCE_HOME` moves it): recent folders and conflict checks, which store where each quote is rather than the quote. **Forget** deletes a check.

## Development

`python3 test_wince.py` runs the self-check. `pipx install -e .` runs your working copy.

## License

MIT, see [LICENSE](LICENSE). Bundled d3 and d3-sankey keep their own licenses ([vendor/LICENSES.md](wince/app/vendor/LICENSES.md)). The Soliant name and logo aren't covered.
