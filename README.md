# nodriver-mcp-server

**An undetected, anti-bot-resistant browser automation MCP server** — a drop-in, stealth alternative to [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp) for AI agents like **Claude**, **Claude Code**, **Cursor**, **Windsurf**, and any [Model Context Protocol](https://modelcontextprotocol.io) client. Powered by [nodriver](https://github.com/ultrafunkamsterdam/nodriver) so your agent can browse, scrape, and automate real Chrome **without tripping Cloudflare, hCaptcha, or WebDriver fingerprint detection**.

![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![MCP compatible](https://img.shields.io/badge/MCP-compatible-purple.svg)
![Tools: 47](https://img.shields.io/badge/tools-47-orange.svg)
![Stars](https://img.shields.io/github/stars/andresolbach/nodriver-mcp-server?style=social)

> **Keywords:** MCP server · browser automation · undetected chromedriver · anti-bot · Cloudflare bypass · web scraping · Claude · Cursor · nodriver · chrome-devtools-mcp alternative · Playwright/Puppeteer alternative · AI agent tools.

## Why?

`chrome-devtools-mcp` and most Playwright/Puppeteer-based servers drive Chrome through CDP/WebDriver in a way that leaves detectable fingerprints (`navigator.webdriver`, CDP artifacts). Anti-bot systems (Cloudflare, hCaptcha, DataDome, etc.) flag these instantly.

`nodriver` is the successor of `undetected-chromedriver`. It talks **directly to the CDP protocol** — no ChromeDriver binary, no Selenium/WebDriver markers — so automated sessions look like a real user. This server exposes that power through the **same tool surface as `chrome-devtools-mcp`** (42 tools), so your agent gets a familiar API with far better stealth.

## Features

- 🕵️ **Undetected by design** — `navigator.webdriver` is `undefined`, no CDP fingerprints.
- ☁️ **Built-in Cloudflare challenge solver** (`cf_verify`).
- 🧩 **42 tools** covering navigation, input, snapshots, screenshots, network + console inspection, device emulation, cookies/storage, sessions, and performance tracing.
- 📄 **Accessibility-tree snapshots** (`take_snapshot`) — searchable, LLM-friendly page text that's far smaller and faster than screenshots.
- 📱 **Device emulation** (Pixel 7, iPad) with correct UA / client hints.
- 💾 **Session save/restore** — persist logins across runs.
- 🧬 **Ephemeral by default, run many at once** — each session gets its own temp Chrome profile (auto-deleted), so Claude Desktop, Claude Code and VS Code can all drive nodriver **simultaneously without colliding**. Named **persistent profiles** are available on demand for reusable logins.
- ⚡ **One-command setup** for 15+ MCP clients.

## Installation

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended):

```bash
# Install as an isolated tool (won't touch your global Python environment)
uv tool install "nodriver-mcp @ git+https://github.com/andresolbach/nodriver-mcp-server.git@main"
```

> Uses upstream [`nodriver`](https://pypi.org/project/nodriver/) `>=0.50.3`, which contains the Chrome 146+ CDP fixes (`sameParty` removed from `Cookie`, `privateNetworkRequestPolicy` → `localNetworkAccessRequestPolicy`) — **verified working against Chrome 150**. `pip install` also works, but `uv tool install` keeps it isolated.

You'll also need a local installation of **Google Chrome** (auto-detected).

### Upgrade

```bash
uv tool upgrade nodriver-mcp
```

## One-command MCP client setup

```bash
# Interactive client selector (terminal TUI)
nodriver-mcp install

# Install to specific clients
nodriver-mcp install claude,cursor,kiro

# Uninstall
nodriver-mcp uninstall claude

# List all supported clients
nodriver-mcp --list-clients

# Print MCP config JSON (for manual setup)
nodriver-mcp --config

# Project-level config (writes to .cursor/mcp.json, .mcp.json, etc.)
nodriver-mcp install --scope project
```

**Supported clients:** Claude Desktop, Claude Code, Cursor, Windsurf, Codex, Gemini CLI, Copilot CLI, Kiro, VS Code, Cline, Roo Code, Amazon Q, Warp, Opencode, Trae.

> The Claude Code VS Code extension shares Claude Code's config (`~/.claude.json`), so installing to `claude-code` covers both the CLI and the extension.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NODRIVER_HEADLESS` | Headless mode (`true`/`false`) | `false` |
| `NODRIVER_USER_DATA_DIR` | Explicit persistent Chrome profile dir (overrides the default) | Ephemeral temp profile, auto-deleted per session |
| `NODRIVER_BROWSER_PATH` | Chrome executable path | Auto-detected |
| `NODRIVER_PROXY` | Proxy server address | None |

## Profiles & running multiple instances at once

By default every server instance launches Chrome with a **fresh temporary profile** that nodriver creates and deletes automatically. That means you can run nodriver from **Claude Desktop, Claude Code and the VS Code extension at the same time** — each gets its own isolated Chrome, and they never fight over a shared profile. No configuration, no detection logic, nothing to clean up.

When you want to **reuse a login across sessions**, create a named persistent profile and switch to it:

- `list_profiles` — list persistent profiles and show the active one
- `create_profile(name, activate=false)` — create a reusable profile
- `use_profile(name)` — switch to a persistent profile (`""`/`"temp"` returns to ephemeral)
- `use_temp_profile` — switch back to a fresh ephemeral profile
- `delete_profile(name)` — remove a persistent profile

Persistent profiles live under `~/.nodriver-mcp/profiles/<name>`. You can still force a fixed profile globally with the `NODRIVER_USER_DATA_DIR` env var.

## Tools (47)

Network collection is enabled automatically on each tab. Console collection is opt-in: call `enable_console_collection` when you want `list_console_messages` / `get_console_message` to start collecting events. This keeps `Runtime.enable()` disabled by default for sites that detect attached debuggers.

For mobile-only sites, pass `device` directly to `new_page(...)` or `navigate_page(...)` so the first real request already carries mobile signals.

| Category | Tools |
|----------|-------|
| **Input automation (10)** | `click` · `click_at` · `hover` · `fill` · `fill_form` · `type_text` · `press_key` · `drag` · `upload_file` · `handle_dialog` |
| **Navigation (7)** | `navigate_page` · `new_page` · `close_page` · `list_pages` · `select_page` · `wait_for` · `scroll_page` |
| **Snapshots & debugging (7)** | `take_screenshot` · `take_snapshot` · `evaluate_script` · `enable_console_collection` · `disable_console_collection` · `list_console_messages` · `get_console_message` |
| **Network monitoring (2)** | `list_network_requests` · `get_network_request` |
| **Device emulation (4)** | `emulate` · `emulate_device` · `reset_emulation` · `resize_page` |
| **Performance (3)** | `performance_start_trace` · `performance_stop_trace` · `take_memory_snapshot` |
| **Cookies & storage (4)** | `get_cookies` · `set_cookie` · `get_local_storage` · `set_local_storage` |
| **Session management (3)** | `save_session` · `load_session` · `list_sessions` |
| **Profile management (5)** | `list_profiles` · `create_profile` · `use_profile` · `use_temp_profile` · `delete_profile` |
| **Anti-detection helpers (2)** | `cf_verify` · `bypass_insecure_warning` |

## Comparison with chrome-devtools-mcp

| Feature | chrome-devtools-mcp | nodriver-mcp-server |
|---------|---------------------|---------------------|
| Browser backend | Puppeteer (ChromeDriver) | nodriver (direct CDP) |
| WebDriver fingerprint | ❌ Exposed | ✅ None |
| `navigator.webdriver` | ❌ `true` | ✅ `undefined` |
| Cloudflare bypass | ❌ | ✅ Built-in `cf_verify` |
| Install method | npx | uv tool install |
| Language | TypeScript / Node.js | Python |
| Tool coverage | 29 tools | 47 tools |

Tools not implemented: `performance_analyze_insight` (needs the DevTools frontend trace parser), `lighthouse_audit` (needs the Lighthouse Node API), `screencast_start/stop` (needs ffmpeg + Puppeteer), extension management (experimental).

## Changelog

See [CHANGES.md](CHANGES.md). Highlights of the latest release: migrated to upstream `nodriver 0.50.3` (Chrome 150 verified) and fixed several previously-broken tools — `fill`/`fill_form`, `evaluate_script` with element args, `select_page` tab switching, `press_key` modifier chords (Ctrl+A/C/V), network/console lookup indexing, and Windows installer crashes.

## Credits

Based on [`nodriver-mcp`](https://github.com/Saber-CC/nodriver-mcp) by **Saber-CC** (MIT). Browser backend by [`nodriver`](https://github.com/ultrafunkamsterdam/nodriver) (ultrafunkamsterdam). Tool surface mirrors [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp).

## License

[MIT](LICENSE)
