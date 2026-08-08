# Changelog

## 2.1.0 — tools that reported success while doing nothing

An independent audit drove this server through 25 agents and 2151 tool calls:
first as a black box, then through the source, then trying to refute its own
findings. The stealth core came out well — four search engines with no captcha,
Reddit's JS challenge solved mid-navigation, Cloudflare cleared on the first
load, and one site out of eight that simply blocked it. Everything below is the
layer on top of that, where the recurring defect was not a missing feature but a
tool answering "done" when nothing had happened.

### Navigation no longer churns the CDP session

`navigate_page` went through nodriver's `Tab.get()`, which delegates to
`Browser.get()` — and that navigates the *first* page target rather than the tab
it was called on, then calls `connection.attach()` again. Every navigation minted
a brand new CDP session for the same target and never detached the old one, so
each domain this server had enabled belonged to a stranded session while commands
went to a session where nothing was ever enabled. nodriver's event dispatcher
matches on event type without looking at `sessionId`, so events kept arriving and
everything *looked* healthy.

Same-tab navigation now sends `Page.navigate` directly and waits for the new
document to be usable. `Tab.back`, `forward` and `reload` were already sending on
the right connection and are untouched. Measured: the session id is now identical
before and after a navigation, `performance_stop_trace` stopped answering
"Tracing is not started", and `reset_emulation` and `disable_console_collection`
take effect after a navigation instead of writing into an orphan.

Two things fall out of it that nodriver was discarding. `Page.navigate` returns
`errorText`, so a domain that does not resolve is now an error instead of a
success on the previous page; and it returns `isDownload`, so a URL that turns
out to be a download says so rather than reporting a navigation that did not
happen. An HTTP error *status* is deliberately not treated as a failure — a 404
that comes with a body is a page, and reading it is usually the point. A 404 with
an empty body is the one case Chrome refuses outright, and the response says so.

### Response bodies work now, which was a second cause

Removing the session churn was not enough: `Network.enable` was sent with no
buffer sizes, so Chrome retained no resource bodies whatsoever and
`Network.getResponseBody` answered `-32000 No resource with given identifier
found` for every request, including one issued a second earlier. DevTools passes
these sizes; this now passes 50 MB total and 10 MB per resource. Measured: 0 of 3
bodies retrievable before, the full JSON after.

The same call was also guarded on `id(tab)`, which does not change when the CDP
session under it does, so the guard reliably skipped the one call that mattered.
It is keyed on `(target, session)` now, with the event handler still registered
once per target so nothing is recorded twice. That is also why `block_resources`
could report success while every image loaded: `setBlockedURLs` is a
Network-domain command and is ignored when the domain is not enabled on the
session it is sent to.

### Fixed

- **`load_session` restored nothing, and reported it as a number rather than an
  error.** It passed the raw JSON float from `expires` into
  `cdp.network.set_cookie`, whose generator calls `expires.to_json()` — a float
  has no `to_json`, so every cookie raised `AttributeError` into a
  `logger.warning` on stderr that no MCP client sees. `save_session` keeps CDP's
  `-1` sentinel for session cookies and `-1` is truthy, so 100% of cookies took
  the failing branch. The counter only advanced on success, which is how the
  advertised "arrive already logged in" round trip came back as
  `Cookies restored: 0` on a normal, non-error result — an agent following it
  scraped the logged-out view of a site and could not tell. Cookies now go
  through `CookieParam` and a bulk `Storage.setCookies`, and the count reported is
  read back from the browser instead of counting calls that did not raise.
- **Clicks and keystrokes were dropped whenever another window covered Chrome.**
  The browser launched without `--disable-backgrounding-occluded-windows`,
  `--disable-renderer-backgrounding` and `--disable-background-timer-throttling`,
  which Puppeteer and Playwright both pass by default. Chrome treats an occluded
  window as hidden: timers fall to about 1 Hz, `requestAnimationFrame` stops and
  input delivery becomes unreliable — while every tool still returns "Clicked
  uid=…". Three agents measured zero events reaching the page, and that state is
  guaranteed the moment two agents each own a browser. Also passes
  `--window-size=1280,900`, because `outerWidth`/`outerHeight` reading 0 is itself
  a signal.
- **`evaluate_script` returned CDP's wire format instead of JSON.** nodriver's
  `Tab.evaluate` asks for "deep" serialization, so `{"a": 1}` came back as
  `[["a", {"type": "number", "value": 1}]]` — several times the tokens, and a
  decoder every caller had to write. Primitives were unaffected, so the shape
  changed silently with the return value.
- **An unparseable CSS selector was reported as a match.** That same
  `Tab.evaluate` *returns* the `ExceptionDetails` object on a JS error instead of
  raising, and the object is truthy, so `wait_for_selector` answered "Element
  found." on its first poll and `scroll_to_selector` claimed to have scrolled;
  `query_selector` leaked a Python `TypeError` out of `json.loads`. All three now
  share one evaluate helper that raises on a JS error, and a selector that cannot
  parse says so instead of timing out.
- **`isolated_context` could never have worked.** nodriver initialises
  `Browser.connection` to `None` and never assigns it, so the documented "hold
  several logins to one site at once" path raised
  `'NoneType' object has no attribute 'send'` every time. `Browser` subclasses
  `Connection`, so it sends CDP itself. Also dropped `for_tab=True`, which asks
  for a target `browser.tabs` filters out, and taught the target wait to refresh
  nodriver's inventory instead of only ever timing out.
- **Uncaught exceptions were invisible.** Only `Runtime.consoleAPICalled` was
  subscribed, so a page throwing an uncaught `TypeError` reported "0 messages" —
  from the tool whose only job is to say what went wrong. `Runtime.exceptionThrown`
  is captured too, with its URL and line.
- **Console collection duplicated every message when toggled.**
  `remove_handler(event_type, callback)` takes both arguments; passing only the
  type raised `TypeError` into a bare `except`, so the handler was never removed
  and each re-enable added another one.
- **Performance traces were always empty.** `Tracing.start` asked for
  `ReturnAsStream` while `stop_trace` only listens for `Tracing.dataCollected`,
  which that transfer mode never emits. It uses `ReportEvents`, and says so when a
  trace is empty rather than quietly not writing the file it was given. Measured
  on one page: 0 events before, 18 655 after.
- **`take_snapshot`'s 200 000-character cap applied to `file_path` too**, so the
  escape hatch for a page too large to read inline was capped at the same size and
  a big page could not be read in full by any means. The cap is inline only now,
  and names what it truncated and how to get the rest.
- **The device presets shipped a malformed `Accept-Language`.** Chrome generates
  q-values itself, so `"en-US,en;q=0.9"` went out as `en-US,en;q=0.9;q=0.9` and
  landed in `navigator.languages` as `["en-US", "en;q=0.9"]` — a one-header
  signature on a server whose whole point is not standing out.
- **`get_page_content` returned an empty string for a frameset**, which is
  indistinguishable from a blank page. It falls back to `documentElement` and,
  when there is still no text, names the frames it can see.
- **`wait_for` swallowed every error and could only ever time out.** A crashed
  renderer, a detached execution context and an open JS dialog all looked like
  "not there yet". The timeout now carries the last error it hit.

### The browser cap stopped being a one-way ratchet

Twelve agents that each opened a browser and dutifully called `close_browser`
wedged the server for everything arriving after them. `close_browser` keeps the
name on purpose — its profile and flags have to survive the relaunch — but
nothing ever gave the name back, and the cap counts registrations rather than
Chromes. Seven of the audit's eighteen agents never got a browser at all.

- Idle browsers holding no Chrome are collected after
  `NODRIVER_BROWSER_IDLE_TTL_S` (default 900s, `0` disables), by a sweep that also
  runs once before the cap refuses. A browser still running Chrome is never
  reclaimed, and neither is one whose idle time cannot be determined — reclaiming
  is destructive, so "cannot tell" has to mean "leave it alone".
- `close_browser` no longer routes through get-or-create. Closing a name that was
  never opened answered with the *creation* cap error — a cleanup call failing
  exactly when cleanup matters — and spawned a whole subprocess to discover there
  was nothing to close, so a typo in `browser` burned a slot permanently.
- `list_browsers` stops advertising room it does not have. It reported
  `open == max` beside a fixed hint reading "pass a new name, nothing needs to be
  created first". It now reports `slots_free`, names what is reclaimable, and
  gathers its status calls instead of awaiting them one after another — eleven
  unresponsive workers cost eleven timeouts in a row, in the one call an operator
  makes when the pool is unhealthy.
- `NODRIVER_MAX_BROWSERS` makes the ceiling configurable. A 128 GB workstation and
  an 8 GB CI box should not be stuck with the same number.
- `close_browser` now says that it keeps the name and its slot, and names
  `shutdown_browser` as the call that releases both. Ten of the audit's agents
  picked the wrong teardown, which is what turned a documented design into an
  outage.

### Tests

Fifteen new regression tests, every one of which fails against the previous
release: six on the registry's bookkeeping with a stub worker (including one that
would hang forever if the reclaim sweep waited on another name's lock), eight
that need a real Chrome — the session round trip, the JSON shape, the unparseable
selector, response-body retrieval, resource blocking across a navigation, a trace
across a navigation and a navigation that genuinely fails — and one pure data
check that no device preset ships its own q-values.

The existing suite is a schema-and-drift contract and would have caught none of
the defects above; it asserts that the prose and the signatures agree, which is
worth having and is not the same thing as asserting that a tool does what it
says. Every defect in this release was of the second kind.

Tool count: 61 -> 61.

## 2.0.1 — the landing page did not mention the headline feature

Documentation only; the server is unchanged from 2.0.0.

The Features list, which is the part most readers actually skim, said nothing
about running several isolated browsers — the whole point of 2.0.0. It does now,
and links to the section that explains it.

One sentence also still claimed "All 57 tools", a number last true before 1.8.0.
The consistency check only ever matched the form "Tools: N", so this other
phrasing drifted freely for three releases. It now checks every "N tools" in the
README, allowing a different number only on a line that states ours beside it,
which is what a comparison against another project looks like. PyPI descriptions
are fixed per release, so 2.0.0's page keeps the wrong sentence; this is the
release that corrects it.

Tool count: 61 → 61.

## 2.0.0 — several independent browsers, one server

Two agents pointed at this server used to share one Chrome, because the whole
browser world lives in module globals: one browser, one selected tab, one
console and network buffer. Agent B calls `select_page`, and agent A's next
`click` lands on a tab it never opened, with every uid it holds gone stale.
Nothing errors. The second agent simply acts on the wrong page.

Every tool now takes an optional `browser` argument, and a name that does not
exist yet creates one on the spot: its own Chrome, in its own process, with its
own profile, cookies, tabs, snapshot uids and captured traffic. Rewriting ~190
global accesses into per-session objects would have touched all 59 tools, so a
browser became a process instead. The tools did not change at all, and isolation
stopped being something the code has to remember.

**Nothing changes for a session that does not use it.** Omit the argument and
you get the browser called `"default"`, which runs inside the server process on
the same code path as before — one process, one Chrome. Only extra names spawn
anything, so the feature is free to everyone not using it. Tool schemas are read
locally too, so listing them starts no subprocess either.

Two new tools: `list_browsers` (what is open, without starting anything) and
`shutdown_browser` (quits one browser's Chrome and frees its name, where
`close_browser` keeps its profile and flags for next time). Up to 12 browsers at
once, including the default.

The `browser` argument's own description is deliberately terse, because it rides
along on all 61 tools and would otherwise cost roughly 7000 tokens of repetition
in every session, most of it paid by people who never open a second browser. The
reasoning, the hazards and the costs live in the server instructions instead,
which are sent once — and there they can be spelled out properly.

### Found by reviewing this release before shipping it

An adversarial review of the merge raised 41 findings; seven survived
verification, several reproduced against the running code. All are fixed, each
with a regression test that failed against the first cut.

- **The temp-profile sweep could have deleted a live profile on Linux and
  macOS.** 1.9.5 swept anything in the system temp directory named `uc_*` that
  was old enough, relying on a rename failing to protect a directory still in
  use. That protection only exists on Windows; POSIX renames a directory happily
  while its files are open. A browser left running for two hours could have had
  its cookies, logins and localStorage deleted underneath it. The sweep now
  removes only profiles this server recorded as its own, and only once the
  process that recorded one is gone — ownership is checkable, a filename is a
  guess. The test that was supposed to cover the old guard never reached it: it
  wrote a file into the directory first, which reset the mtime the age check
  reads, so the sweep skipped on age and the rename was never attempted.
- **`_MAX_BROWSERS` did not hold under load.** The slot was taken only after the
  spawn finished, so concurrent first calls all counted the same free registry
  and all spawned. Ten simultaneous requests opened ten browsers regardless of
  the cap. The slot is now claimed before the slow part.
- **A worker whose subprocess died was never noticed.** Its lifecycle task waits
  on an event only this process sets, so every flag kept saying "running", the
  respawn path was unreachable, and each later call to that name waited out the
  full five-minute timeout while the orphaned Chrome kept running. Workers are
  now pinged before reuse and replaced when they stop answering.
- **`shutdown_browser` raced first use.** It read the registry without the lock
  that guards a spawn, so a shutdown arriving while a browser was starting
  answered "nothing to shut down" and then let the Chrome finish coming up.
- **`list_browsers` could fail with a one-character error.** It snapshotted the
  names, then awaited per browser, and indexed the registry afterwards — a
  browser retired in between raised a bare `KeyError`.
- **Errors on the default browser said everything twice.** FastMCP already
  raises `Error executing tool <name>: …`, and the routing layer added the same
  prefix again, on exactly the path where nothing was supposed to change.
- **Workers relied on nodriver's atexit handler.** They served until stdin
  closed and then simply exited, leaving the browser to the unreliable cleanup
  1.9.5 exists to replace. They now shut it down deliberately, in the same event
  loop, before the process ends.

One finding was refuted rather than fixed: with `NODRIVER_BROWSER_URL` set,
every browser name does attach to the same Chrome, but that is what the setting
asks for, and per-process state means the tabs still cannot collide.

### Smaller things

Two more fell out of comparing the new entry point against the old one rather
than assuming they matched:

- **Resources and prompts still answer.** FastMCP registers those handlers
  whether or not anything is defined, so this server advertised both
  capabilities and replied with an empty list. Serving only tools would have
  turned that into `Method not found` for any client that probes. They are
  forwarded, and a test now compares the advertised capabilities against the
  single-browser server directly.
- **The server reports its own version.** FastMCP left it unset, and the SDK
  then falls back to the version of the `mcp` library, so every release so far
  introduced itself as "1.26.0". It now says 2.0.0.

Tool count: 59 → 61.

## 1.9.5 — orphaned Chromes and temp profiles that were never cleaned up

Closing a browser was fire-and-forget. nodriver's `Browser.stop()` schedules
the connection close as a task nobody awaits, terminates Chrome in the same
breath, and leaves the throwaway profile to an `atexit` handler that retries
for 0.75 seconds. Chrome releases its files asynchronously on Windows — the
same race `delete_profile` hit in 1.9.3 — so the directory regularly survived.

Worse, none of it runs when the process is killed rather than asked to exit,
which is how an MCP server usually ends: the client restarts and terminates it.
Measured by killing the server mid-session: Chrome lived on as eight orphaned
processes and its profile stayed behind. On one developer machine that had
accumulated 16 abandoned profiles holding 1.8 GB.

`close_browser` and every profile switch now shut down in order and wait for
each step: close the CDP sockets, terminate Chrome, wait for the process to
actually exit, then remove the directory with retries that outlast the race.
The browser is deregistered from nodriver's atexit list so it is not torn down
twice, which also silences the `RuntimeError: Event loop is closed` noise that
used to appear in the log on every shutdown.

For the case nothing in-process can cover — being killed — the server sweeps
abandoned `uc_*` profiles from the temp directory at startup. Two guards keep
it away from a profile in use: the directory must be older than two hours, and
it is renamed before being deleted, so a live browser's profile fails the
rename and is left entirely alone rather than being emptied underneath it.

Tool count: 59 → 59.

## 1.9.4 — get_cookies and save_session only saw one tab's cookies

`get_cookies` says it reads the whole browser cookie jar unless you pass `url`.
It used `Network.getCookies`, which despite the name returns only the cookies of
the tab it is sent to. Open a second tab and a cookie set on the first vanished
from a call that claimed to list everything — reporting `Cookies (0):` moments
after `set_cookie` returned success.

`save_session` collected cookies the same way, under a comment reading "Collect
all cookies". That is the costly half: a session saved with three logins open
kept whichever site happened to be selected and silently dropped the other two,
which only shows up later, when `load_session` restores an incomplete login.

Both now use `Storage.getCookies`, which is the actual whole-jar call. Passing
`url` to `get_cookies` still filters through `Network.getCookies`, unchanged.

Found by an agent driving its own browser and reporting the mismatch between
what `set_cookie` claimed and what `get_cookies` showed.

Tool count: 59 → 59.

## 1.9.3 — delete_profile lost a race with Chrome's shutdown

Found by exercising all 59 tools end to end. Deleting a profile right after
switching away from it failed with a raw `WinError 32`, because Chrome releases
the profile directory asynchronously and its files were still open. One second
later the same call succeeded.

It now retries for a few seconds, and if the directory is genuinely still held
it says so in terms the caller can act on — a browser is still shutting down —
instead of passing back an OS error code.

Tool count: 59 → 59.

## 1.9.2 — the registry blurb still said 57 tools

`server.json` carries the one-line description that directories show, and it had
been left at "57 tools" since 1.8.0. The consistency tests covered the README,
the badge, the tool table and the changelog, but not that string. Now they do,
including the registry's 100-character limit.

Tool count: 59 → 59.

## 1.9.1 — readable message when a click is refused

The 1.9.0 error read "covered by the element is outside the viewport after
scrolling": the template prepended "covered by" to a phrase that was already a
full sentence. Garbled text is worse than cosmetic here, because the agent reads
it to decide what to do next. The page now returns one complete reason and the
message uses it as is.

Tool count: 59 → 59.

## 1.9.0 — click stops claiming success when it hit something else

Found by a user testing whether the thing actually works, on a plain
documentation page. `click` reported `Clicked uid=1_333`; the page never
navigated. Same failure family as the two fixed in 1.8.0, in the one tool that
was still not checking anything.

**Two defects, one symptom.**

- **The CDP path never scrolled.** `scrollIntoView` only ever ran in the
  scripted fallback, so the tool's own description ("scrolled into view
  automatically") was true only when the fast path had already failed. An
  element below the fold was clicked at whatever those coordinates happened to
  land on. It now scrolls with `DOM.scrollIntoViewIfNeeded` first.
- **Nothing checked that the click would reach the element.** A trusted click is
  delivered by coordinate, and a sticky header, a cookie banner or the element's
  own layout can own that pixel. Measured on `docs.pypi.org`: **43 of 54 visible
  links** had a centre point that hit something other than the link.

`click` now hit-tests several points inside the element and aims at one that
actually reaches it, which resolves the ordinary sticky-header case outright.

**When nothing reaches it, the caller decides** — via the new `if_covered`:

- `"report"` (default) does not click at all. It returns an error naming what is
  in the way and leaves the page untouched, so the session stays
  indistinguishable from a person's. Dismiss the banner, close the modal or
  scroll, then click again.
- `"synthetic_click"` dispatches the click on the element directly. It works
  through anything, but the page sees `isTrusted=false` — exactly the signal
  anti-bot systems read. The response says so whenever it is used.

The default deliberately refuses rather than quietly trading away the one
property this server exists to provide.

Tool count: 59 → 59.

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
