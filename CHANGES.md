# Changelog

## 1.5.1 — selector query/scroll, resource blocking, arbitrary Chrome flags

- **`set_browser_flags` now sets arbitrary Chrome launch flags** via `extra_args`
  (e.g. `["--lang=de-DE", "--window-size=1280,800"]`), on top of the named
  translate/extensions toggles.
- **`query_selector`** — find elements by CSS selector and list their tag, text,
  href and id.
- **`scroll_to_selector`** — scroll a specific element into view.
- **`block_resources`** — block images/fonts/stylesheets/media to speed up
  scraping and save bandwidth (pass `[]` to unblock).

Tool count: 53 → 56.

## 1.5.0 — content/PDF export, selector waits, cookie clearing, runtime flags

Five new tools (count 48 → 53):

- **`set_browser_flags`** — toggle the Google Translate popup and external Chrome
  extensions **at runtime** (overrides the env vars; restarts the browser to apply).
- **`get_page_content`** — raw page text (`innerText`) or full HTML, for scraping/reading.
- **`wait_for_selector`** — wait for a CSS selector to appear (optionally visible),
  complementing the text-based `wait_for`.
- **`save_pdf`** — export the current page as a PDF (Chrome print-to-PDF).
- **`clear_cookies`** — clear all browser cookies.

## 1.4.3 — close_browser tool

- New **`close_browser`** tool to quit Chrome entirely (unlike `close_page`,
  which keeps the last tab). The browser relaunches automatically on the next
  tool call with the currently selected profile. Tool count: 47 → 48.

## 1.4.2 — clean-launch defaults + auto-recovery

- **Google Translate popup suppressed by default** (`--disable-features=Translate`);
  re-enable with `NODRIVER_ENABLE_TRANSLATE=true`.
- **Externally-installed Chrome extensions blocked by default**
  (`--disable-extensions`), so the "an extension requires your attention" prompt
  no longer appears; re-enable with `NODRIVER_ENABLE_EXTENSIONS=true`.
- **Browser auto-recovery**: if Chrome is closed/crashes between calls, a cheap
  liveness probe detects the dead connection and relaunches the browser instead
  of every tool failing with a "no close frame" websocket error until restart.

## 1.4.1 — fresh page URLs/titles in responses

- `navigate_page`, `new_page` and `list_pages` now refresh CDP target info before
  formatting, so the reported page URL and title are current instead of
  occasionally showing an empty URL / stale "New Tab" right after a navigation.

## 1.4.0 — ephemeral profiles by default + profile management

- **Temp profile by default.** The browser now launches with a fresh ephemeral
  Chrome profile (created and auto-deleted by nodriver) instead of a single
  shared `~/.nodriver-mcp/chrome-profile`. This lets multiple nodriver instances
  (Claude Desktop, Claude Code, VS Code, …) run **at the same time** without
  colliding on one profile — no detection logic or prompts needed. Verified with
  two independent browsers navigating concurrently.
- **Named persistent profiles** for reusing logins across sessions, via 5 new
  tools: `list_profiles`, `create_profile`, `use_profile`, `use_temp_profile`,
  `delete_profile`. Stored under `~/.nodriver-mcp/profiles/<name>`.
- `NODRIVER_USER_DATA_DIR` still works as an explicit persistent override.
- Tool count: 42 → 47.

## 1.3.0 — audit fixes & upstream nodriver

Backend migrated from the `Saber-CC/nodriver` fork (0.48.1, pinned at a moving
`@main`) to **upstream `nodriver>=0.50.3`**. The Chrome 146+ CDP fixes that once
required the fork (`sameParty` removed from `Cookie`,
`privateNetworkRequestPolicy` → `localNetworkAccessRequestPolicy`) are upstream
as of 0.50.x. Verified working against **Chrome 150** (navigate, a11y snapshot,
screenshot, cookie parsing, UA client-hints and device-metrics emulation).

`mcp` is now bounded `>=1.26.0,<2` to avoid the upcoming breaking v2.

### Fixed (all verified end-to-end against Chrome 150)

- **`select_page` had no effect** (critical). `_active_tab()` always returned
  `browser.tabs[-1]`, so every tool after `select_page(N)` acted on the
  last-opened tab, not the selected one. A selected `target_id` is now tracked
  and honored; `close_page` clears it; a foreground `new_page` selects itself.
- **`fill` / `fill_form` always failed** (critical). nodriver returns a
  `(RemoteObject, ExceptionDetails)` tuple from `Runtime.callFunctionOn`; the
  code read `.value` on the tuple → `AttributeError`. Added a tuple-safe
  `_call_function_on` helper that also surfaces JS exceptions. `fill_form` now
  reports per-field failures accurately instead of always saying "filled".
- **`evaluate_script(function, args=[…])` always errored** (critical) with
  `Either objectId or executionContextId must be specified`. It now binds the
  first resolved element as the call target and surfaces JS exceptions.
- **`press_key` modifier combos did nothing** (high). `Control+A`, `Control+C`,
  etc. never applied the CDP modifier bitmask. Modifiers (Alt=1/Ctrl=2/Meta=4/
  Shift=8) are now applied, plus `code`/`windowsVirtualKeyCode`/`text` for named
  and printable keys. `type_text`'s `submit_key` uses the same descriptor.
- **`get_network_request` / `get_console_message` returned the wrong item**
  (high). List views showed positional indices over a filtered/paginated/
  preserved set while the getters indexed the raw list. Each collected request/
  message now carries a stable monotonic `seq`; lists show it and the getters
  resolve by it.
- **`nodriver-mcp install --scope project` crashed** (high) with argparse exit 2
  (`--scope` was only on the parent parser). It is now accepted on the
  `install`/`uninstall` subcommands.
- **`--list-clients` / installer crashed on Windows cp1252 consoles** (high) when
  printing `✓`/`✗`. Replaced with ASCII status; CLI output is also reconfigured
  to UTF-8 defensively (so non-ASCII config paths never crash either).
- **`dbl_click` never fired a `dblclick`** (medium). Two `mouse_click`s produce
  two `click_count=1` clicks; a proper escalating `click_count` 1→2 sequence is
  now dispatched.
- **`ipad_air` preset was actually an Android Pixel Tablet** (medium): wrong UA
  and Android UA-CH client hints. Now uses a real iPad Safari UA and sends no
  UA-CH (Safari doesn't). All device presets now default to `en-US` Accept-
  Language instead of `zh-CN`, and their Chrome UA is bumped to 150.
- **`handle_dialog` / emulation input validation** (low). `handle_dialog`
  rejects unknown actions and fails gracefully when no dialog is open; malformed
  `viewport` / `geolocation` strings now return a clear error instead of a raw
  `ValueError`/`IndexError`.

### Known limitations (not changed in this release)

These are real but were left as-is to avoid larger refactors / behavior changes;
they mostly affect multi-tab or niche flows:

- **Console & network collection is process-global**, not per-tab, even though
  the tool docstrings say "the currently selected page". With multiple tabs the
  streams interleave, and a navigation on any tab rotates the preserved history
  for all of them.
- **Tab identity for the collection-enabled sets uses `id(tab)`**, which Python
  can recycle after a tab object is freed.
- **`new_page(isolated_context=…)`** uses `create_target(for_tab=True)`; matching
  the returned tab target can be brittle — prefer the default context if you hit
  timeouts opening isolated pages.
- **Named browser contexts and the tracing flag** are not disposed on browser
  restart.
- **Character typing (`type_text`, `fill`)** sends `text`-only key events, so
  literal newlines/Tabs inside typed text aren't submitted, and non-BMP
  characters (emoji, astral CJK) may split. Use `press_key` / `submit_key` for
  named keys.
- **`save_session` / `load_session`** only capture localStorage for the active
  tab's origin.
- **`resize_page`** sets the OS window size (no-op in headless); it does not set
  the content viewport. Use `emulate(viewport=…)` for a deterministic viewport.
- **`wait_for`** matches `document.body.innerText` only (misses inputs, aria,
  shadow DOM).
- **Error-handling contract is mixed**: some tools return `"Error: …"` strings,
  others let exceptions propagate to the MCP client.
