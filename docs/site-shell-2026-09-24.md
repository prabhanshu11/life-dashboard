# Site shell (lane site-shell-0924, 2026-09-24)

His ask (2026-09-24 20:3x IST): "Also consider the entire website and bind it
together, right now, the /day lives differently than the daily dashboard,
financial dashboard, pomodoro etc."

## 1. Audit: every page before the shell

Where it is served: only the **desktop** (`life-dashboard.service`, user unit,
port 8090, http://100.92.71.80:8090 over Tailscale/LAN). The Pi/VPS route is
dormant: the Pi has no copy, and `life.prabhanshu.space` now answers with a
different app (a 302 to a "Tradesight" login), so no page of this repo is on
the VPS.

| Route | Template / source | How it was reached | Look before the shell |
|---|---|---|---|
| `/` (Home panel, `#home`) | `pi/templates/dashboard.html`, read as a file | typed URL; left icon rail | Dark "command center" (#080810, purple accent), Space Grotesk requested via a Google Fonts `@import` placed after other rules, full viewport, 56 px left icon rail with emoji icons and hover tooltips, no light mode |
| `/#time` | same file, panel `#page-time` | rail icon / key 2 | Amber-on-black "memento mori", mono |
| `/#habits` | panel `#page-habits` | rail / key 3 | Green terminal RPG, mono |
| `/#cal` | panel `#page-cal` | rail / key 4 | Light "paper planner", Playfair Display serif (the only light panel) |
| `/#cam` | panel `#page-cam` | rail / key 5 | Green surveillance monitor |
| `/#fin` (finance dashboard) | panel `#page-fin` | rail / key 6 | Orange-on-black Bloomberg terminal, mono |
| `/#tv` | panel `#page-tv` | rail / key 7 | Content / TV panel |
| `/day` | `pi/templates/day.html`, read as a file | typed URL only (the dashboard had no link to it) | Paper-and-ink light theme with a dark theme by system preference (no toggle), Geist/system sans, centred 1280 px column, its own top-right text nav (Calendar, Day, struck-out Week/Month, Camera ↗) |
| `/week`, `/month` | inline HTML strings in `calendar_api.py` | only by URL | Unstyled one-line stubs, 16 px system-ui, no nav |
| Camera site `:8100/` | star-trek-camera (other repo) | /day's "Camera ↗" link | Its own dark Courier console; out of this repo |
| Pomodoro | **does not exist** | - | Searched this repo, every repo under `~/Programs`, the desktop's `~/Programs` and its listening ports: no pomodoro/focus-timer page anywhere. Not linked. |

Three unrelated systems, then: the dashboard (seven art-directed dark panels
behind an icon rail), /day (a light-first document page with its own nav), and
bare stubs. Nothing linked the dashboard to /day, the fonts and widths
differed, and only /day had a light/dark system.

## 2. The shell

One header on every page, one token file.

- `pi/static/shell.css`: the tokens (colour, type, spacing, radius, widths)
  plus the header. The palette is /day's paper-and-ink system, light and dark,
  because it is the only page that already had both; accent #b9532a / #e0805a.
  Font: /day's system sans stack (no web-font download; the site must work
  on the LAN without internet).
- `pi/templates/_shell_header.html`: site name "Life OS", one nav for every
  section: the seven dashboard panels (Home, Time, Habits, Calendar, Eye,
  Finance, Content; same order as the dashboard's 1-7 keys), a hairline, then
  the camera record (Day, Week, Month). Right side: a per-page action slot
  (the dashboard puts "Full screen" there), the camera-site link, and the
  theme button (Auto / Light / Dark, saved in `localStorage` as
  `lifeos-theme`, drives `<html data-theme>`, which /day already honoured).
  The current section is marked with `aria-current="page"`, from the path or,
  on the dashboard, the `#hash` (updates on hashchange).
- `pi/templates/_shell_head.html`: the stylesheet link (cache-busted by file
  mtime) and a pre-paint theme script (no flash).
- Width: document pages (/day, /week, /month) keep the 1280 px centred column
  with the `clamp(16px, 4vw, 56px)` gutter; the header sits in that column.
  The dashboard is a full-viewport app, so its header spans the viewport with
  a 24 px gutter matching the panels.
- Rendering: `render_page()` in `calendar_api.py` fills two HTML-comment slots
  (`<!--shell:head-->`, `<!--shell:header-->`). No Jinja: the desktop unit runs
  `uv run --with fastapi ...` without jinja2, and the pages' JS uses `${...}`
  freely. `/static` is now mounted.

## 3. Per-page wrap method

| Page | Method |
|---|---|
| `/` dashboard (all 7 panels incl. finance) | Wrapped: slots added; the left icon rail (and its CSS) removed, replaced by the shell nav (`/#fin` etc. go through the existing hashchange router, so no JS change). Body became a column so the panels fill the space under the header. Panel content and ids untouched; `.nav-item` lookups in `goPage()` are null-safe. Fullscreen moved to the header's action slot; the F key still works. |
| `/day` | Wrapped: slots added; its own nav removed (Calendar/Day/Week/Month/Camera now live in the shell; `#camlink` moved into the shell with the same id, so /day's script still corrects it). Body top padding 28 px -> 0 so the header sits at the top like every other page. The tracking section (record, totals, tables) is untouched for the parallel `day-tracking-view-0924` lane. |
| `/week`, `/month` | Moved from inline Python strings to one small template `stub.html` rendered in the shell. |

## 4. What is not bound (yet)

- The dashboard panels keep their own art direction (amber memento mori,
  green terminal, paper planner, Bloomberg orange). They sit under the shared
  header but do not follow the Light/Dark button. Re-skinning seven panels to
  the shell tokens is a content redesign, not a shell; it needs his call.
- `day.html` still defines the same colour tokens itself (identical values to
  `shell.css`) and has now-unused `nav` rules. Fold them into the shell after
  the tracking lane merges, to keep that merge clean.
- Pre-existing, not from this lane: opening `/#cal` directly throws
  `Cannot access 'CAL_START' before initialization` (the router runs before
  `CAL_START` is declared). Seen on the live desktop build too.
- Pomodoro: nothing to link (see audit).
