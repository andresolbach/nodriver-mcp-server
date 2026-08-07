# Changelog

## 1.8.0 — attach to your own browser, a snapshot two thirds smaller, two silent failures fixed

### Two tools reported success while doing nothing

Both were found by using this server on real sites, and both share the worst
failure mode an agent can meet: the call looks fine, so the agent carries on and
fails three steps later somewhere unrelated.

- **`fill` left framework-controlled fields empty.** It cleared the field first
  (`value = ''` plus an input event), which makes React and similar re-render;
  the focus goes with the replaced node and every keystroke after that lands
  nowhere. It now selects the existing content and types over it, the way a
  person does, falls back to inserting the text as one edit, and **reads the
  value back** — if it did not land, you get an error instead of a false success.
- **`upload_file` attached nothing.** Chrome renders `<input type=file>` with an
  internal shadow button, and that is what the accessibility tree exposes, so
  addressing the uid made `DOM.setFileInputFiles` a silent no-op. It now resolves
  to the real input (itself, a descendant, or the input a label controls) and
  reports what was actually attached.

Two more that surfaced while testing the above:

- **A contenteditable element could not be addressed at all.** Its role is
  `generic`, which the snapshot collapsed away, leaving only the text node
  behind — and `fill` on that died with a `TypeError` about `toLowerCase`.
  Focusable containers are now kept in the snapshot, and `fill` resolves a text
  node to its element and otherwise says plainly that the uid is not fillable.
- **An attached browser was reconnected on every call.** nodriver derives
  `.stopped` from a process it spawned, so a browser we attached to always
  reported stopped. Liveness for those now comes from an actual CDP probe.

### New

- **`use_running_browser`** — drive a Chrome that is already running, instead of
  launching one. Chrome locks its user-data-dir, so the profile holding your real
  logins can only be driven by attaching to it. Start Chrome with
  `--remote-debugging-port=9222` and attach, at runtime or via
  `NODRIVER_BROWSER_URL`. While attached the server **never closes a browser it
  did not start**: `close_browser` and profile switches only detach.
  That profile becomes part of the agent's reach, which the README states plainly.
- **`get_computed_styles`** — an element's resolved styles, box and whether it is
  actually rendered and in the viewport. Answers why a click does nothing on an
  element that is present but has zero size, or why something is invisible.
- **`evaluate_script` gained `script_path` and `file_path`** — read the function
  from a .js file (no escaping a long script into JSON) and write the result to
  disk instead of into the conversation.
- **`list_pages` now reports each page's CDP `targetId`** and marks the selected
  page. Unlike the index, a targetId survives other tabs opening and closing.

### Snapshots are roughly two thirds smaller

`take_snapshot` was emitting a `StaticText` line for text that its parent line
already showed — a link's accessible name is computed from exactly those
children. The check meant to drop them never fired, because `StaticText` always
carries `InlineTextBox` children and so never looked like a leaf.

Measured on the Hacker News front page, same page load:

| | chars |
|---|---|
| unfiltered (`verbose=true`) | 106,266 |
| **compact (new default)** | **34,144** |

68% smaller, with **all 201 URLs and all 270 distinct texts still present** —
verified rather than assumed. Layout-table scaffolding is collapsed too. Real
data tables are deliberately left alone: flattening `row` and `cell` would leave
values with no row to belong to. `verbose=true` still returns everything.

Tool count: 57 → 59.

## 1.7.3 — lead with the symptom, and a Glama manifest

- **README now opens with the problem, not the pitch.** It described the
  solution ("undetected", "anti-bot-resistant") — vocabulary nobody types into
  a search box. It now names what people actually see: "Just a moment…",
  "Verify you are human" loops, "Unusual Activity Detected", bare 403s from a
  site that opens fine by hand, `navigator.webdriver === true`. It also names
  the tools people migrate from, and states plainly what this does **not** do
  (no captcha solving, not every site, not a fix for hammering a server).
- **New FAQ entries** for the high-intent questions, including the one-line
  answer to "I already use chrome-devtools-mcp and keep getting blocked — how
  do I switch?" (replace the config entry; the tool names match, so prompts and
  scripts keep working).
- **Added `glama.json`**, which Glama uses for maintainer verification and
  server metadata.

No changes to the server itself; 1.7.0-1.7.3 behave identically.

Tool count: 57 → 57.

## 1.7.2 — fix the broken logo and links on PyPI

The README doubles as the PyPI project description, where it is rendered
standalone — so every relative reference in it pointed at `pypi.org` and broke.

- **Fixed: the logo did not load on PyPI.** It used a relative `assets/logo.svg`.
  PyPI proxies images through camo, which fetches the URL server-side and cannot
  resolve a relative path. Now an absolute `raw.githubusercontent.com` URL, and
  a PNG rather than an SVG — a 512px raster survives any image proxy, and at the
  130px display size it is sharper than needed either way.
- **Fixed: four relative links** (`requirements.txt`, `docs/TOOLS.md`,
  `CHANGES.md`, `LICENSE`) were 404s on the package page. Now absolute.
- Tests reject relative image sources and relative links in the README, plus
  repo-hosted SVGs, so this cannot come back.

No changes to the server itself; 1.7.0-1.7.2 behave identically.

Tool count: 57 → 57.

## 1.7.1 — MCP registry listing

- Added the `mcp-name:` ownership marker the MCP Registry looks for in the
  published package description, plus a `server.json` on the current
  (2025-12-11) schema and a GitHub Actions workflow that publishes the registry
  entry from a version tag over OIDC — no stored registry token.
- No functional changes to the server; 1.7.0 and 1.7.1 behave identically.

Tool count: 57 → 57.

## 1.7.0 — schemas an agent can actually read, PyPI release, tool reference

No new tools this time. Instead, the 57 that exist now describe themselves
properly to the model calling them — which is what decides whether a tool call
succeeds on the first attempt.

- **Every parameter now carries a description in the JSON Schema.** Previously
  the `Args:` block lived only in the free-text description blob, so a client
  reading the schema saw `{"title": "Uid", "type": "string"}` and nothing else.
  All 57 tools, every parameter, enforced by a test.
- **Fixed-value parameters are real enums.** `navigate_page(type=…)`,
  `handle_dialog(action=…)`, `scroll_page(direction=…)`,
  `take_screenshot(format=…)`, `get_page_content(format=…)`,
  `manage_extensions(action=…)`, `emulate(color_scheme=…, network_conditions=…)`,
  `block_resources(types=…)` and both log-filter parameters. A wrong value is
  now rejected by validation instead of reaching Chrome and coming back as prose.
- **Numeric and array bounds are declared** — quality 0-100, non-negative
  timeouts and indices, `wait_for(text=…)` needs at least one entry.
- **`fill_form` takes a typed `{uid, value}` model** instead of `list[dict]`, so
  the keys are published rather than guessed.
- **Every tool declares MCP behaviour hints** (`readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`) and a human-readable
  title. Clients can auto-approve the 15 read-only tools while still prompting
  for the 8 destructive ones.
- **Descriptions say when not to use a tool**, and name the better one —
  `take_screenshot` points at `take_snapshot`, `click_at` points back at `click`,
  `type_text` at `fill`.
- **Docstring indentation no longer ships to the model.** Descriptions are run
  through `inspect.cleandoc`, which removes four leading spaces per line from
  every tool description in every request.
- **Server instructions rewritten** into a usable briefing: the snapshot→uid→act
  loop, what invalidates a uid, which tool to reach for when, and how to keep a
  login.

- **Fixed: `list_network_requests(resource_types=…)` could never match anything.**
  The collected type was `str(ResourceType.XHR)` — which is the string
  `"ResourceType.XHR"`, not `"XHR"` — so every filter silently returned zero
  results. Now stored as the bare CDP value, and a test pins the advertised enum
  to `nodriver`'s real `ResourceType`.
- **Fixed: the console type filter documented a value that does not exist.**
  CDP emits `warning`; the docs said `warn`, which matched nothing.

- **Published to PyPI** — `uvx nodriver-mcp`, `uv tool install nodriver-mcp` or
  `pip install nodriver-mcp` instead of a git URL.
- **New [`docs/TOOLS.md`](docs/TOOLS.md)**, generated from the live schemas by
  `scripts/generate_tool_docs.py`, so the reference cannot drift from the code.
- **Tests and CI.** Contract tests over the emitted schemas, plus tests that fail
  the build when the README's tool count, the badge, the tool table, the
  changelog or `server.json` disagree with what the server actually registers —
  the drift that left the README advertising 56 tools while 57 shipped.
- **`server.json`** for the official MCP registry.

Tool count: 57 → 57.

## 1.6.0 — no more stray blank tab, extension management, merged feature flags

- **`new_page` reuses the empty startup tab.** Chrome always comes up with one
  tab, so the first `new_page` used to leave a blank page sitting next to the
  real one for the rest of the session. It is now reused when it is the only
  tab and genuinely empty — a blank tab among others is still left alone, and
  isolated contexts still get their own target.
- **Chrome starts on `about:blank` instead of the New Tab page.** The NTP issues
  its own Google requests, which showed up in `list_network_requests` and cost
  a page load nobody asked for.
- **`manage_extensions`** — list extensions installed in the active profile
  (name, version, id), flip the master switch on/off, and load/unload unpacked
  extensions from disk. Loading unpacked ones works on Chromium and Chrome for
  Testing; on official Chrome builds (which have ignored `--load-extension`
  since v137 — verified still ignored on Chrome 151, even with
  `--enable-unsafe-extension-debugging`) the tool says so instead of silently
  doing nothing.
- **Fixed: duplicate `--disable-features` silently dropped entries.** Chrome
  honours only the last occurrence of the switch, and nodriver passes one of
  its own, so the server's `Translate` entry was overriding nodriver's
  `IsolateOrigins,site-per-process` (and would have broken extension loading).
  Both are now merged into a single switch.
- **`click` / `click_at`: trusted CDP input again, scripted click only as a
  fallback.** An earlier local fix had switched `click` to `element.click()`
  plus synthetic events to dodge a renderer crash. That works, but those events
  reach the page with `isTrusted=false` — precisely what bot detection reads,
  in a server whose entire purpose is not being detectable. Real CDP input
  events are the default again; the scripted path is used only where the CDP
  one genuinely cannot be delivered: on a **touch-emulated target** (where
  `Input.dispatchMouseEvent` can take the renderer down — now tracked per
  target by `emulate`/`emulate_reset`) or after the CDP click times out or
  errors. When the fallback runs, the response says so, so a degraded click is
  never silent. Both tools are bounded at 10s per step, so a wedged page can no
  longer hang the call, and `click_at` no longer behaves differently to `click`.
- **Fixed: `manage_extensions("off")` did not turn unpacked extensions off.**
  The master switch only gated profile-installed extensions, so anything loaded
  via `"load"` kept loading after an explicit `"off"`. It now gates both; the
  paths stay registered, and `"on"` brings them back.

Tool count: 56 → 57.

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
