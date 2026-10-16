<p align="center">
  <img src="https://raw.githubusercontent.com/andresolbach/nodriver-mcp-server/main/assets/logo.png" alt="nodriver-mcp — browser-tab monogram" width="130" height="130">
</p>

# nodriver-mcp-server

<!-- mcp-name: io.github.andresolbach/nodriver-mcp-server -->

**Give your AI agent a full Chrome workspace.** Browse, fill forms, extract data and inspect the requests behind a page with **70 MCP tools**, powered by [nodriver](https://github.com/ultrafunkamsterdam/nodriver). Keep your logins, run isolated browsers side by side, and pick up where you left off with automatic tab restoration. One-command setup for **Claude, Claude Code, Cursor, Windsurf, Codex** and other [Model Context Protocol](https://modelcontextprotocol.io) clients.

[![PyPI](https://img.shields.io/pypi/v/nodriver-mcp.svg)](https://pypi.org/project/nodriver-mcp/)
[![CI](https://github.com/andresolbach/nodriver-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/andresolbach/nodriver-mcp-server/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![MCP compatible](https://img.shields.io/badge/MCP-compatible-purple.svg)
![Tools: 70](https://img.shields.io/badge/tools-70-orange.svg)
![Stars](https://img.shields.io/github/stars/andresolbach/nodriver-mcp-server?style=social)

> **Keywords:** MCP server · browser automation · undetected chromedriver · anti-bot · Cloudflare bypass · web scraping · Claude · Cursor · nodriver · chrome-devtools-mcp alternative · Playwright/Puppeteer alternative · AI agent tools.

## More browsing, less setup

- **Log in once, come back ready.** Persistent profiles keep cookies and logins between sessions.
- **Start your way.** Open a fresh tab, a set of favorite URLs, or the tabs you left open — configurable through MCP, per profile.
- **Give each task its own browser.** Run up to 12 isolated browser sessions without sharing tabs, cookies or the selected page.
- **Get the data behind the page.** Read accessibility snapshots, inspect API traffic and save response bodies for later search.
- **Automate real Chrome directly.** nodriver, the successor of `undetected-chromedriver`, uses CDP without Selenium or a ChromeDriver binary and is designed to reduce common automation signals.

Explore the [70-tool reference](https://github.com/andresolbach/nodriver-mcp-server/blob/main/docs/TOOLS.md), or get started with `uvx nodriver-mcp`.

## Does it actually pass? Here is the evidence

Claiming "undetected" is easy, so here is what the standard fingerprint suite reports when this server drives the page itself. Reproduce it in one line: point your agent at `https://bot.sannysoft.com` and take a snapshot.

<p align="center">
  <img src="https://raw.githubusercontent.com/andresolbach/nodriver-mcp-server/main/assets/proof/sannysoft.png" alt="bot.sannysoft.com results: all checks green" width="700">
</p>

| Check | Result |
|---|---|
| `bot.sannysoft.com` — Intoli + fingerprint suite | **58 checks, 0 failed** |
| `navigator.webdriver` | `false` — a boolean on `Navigator.prototype`, not an own property, exactly as in a normal browser |
| `window.chrome` | present |
| `navigator.plugins` | 5 |
| `navigator.languages` | 4 entries |
| User agent | no `Headless` token |

Measured on Chrome 151 / Windows 11, headful, with the default profile. The current functional test suite uses Chrome 155 and nodriver 0.50.6. See [Scope and limitations](#scope-and-limitations) for how to interpret the fingerprint result.

## Features

- 🕵️ **Direct CDP automation** — nodriver avoids Selenium and a ChromeDriver binary and is designed to reduce common automation signals.
- ☁️ **Cloudflare checkbox helper** (`cf_verify`, needs the `[cf]` extra); success depends on the challenge and browser configuration.
- 🖼️ **Frames are not a blind spot** — `list_frames` plus a `frame` argument on the readers, and `take_snapshot` splices each iframe's tree in under the element hosting it, so a payment field or consent wall inside one gets a uid and can be clicked and filled like anything else.
- 🔍 **A network log that says what happened** — status, response headers, a DevTools-style timing breakdown (DNS, connect, TLS, time to first byte, download), the script and line that started each request, transfer size, redirect hops and failures, plus WebSocket frames and server-sent events. A 500, a 404, a redirect and a DNS failure are different lines, not the same one.
- 💾 **Response capture to disk** — `capture_bodies` captures matching responses from tabs, workers and service workers before later navigation or buffer eviction can discard them. A JSON index records URL, POST data, status and headers; `search_bodies` searches saved traffic. Capture status reports saved bodies, skips and errors.
- 🛡️ **Passive security review built in** — `audit_security` checks security headers (with a real CSP analysis: nonces, `strict-dynamic`, multiple policies), CORS, TLS and certificates of every origin, cookie flags, responses a CDN could serve to the wrong user, every first and third party the page talks to, and lists what Chrome's own Issues panel reports: CSP violations, mixed content, rejected cookies. `inspect_storage` inventories localStorage, sessionStorage, IndexedDB, Cache Storage and service workers, and decodes tokens it finds there. `export_har` writes everything to a HAR file for Burp, ZAP or DevTools — with the wire headers, Cookie and Set-Cookie included.
- 👥 **Several isolated browsers at once** — pass `browser: "agent-a"` to any tool and that name gets its own Chrome, in its own process, with its own cookies, tabs and uids. Parallel agents stop stealing each other's selected tab. Omit it and nothing changes: the default browser runs in the server process, exactly as before. [How it works](#several-agents-several-browsers)
- 🧩 **70 tools** covering navigation, input, snapshots, screenshots, content/PDF export, network + console inspection, device emulation, cookies/storage, sessions, profiles, and performance tracing.
- 🧠 **Schemas written for the agent, not just the compiler** — every parameter carries a description, fixed-value options are real enums, and each tool declares read-only/destructive hints. See [why this matters](#built-for-the-agent-that-calls-it).
- 📄 **Compact accessibility-tree snapshots** (`take_snapshot`) — searchable page text with a uid per element. Measured on the Hacker News front page: **27 KB against 97 KB unfiltered, 72% smaller, with every link, URL and text preserved.** Same-origin URLs are printed relative to the page, which is lossless because the root node keeps the absolute one. That saving lands on every single agent step.
- 🔗 **Attach to a browser you are already signed into** (`use_running_browser`) — drive your real Chrome profile over its debugging port instead of rebuilding logins in a fresh one.
- 📱 **Device emulation** (Pixel 7, iPad) with correct UA / client hints.
- ✅ **Actions that verify themselves** — `fill` reads the value back, `set_checked` reads the checked state back, and `click`, `press_key` and `type_text` report when no input event reached the page. A tool that cannot confirm what it did says so instead of reporting success.
- 💾 **Session save/restore** — persist logins across runs.
- 🧬 **Ephemeral by default, persistent when needed** — each session gets its own temporary Chrome profile. Named persistent profiles retain logins and offer configurable startup: a new tab (default), specified URLs, or the previous tabs without a restore-button click.
- ⚡ **One-command setup** for 15+ MCP clients.

## Installation

```bash
# Recommended: isolated install, won't touch your global Python environment
uv tool install nodriver-mcp

# or with pip
pip install nodriver-mcp

# or run it without installing anything
uvx nodriver-mcp
```

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) for the first and third form. To track the development branch instead of a release:

```bash
uv tool install "nodriver-mcp @ git+https://github.com/andresolbach/nodriver-mcp-server.git@main"
```

Also listed in the [official MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.andresolbach/nodriver-mcp-server`, so clients and marketplaces that read the registry can find and install it directly.

> Uses upstream [`nodriver`](https://pypi.org/project/nodriver/) `>=0.50.6`, tested against Chrome 155. `uv tool install` keeps dependencies isolated from your global Python environment.

You'll also need a local installation of **Google Chrome** (auto-detected).

### Upgrade

```bash
uv tool upgrade nodriver-mcp
```

## Requirements & tested versions

The automated suite runs schema, documentation and behavior checks plus real-browser integration tests. The current Windows browser test environment is **Chrome 155 / nodriver 0.50.6 / Python 3.12.11**. CI also runs the non-browser suite on macOS and Linux. Future Chrome or nodriver releases can require compatibility updates.

| Component | Requirement | Verified version |
|-----------|-------------|------------------|
| Python | 3.12+ | 3.12.11 |
| Google Chrome | any recent stable | 155.0.8059.79 |
| Operating system | Windows / macOS / Linux | Windows 11 |
| `nodriver` | >= 0.50.6 | 0.50.6 |
| `mcp` (MCP SDK) | >= 1.26.0, < 2 | 1.26.0 |
| `pillow` | >= 12.1.1 | 12.1.1 |
| `tomli-w` | >= 1.0.0 | 1.2.0 |

Runtime dependency ranges and tested versions are listed in [`requirements.txt`](https://github.com/andresolbach/nodriver-mcp-server/blob/main/requirements.txt). `uv tool install` creates an isolated installation; contributors can use `uv sync --locked --group dev` in this repository to reproduce the committed dependency set.

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

### Manual config

If you'd rather paste it yourself, this works in any MCP client (`claude_desktop_config.json`, `~/.claude.json`, `.cursor/mcp.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "nodriver": {
      "command": "uvx",
      "args": ["nodriver-mcp"]
    }
  }
}
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NODRIVER_HEADLESS` | Headless mode (`true`/`false`) | `false` |
| `NODRIVER_USER_DATA_DIR` | Explicit persistent Chrome profile dir (overrides the default) | Ephemeral temp profile, auto-deleted per session |
| `NODRIVER_BROWSER_PATH` | Chrome executable path | Auto-detected |
| `NODRIVER_PROXY` | Proxy server address | None |
| `NODRIVER_BROWSER_URL` | Attach to a Chrome already running at this address (e.g. `http://127.0.0.1:9222`) instead of launching one | Launch our own |
| `NODRIVER_ENABLE_TRANSLATE` | Set `true` to re-enable Chrome's Google Translate popup | Disabled |
| `NODRIVER_ENABLE_EXTENSIONS` | Set `true` to allow externally-installed Chrome extensions (and their prompts) | Disabled |

By default the browser starts clean for automation: the **Google Translate popup is suppressed** and **externally-installed Chrome extensions are blocked** (so you don't get "an extension requires your attention" prompts). Re-enable either via the env vars above **or at runtime with the `set_browser_flags` tool** — which can also set **any other Chrome launch flags** (e.g. `--lang=de-DE`, `--window-size=1280,800`) via its `extra_args` parameter. The browser also **auto-recovers** if Chrome is closed or crashes between calls — tools relaunch it instead of failing.

Ephemeral browsers start on `about:blank`, keeping New Tab page traffic out of the network log. Persistent profiles default to a new tab and can instead open specified URLs or restore their previous tabs; see [Profiles](#profiles). `new_page` reuses an empty startup tab.

## Extensions

`manage_extensions` handles extensions at runtime:

- `manage_extensions("list")` — extensions installed in the active profile (name, version, id) plus the current state
- `manage_extensions("on")` / `("off")` — the master switch (`--disable-extensions`); restarts Chrome. It covers unpacked extensions too, so `"off"` really means off
- `manage_extensions("load", path)` / `("unload", path)` — unpacked extensions from a folder

To use an extension permanently, switch to a persistent profile, install it once from the Chrome Web Store in that browser, and turn extensions on — it then loads on every launch.

> **Unpacked extensions need Chromium or Chrome for Testing.** Official Chrome builds dropped `--load-extension` in v137, and as of Chrome 151 neither `--enable-unsafe-extension-debugging` nor disabling `DisableLoadExtensionCommandLineSwitch` brings it back — the flag is accepted and the extension is silently never registered. `manage_extensions` detects a branded build and says so instead of pretending it worked. Point `NODRIVER_BROWSER_PATH` at Chromium / Chrome for Testing if you need unpacked loading.

## Several agents, several browsers

One agent working alone can skip this section: without the `browser` argument everything runs on a single shared browser, exactly as before.

Point **two agents** at one browser, though, and they collide. They share the selected tab, so a `select_page` or a navigation by one silently changes what the other sees, and every `uid` the other is holding goes stale. Nothing errors — the second agent simply acts on the wrong page.

So every tool takes an optional `browser` argument, and a name that does not exist yet creates one on the spot:

```jsonc
{ "url": "https://example.com", "browser": "agent-a" }
```

Each name is a **separate Chrome in a separate process**, with its own profile, cookies, tabs, snapshot uids and console/network capture. Isolation is structural rather than careful: there is no shared state left to collide over, and a Chrome that hangs or crashes takes down nothing but its own browser.

The default costs nothing. `"default"` runs inside the server process itself, so a session that never opens a second browser is the same single process and the same code path it always was. Only extra names spawn anything.

| Tool | |
|---|---|
| `list_browsers` | What is open: names, whether Chrome runs, profile, tabs and their URLs. Reports without starting anything. |
| `shutdown_browser` | Quits one browser's Chrome and frees its name. `close_browser` only quits Chrome and keeps the browser's profile and flags for next time. |

Each extra browser is a Python process plus a full Chrome — roughly 200 MB and about a second to start. Up to **12** can be open at once, including the default. Two callers sharing one *name* still share one Chrome: the isolation is per browser, not per caller, so parallel agents each need their own.

## Profiles

By default every browser launches Chrome with a **fresh temporary profile** that is created and deleted automatically. That also means you can run nodriver from **Claude Desktop, Claude Code and the VS Code extension at the same time** — each gets its own isolated Chrome, and they never fight over a shared profile. No configuration, no detection logic, nothing to clean up.

When you want to **reuse a login across sessions**, create a named persistent profile and switch to it:

- `list_profiles` — list persistent profiles and show the active one
- `create_profile(name, activate=false, restore_tabs=false)` — create a reusable profile; starts with a new tab by default
- `use_profile(name)` — switch to a persistent profile (`""`/`"temp"` returns to ephemeral)
- `use_temp_profile` — switch back to a fresh ephemeral profile
- `delete_profile(name)` — remove a persistent profile

Persistent profiles live under `~/.nodriver-mcp/profiles/<name>`. Startup defaults to a new tab. Select a profile with `use_profile(name)`, then choose its startup behavior through MCP:

```python
set_browser_flags(startup_mode="new_tab", restart=False)
set_browser_flags(startup_mode="urls", startup_urls=["https://example.com", "https://example.org"], restart=False)
set_browser_flags(startup_mode="restore", restart=False)
set_browser_flags()  # read the saved choice without changing anything
```

Each setting is saved in that profile's `nodriver-startup.json`, survives MCP restarts, and takes precedence over Chrome's startup preference. `restart=False` leaves current tabs open and applies the choice on the next browser launch; the default `restart=True` closes Chrome immediately so the next action uses the new setting. `restore` resumes the last open tabs without clicking a recovery prompt or adding an extra blank tab. With no saved MCP choice, explicit Chrome startup preferences are respected. Temporary profiles still start blank and cannot save these startup settings. After restarting the MCP server itself, select the same profile again with `use_profile(name)`; profile selection is per server session. You can still force a fixed profile globally with the `NODRIVER_USER_DATA_DIR` env var.

## Drive a browser you are already signed into

Chrome locks its user-data-dir, so the profile holding your real logins cannot be opened a second time. The way in is to attach to the browser that already has it open — then you skip rebuilding every login through automation.

Start Chrome yourself, once:

```bash
# Windows
chrome.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\agent-profile"
# macOS / Linux
google-chrome --remote-debugging-port=9222 --user-data-dir=~/.config/agent-profile
```

Then attach, either at runtime with `use_running_browser(port=9222)`, or up front by setting `NODRIVER_BROWSER_URL=http://127.0.0.1:9222`. Every tool then acts on that browser and its real tabs.

While attached, this server **never closes a browser it did not start**: `close_browser` and profile switches only detach. Go back to a self-launched browser with `use_temp_profile` or `use_profile`.

> ⚠️ **That profile becomes part of the agent's reach.** Whatever it is signed into — mail, bank, company systems — is reachable from here, because a cookie jar is all or nothing. Point this at a profile you are willing to expose, not your everyday one.

## Tools (70)

Network collection is enabled automatically on each tab. Console collection is opt-in: call `enable_console_collection` when you want `list_console_messages` / `get_console_message` to start collecting events. This keeps `Runtime.enable()` disabled by default for sites that detect attached debuggers.

For mobile-only sites, pass `device` directly to `new_page(...)` or `navigate_page(...)` so the first real request already carries mobile signals.

`click` sends **real CDP input events**, so the page sees `isTrusted=true`. Because those are delivered by coordinate, it scrolls the element into view and then hit-tests several points inside it, since a sticky header or cookie banner can own the centre pixel — on `docs.pypi.org` that affects 43 of 54 visible links. If no point reaches the element, `if_covered` decides: `"report"` (default) refuses and names the blocker, leaving the page untouched and the session undetectable; `"synthetic_click"` clicks the element directly, which works through anything but makes the page see `isTrusted=false`. Either way the response says which path was taken, so a detectable click is never silent.

| Category | Tools |
|----------|-------|
| **Several browsers (2)** | `list_browsers` · `shutdown_browser` |
| **Input automation (12)** | `click` · `click_at` · `hover` · `fill` · `fill_form` · `set_checked` · `select_option` · `type_text` · `press_key` · `drag` · `upload_file` · `handle_dialog` |
| **Navigation (10)** | `navigate_page` · `new_page` · `close_page` · `close_browser` · `list_pages` · `select_page` · `wait_for` · `wait_for_selector` · `scroll_page` · `scroll_to_selector` |
| **Snapshots & debugging (12)** | `take_screenshot` · `take_snapshot` · `get_page_content` · `query_selector` · `list_frames` · `evaluate_script` · `get_computed_styles` · `save_pdf` · `enable_console_collection` · `disable_console_collection` · `list_console_messages` · `get_console_message` |
| **Network monitoring (6)** | `list_network_requests` · `get_network_request` · `capture_bodies` · `search_bodies` · `export_har` · `block_resources` |
| **Security audit (2)** | `audit_security` · `inspect_storage` |
| **Device emulation (4)** | `emulate` · `emulate_device` · `reset_emulation` · `resize_page` |
| **Performance (3)** | `performance_start_trace` · `performance_stop_trace` · `take_memory_snapshot` |
| **Cookies & storage (5)** | `get_cookies` · `set_cookie` · `clear_cookies` · `get_local_storage` · `set_local_storage` |
| **Session management (3)** | `save_session` · `load_session` · `list_sessions` |
| **Profiles & browser (9)** | `list_profiles` · `create_profile` · `use_profile` · `use_temp_profile` · `use_running_browser` · `delete_profile` · `set_browser_flags` · `set_proxy` · `manage_extensions` |
| **Anti-detection helpers (2)** | `cf_verify` · `bypass_insecure_warning` |

📖 **[Full tool reference →](https://github.com/andresolbach/nodriver-mcp-server/blob/main/docs/TOOLS.md)** — every tool with its exact parameters, types, defaults and enum values, generated straight from the live schemas.

## Built for the agent that calls it

An MCP tool is only as good as what the model can see of it. Most servers hand over a name, a sentence, and untyped parameters — leaving the agent to guess whether it's `type="url"` or `type="goto"`, and burning a failed call to find out.

Here, the schema does that work:

- **Every parameter has a description in the schema itself** — not buried in a prose blob the client may never show. All 70 tools, all parameters, no exceptions (there's a test for it).
- **Fixed-value parameters are real enums.** `navigate_page(type=…)` advertises exactly `url`, `back`, `forward`, `reload`. A wrong value is rejected by validation before it ever reaches Chrome, instead of returning an error the agent has to interpret.
- **Numeric and array bounds are declared** — `quality` is 0–100, `wait_for(text=…)` requires at least one entry.
- **Structured parameters are typed.** `fill_form` publishes `{uid, value}` rather than an opaque `list[dict]`.
- **Every tool declares behaviour hints** (`readOnlyHint`, `destructiveHint`, `idempotentHint`). Clients use these to group permissions — so a client can auto-approve `take_snapshot` while still prompting for `delete_profile`.
- **Descriptions say when *not* to use a tool**, and point at the better one. `take_screenshot` tells the model to prefer `take_snapshot`; `click_at` points back to `click`.

## Relationship to chrome-devtools-mcp

This project uses nodriver and Python. [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp) is a separate implementation with its own evolving tool API. Compare the current tool references when choosing a server; matching names do not imply identical arguments or behavior.

This server does not implement `performance_analyze_insight`, `lighthouse_audit` or `screencast_start/stop`. Extension management is available through `manage_extensions`, subject to the Chrome build restrictions described above.

## Use cases

- **Browse and extract structured content** using a real Chrome session, with network and response-body inspection when the data comes from an API.
- **Let an AI agent browse the real web** — Claude, Cursor, Windsurf and other LLM agents can log in, fill forms, click, read pages, and screenshot.
- **Automate authenticated workflows** and reuse the login across sessions with persistent profiles.
- **LLM-driven web research & data extraction** using compact accessibility-tree snapshots instead of brittle screenshots.
- **End-to-end / QA testing** with device emulation, network + console inspection, and performance traces.
- An **undetected alternative to Playwright, Puppeteer and Selenium** for agentic browsing.

## FAQ

**Is this an undetected alternative to chrome-devtools-mcp?**
It is an alternative built on nodriver, with familiar browser tools and additional profile and network features. It is not an exact API replacement and does not guarantee that sites cannot detect automation.

**Can it bypass Cloudflare?**
The optional `cf_verify` helper attempts supported Cloudflare checkbox challenges. Success is not guaranteed, and it does not solve image or text CAPTCHA puzzles.

**I already use `chrome-devtools-mcp` or `playwright-mcp` and keep getting blocked. How do I switch?**
Add the server entry under [Manual config](#manual-config), then review the [tool reference](https://github.com/andresolbach/nodriver-mcp-server/blob/main/docs/TOOLS.md) and adapt any tool-specific prompts or scripts. You can run both servers side by side.

**The site works in my normal browser but not under automation. Why?**
Possible causes include profile or cookie differences, automation signals, IP reputation, request rates and application errors. Compare the network log and browser state before assuming the driver is the cause.

**Does it help with captchas?**
The optional `cf_verify` helper supports some Cloudflare checkbox challenges. Image grids, text CAPTCHAs and hCaptcha puzzles are not solved.

**Which clients are supported?**
One command installs it into 15+ MCP clients: Claude Desktop, Claude Code, Cursor, Windsurf, Codex, Gemini CLI, Copilot CLI, Kiro, VS Code, Cline, Roo Code, Amazon Q, Warp, Opencode, Trae.

**Can I run it in several clients at the same time?**
Yes. Each instance uses its own ephemeral Chrome profile by default, so Claude Desktop, Claude Code and the VS Code extension can all use nodriver **simultaneously** without colliding.

**Headless or visible browser?**
A real Chrome window by default; set `NODRIVER_HEADLESS=true` for headless.

**How do I keep a login between sessions?**
Create a persistent profile with `create_profile` and switch to it with `use_profile`, or use `save_session` / `load_session`.

**Does it work on Windows / macOS / Linux?**
The project supports all three. Browser integration tests run on Windows 11 with Chrome 155; CI runs the non-browser suite on Windows, macOS and Linux.

## Scope and limitations

- Detection resistance is site- and configuration-dependent. The fingerprint result above documents those checks in that environment; it does not guarantee access to every site. IP reputation, request rates and session state also matter.
- `cf_verify` is an optional helper for supported Cloudflare checkbox challenges, not a general CAPTCHA service. Image grids, text CAPTCHAs and hCaptcha puzzles are not solved.
- This is an independent tool API, not an exact replacement for `chrome-devtools-mcp`. Check the tool reference when migrating scripts or prompts.
- Persistent profiles can be used by only one running browser at a time. After a complete MCP server restart, select the same profile again with `use_profile` to resume it.

## Changelog

See [CHANGES.md](https://github.com/andresolbach/nodriver-mcp-server/blob/main/CHANGES.md). Version 2.6.0 adds per-profile startup modes, fixes graceful browser shutdown and tab restoration, and updates nodriver to 0.50.6. Existing network capture, passive security checks and isolated browser sessions remain available.

## Credits

Based on [`nodriver-mcp`](https://github.com/Saber-CC/nodriver-mcp) by **Saber-CC** (MIT). Browser backend by [`nodriver`](https://github.com/ultrafunkamsterdam/nodriver) (ultrafunkamsterdam). Some tool names and workflows are inspired by [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp).

## License

[MIT](https://github.com/andresolbach/nodriver-mcp-server/blob/main/LICENSE)
