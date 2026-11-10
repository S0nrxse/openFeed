---
name: OpenFeed
description: A near-black instrument panel for your own YouTube feed, where color is reserved for signal.
colors:
  blackout: "#0a0a0b"
  cold-ash: "#121214"
  overlay-ash: "#151517"
  graphite: "#1a1a1d"
  dry-slate: "#232327"
  bone: "#ededef"
  smoke: "#9a9aa3"
  deep-smoke: "#84848d"
  ember: "#e5484d"
  ember-text: "#f2686c"
  ember-wash: "rgba(229,72,77,.12)"
  on-bone: "#0a0a0b"
  cobalt-link: "#6ea8ff"
  live-green: "#3fb950"
  amber-hold: "#d29922"
  hairline: "rgba(255,255,255,.09)"
  hairline-soft: "rgba(255,255,255,.055)"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', Inter, 'Segoe UI', Roboto, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "19px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.016em"
  title:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "17px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.014em"
  body:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "13.5px"
    fontWeight: 500
    lineHeight: 1.45
    letterSpacing: "-0.005em"
  label:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.07em"
rounded:
  xs: "6px"
  control: "8px"
  nav: "10px"
  thumb: "12px"
  panel: "14px"
  pill: "999px"
spacing:
  "2xs": "4px"
  xs: "6px"
  sm: "8px"
  md: "10px"
  lg: "12px"
  xl: "14px"
  "2xl": "16px"
  section: "40px"
components:
  button:
    backgroundColor: "{colors.graphite}"
    textColor: "{colors.bone}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "0 15px"
    height: "36px"
  button-hover:
    backgroundColor: "{colors.dry-slate}"
    textColor: "{colors.bone}"
  button-primary:
    backgroundColor: "{colors.bone}"
    textColor: "{colors.on-bone}"
    rounded: "{rounded.pill}"
    padding: "0 15px"
    height: "36px"
  toggle-on:
    backgroundColor: "{colors.bone}"
    textColor: "{colors.on-bone}"
    rounded: "{rounded.pill}"
    width: "34px"
    height: "20px"
  chip:
    backgroundColor: "{colors.graphite}"
    textColor: "{colors.bone}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "34px"
  chip-active:
    backgroundColor: "{colors.bone}"
    textColor: "{colors.blackout}"
    rounded: "{rounded.pill}"
  nav-item:
    backgroundColor: "transparent"
    textColor: "{colors.bone}"
    rounded: "{rounded.nav}"
    padding: "0 12px"
    height: "38px"
  nav-item-active:
    backgroundColor: "{colors.dry-slate}"
    textColor: "{colors.bone}"
    rounded: "{rounded.nav}"
  search-field:
    backgroundColor: "{colors.cold-ash}"
    textColor: "{colors.bone}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "36px"
  text-input:
    backgroundColor: "{colors.blackout}"
    textColor: "{colors.bone}"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "36px"
  menu-panel:
    backgroundColor: "{colors.overlay-ash}"
    textColor: "{colors.bone}"
    rounded: "{rounded.panel}"
    padding: "5px"
    width: "268px"
---

# Design System: OpenFeed

## Overview

**Creative North Star: "The Quiet Control Room"**

OpenFeed looks like an instrument panel running in a dark room. The interface sits at
near-black and stays there; nothing glows, nothing gradients, nothing competes with the
thumbnails. Every surface is one of four steps on a single cold-grey ramp, separated by
hairlines thin enough to read as seams rather than boxes. The result is a shell that
disappears the moment content loads into it — which is the point, because the content is
video artwork and the chrome's job is to hold it without arguing.

Density is deliberate and high. Type runs small (11–17px across almost the entire
interface) and lands on half-pixel steps — 11.5, 12.5, 13.5, 14.5 — because the system
would rather fit one more row of real information than pad a card. Weights do the same
thing at finer grain than usual, using 550, 650, and 680 where most systems would jump
500 → 600 → 700. This is a tool for someone who uses it every day and already knows where
everything is, so legibility beats spaciousness and scanability beats emphasis.

Color is treated as instrumentation, not decoration. The palette is functionally
monochrome, and the red is a light on a panel: it marks the brand, unread activity,
danger, and failure, and it stays off the rest of the time. A blue appears only where a
human wrote a link. A green and an amber appear only where a backend is reporting its own
health. Nothing in the system is colored because color would look nice there.

**Key Characteristics:**
- Four-step tonal ramp from `#0a0a0b` to `#232327`; depth from value, not shadow
- Hairline borders at 5.5–9% white; no heavy strokes anywhere
- Pill-first geometry — 52 of 144 radius declarations are fully round
- Small, dense type on half-pixel steps with intermediate weights (550/650/680)
- Accent red is almost all status; selection is marked by inverting to Bone, not by color
- Press feedback is physical: controls scale to 96% on `:active`

## Colors

A functionally monochrome cold-grey system, tinted very slightly blue-violet, with a
single red reserved for signal and three status hues that only backends may trigger.

### Primary

- **Ember** (`#e5484d`): The brand red. Its uses are a closed list: progress fills,
  range-slider tracks, unread dots and the notification badge, the active nav icon (never
  its background), the now-playing glyph, the "catch-up" eyebrow, and danger. It marks no
  selected state, no toggle, no active tab and no hover. The 26px logo tile used to be an
  Ember plate behind a drawn glyph; it now holds the real artwork
  (`/assets/openfeed-logo.png`) on an empty background.
- **Ember Text** (`#f2686c`): The text-safe sibling. Used wherever red has to be read as
  words rather than seen as a light — destructive menu rows, remove-buttons on hover,
  failure text in diagnostics. It exists because Ember at 14px on Blackout is too dim to
  read comfortably; this is the lighter tint that clears.
- **Ember Wash** (`rgba(229,72,77,.12)`): The only tinted background in the system.
  Backs failed-status panels. Never a hover state.
- **Brand artwork** (`/assets/openfeed-logo.png`): The logo carries its own orange-to-pink
  gradient. It is the single exception to the flat, gradient-free rule below, and it earns
  that exception by being a fixed asset rather than a style — the gradient never leaves the
  mark. Do not sample colors out of it for UI, and do not add a plate behind it.

### Secondary

- **Cobalt Link** (`#6ea8ff`): Hyperlinks in human-authored content only — video
  descriptions, channel about-text, channel link lists and `@mentions` in comments.
  Timestamps are pills, not links, because a seek is product behaviour. It is the one color the product does not control, because it marks
  text a creator wrote. It never appears in navigation or product chrome.

### Tertiary

Status only. These three are emitted by backend health, never chosen for appearance.

- **Live Green** (`#3fb950`): A healthy playback source; an OK hop in the diagnostic walk.
- **Amber Hold** (`#d29922`): A source in cooldown — degraded but not dead.
- **Ember** (`#e5484d`): A failing source. The same red as the brand, which is
  intentional: the logo and the failure light are the same bulb.

### Neutral

- **Blackout** (`#0a0a0b`): Page ground and the base of every scroll surface. Also the
  fill of text inputs, which sit *below* their surroundings rather than above.
- **Cold Ash** (`#121214`): The first raised step. Search field, quiet panels.
- **Overlay Ash** (`#151517`): Reserved for things that float — menu panels and dialogs.
  A half-step off Cold Ash so an open menu reads as separate from the panel behind it.
- **Graphite** (`#1a1a1d`): Resting state for interactive controls — pills, chips,
  buttons. The "this is pressable" value.
- **Dry Slate** (`#232327`): Hover and active state for those same controls, and the
  background of the active nav item. The top of the ramp; nothing goes lighter.
- **Bone** (`#ededef`): All primary text. Also, inverted over Blackout, the *background* of
  anything selected (chips, segments, a toggle that is on) and of the one primary button
  in a view.
- **Smoke** (`#9a9aa3`): Secondary text — channel names, view counts, timestamps, resting
  icons. At least 5.6:1 on every surface in the ramp.
- **Deep Smoke** (`#84848d`): Tertiary text — section eyebrows, placeholders, disabled
  labels. The floor of legibility in this system: at least 4.5:1 up to `--surface-2`. On
  Graphite (`--surface-3`) it is 4.2:1, so small text there uses Smoke.
- **Hairline** (`rgba(255,255,255,.09)`) and **Hairline Soft**
  (`rgba(255,255,255,.055)`): Every border in the product. Structural dividers use
  Hairline; incidental seams use Hairline Soft.

### Named Rules

**The Panel Light Rule.** Ember means *something happened here* or *this will destroy
something*. It is never a decorative fill, never a default button background, and never an
active-state background. One exception is permitted: the single destructive confirm inside
a dialog (`.btn-danger-fill`), for an irreversible wipe. Ordinary deletes use Ember Text on
Graphite (`.btn-danger`). If two red fills appear on one screen, one of them is wrong.

**The Inversion Rule.** Selection is Bone with Blackout on it. The primary action uses the
same inversion, so at most one `.btn-primary` appears per view. Subscribe, Play all and
Import are the model uses.

**The Read-It-Don't-See-It Rule.** When red must be read as text, use Ember Text
(`#f2686c`), not Ember. Ember is for shapes you notice; Ember Text is for words you read.

**The Profile Swatch Rule.** A household profile is the one thing in the product coloured
to identify a person rather than a state. Its avatar takes one of six muted swatches
(graphite, slate, sage, clay, plum, steel; tokens in `36-profiles.css`), all far below
Ember in chroma and none of them a status hue. A swatch only ever fills a profile avatar
(the header face, the switcher, "Who's watching", Settings → Profiles); it never colours
text, borders, buttons or selection. Selection inside the colour picker is still a Bone ring.

**The Borrowed Blue Rule.** Cobalt Link belongs to creator-authored content. Product chrome
never uses it — a blue button would read as someone else's link.

### Light theme

Dark is the product and the default. Light exists because people asked YouTube for it, and
it is the same system with the ramp turned over, not a second design. YouTube light is the
reference: a white page, near-white raised steps, near-black text and the same Ember.

- **Where it lives:** `00-tokens.css` restates every colour token under
  `:root[data-theme="light"]`, and again under `:root[data-theme="system"]` inside
  `@media (prefers-color-scheme: light)` for Use device theme. `test_theme` keeps the two
  copies identical and fails if a colour token has no light value. Components never test the
  theme; anything that varies with it is a token (`--header-glass`, `--bar-glass`,
  `--border-strong`, `--wash`, `--thumb-bed`, `--avatar-l`, `--select-chevron`, `--ink` for the
  odd `rgba(var(--ink), a)`).
- **The ramp:** White `#ffffff` page and menus, `#f8f8f9`, `#f1f1f3` (controls at rest) and
  `#e6e6e9` (hover). Text `#0f0f10`, Smoke `#5c5c63` (5.3:1 or better on every step), Deep
  Smoke `#686870` (4.4:1 on the hover step, as in dark).
- **The rules hold:** selection still inverts, so Bone becomes near-black with white on it.
  Borders are black at low alpha, and the focus ring is `rgba(0,0,0,.6)`, still one neutral
  ring. Ember Text, Cobalt Link and Live Green darken (`#c92a2f`, `#065fd4`, `#1a7f37`) to
  clear 4.5:1 on white; Ember the light stays `#e5484d`.
- **Video stays dark:** thumbnail badges, the progress rail, the Shorts overlay and rail on a
  phone, and every player document use fixed dark values (`--badge`, `--over-video-*`), as
  YouTube's do. The player chrome does not follow the theme.
- **First paint:** `appearance.head()` puts two `theme-color` metas and a one-line script in
  every `<head>`. The script sets `data-theme` from the profile's saved choice, else this
  browser's mirror (localStorage, then a cookie), else dark, before the stylesheet loads.

## Typography

**Display Font:** system stack — `-apple-system`, `BlinkMacSystemFont`, `SF Pro Text`,
Inter, `Segoe UI`, Roboto
**Body Font:** the same stack; there is no second family
**Label/Mono Font:** no label font. The platform monospace (`ui-monospace`) appears only on
values a machine wrote: token ids, environment names, latencies and raw output.

**Character:** One family doing everything, tuned by weight and tracking rather than
contrast. Because the stack resolves to SF Pro on macOS and Segoe/Roboto elsewhere, the
system is deliberately platform-native rather than branded — it reads like part of the OS,
which suits a tool that runs on your own machine. Negative tracking tightens as size
increases (−0.005em at body, −0.025em at the wordmark), so headings feel set rather than
merely large.

### Hierarchy

- **Display** (650, 22px, 1.25, −0.02em): The largest type in the product. Dialog and
  page titles only; appears twice in the entire stylesheet.
- **Headline** (650, 19px, 1.3, −0.016em): Channel names on channel pages.
- **Title** (600, 17px, 1.3, −0.014em): Section headings on the home feed —
  "Recommended for you", "Continue watching".
- **Card Title** (550, 14.5px, 1.4, −0.008em): Video titles, clamped to two lines with
  `-webkit-line-clamp`. The most-read text in the product.
- **Body** (500, 13.5px, 1.45, −0.005em): Navigation items, chips, search input, general
  interface copy.
- **Dense Body** (500–550, 12.5–13px): Metadata rows, menu items, control labels. The
  workhorse range.
- **Label** (600, 11px, +0.07em, uppercase): The catch-up eyebrow, sidebar and notification
  group labels ("NEW", "EARLIER") and field labels, in Deep Smoke unless it is catch-up.

### Named Rules

**The Half-Step Rule.** The scale moves in half-pixels (11.5, 12.5, 13.5, 14.5) on
purpose. When a new size is needed, take the nearest existing step rather than introducing
a round number — a 14px title next to a 14.5px one reads as a mistake, not a variation.

**The Uppercase Tax Rule.** Uppercase appears only at 11px/600 with +0.07em tracking, and
only for eyebrows and field labels. Uppercase at any other size is not part of this system.

## Layout

A fixed 56px header over a fixed 244px sidebar, with content occupying the remainder.
Both dimensions are tokens (`--header`, `--sidebar`) that the responsive layer rewrites
rather than overriding component-by-component.

The header is a three-column grid — `minmax(150px, 1fr) minmax(0, 560px) minmax(150px, 1fr)`
— which centers the search field optically regardless of how wide the brand or account
cluster grows. It is translucent (`rgba(10,10,11,.86)`) over a
`saturate(160%) blur(14px)` backdrop, so content scrolling beneath it stays faintly
visible instead of hitting a hard edge.

The video grid is `repeat(auto-fill, minmax(var(--grid-min), 1fr))` with `--grid-min: 260px`
and `28px 20px` gaps, which lands on four across at a 1440px window: more real content per
screen. Timeline and playlist grids share the token. Row gap exceeds column gap (28 vs 20)
because card titles run two lines and need the vertical breathing room to separate rows.
The grid stays a grid down to 561px and is one column below that, where Home thumbnails run
edge to edge and the text keeps the 16px gutter.

Home opens with `HOME_LEAD_SIZE` (12) recommendations, then the shelves, then the rest of the
feed. Section headings are one line of Title with no eyebrow; catch-up alone keeps its
eyebrow, because there it is a signal rather than a repeat of the heading.

Recommendation order comes from ranker v2 (`app/openfeed/ranker.py`; README → Ranking).
The first 50 slots of each recommendation run are picked for variety by the ranker
itself (relevance × 0.8 per earlier pick from the same creator, minus topic overlap); the
Home sections' own dedupe and channel spreading still run on top. Each card's reason
("Linked from 3 videos of 2 channels you follow", "Popular and recent") is the `title` of
its metadata line and its `data-reason`, which the feedback menu sends as `shown_reason`. It is a tooltip rather than a visible line: the card's
metadata row stays views and age. A phone has no hover, so the same text also heads the
card's ⋮ menu (and its phone sheet) as a stacked "Why this video" row whose sub-line wraps
to three lines; choosing it repeats the reason in a toast.

A list played from Play all or Shuffle sits above Up next on the watch page as a bordered
panel like the chapter card: the list name (a link back), "N of M" (prefixed "Shuffled ·"),
then numbered rows with 96px thumbnails, the current one on Surface 3 with an ember play
glyph in place of its number.

Spacing is a fine-grained continuum rather than a scale: 4, 6, 8, 10, 12, 14, 16 all
appear frequently, with 8px and 10px dominant. Sections separate at 40px. This is a
2px-granular system, and treating it as a rigid 8pt grid would be a misreading.

**Responsive.** The primary break is 900px, where `--sidebar` collapses to `0px`,
`--header` drops to 52px, the desktop sidebar is removed, the header search field and the
hamburger go, and navigation moves to a bottom bar of height `--tabbar`. At 1100px and below
the sidebar is the 64px rail. At 600px and below, every menu and dialog becomes a
bottom sheet. Further breakpoints (1300, 1100, 1000, 760, 720, 700, 560, 430) handle
individual surfaces. These are per-surface adjustments, not a device ladder — do not expect
a named breakpoint scale.

**Safe areas and viewport units.** `viewport-fit=cover` is on, so `--safe-top`,
`--safe-right`, `--safe-bottom` and `--safe-left` carry the insets. `--header` already
includes the top inset, so anything offset by it stays correct. Use `dvh`, not `vh`, for
anything a phone's browser chrome can resize.

## Elevation & Depth

Flat by construction. Depth comes from a four-step tonal ramp — Blackout → Cold Ash →
Graphite → Dry Slate — combined with hairline borders. A control does not lift when you
hover it; it moves one step up the value ramp. This is why the system reads as a panel
rather than a stack of cards.

Shadows exist for exactly one purpose: things that genuinely float above the page plane.
Menus and dialogs cast them. Nothing else does.

### Shadow Vocabulary

- **Overlay** (`box-shadow: 0 12px 32px rgba(0,0,0,.55)`): Dropdown menus and popovers.
  The `--shadow` token.
- **Dialog** (`box-shadow: 0 24px 64px rgba(0,0,0,.7)`): Modal dialogs only. The
  `--shadow-dialog` token. Deeper and darker because a modal must separate from an entire
  page rather than a local panel.
- **Thumbnail lift** (`box-shadow: 0 1px 3px rgba(0,0,0,.5)`): A single hairline shadow
  under overlaid thumbnail badges, so duration text survives a light frame behind it.
- **Status glow** (`box-shadow: 0 0 8px rgba(<status>,.4–.45)`): An 8px halo under health
  dots, in the dot's own color. The only glow in the system, and it is diagnostic.

### Named Rules

Backdrops are flat: `--scrim`, `rgba(0,0,0,.6)`, with no blur.

**The Ramp-Not-Riser Rule.** To make something feel raised, move it up the tonal ramp
(Graphite → Dry Slate) and let its hairline do the separating. Reach for `box-shadow` only
when the element genuinely overlaps unrelated content — a menu, a dialog. A shadow on an
in-flow card is out of system.

## Shapes

Pill-first. Of 144 radius declarations, 52 are `999px`: every chip, every action button,
every avatar, every badge, the search field. The interface's default silhouette is fully
round, and rectangles are the exception that marks something as a container rather than a
control.

The remaining radii form a short ladder tied to element size — 6px for inline badges, 8px
for text inputs, 10px for navigation rows, 12px for video thumbnails,
14px for menu panels and dialogs. Bigger surface, softer corner.

Borders are always exactly 1px and always white at low alpha; there are no colored borders
in the resting state and no border wider than a hairline anywhere in the product. Video
thumbnails are 16:9 with `object-fit: cover` over a pure `#000` bed, so letterboxed source
art blends into the frame instead of showing grey bars.

## Components

### Buttons

`.btn` is the family. The older names stay as working aliases until every surface has
moved: `.pill-action` is `.btn`, `.pill-icon` is `.btn-icon`, `.text-action` and
`.subscribe-pill` are `.btn.btn-sm`, and `.primary-action` is a 32px `.btn-primary`.

- **Shape:** Fully round (`999px`), 36px tall, 15px horizontal padding. `.btn-sm` is 32px.
- **Default (`.btn`):** Graphite fill, Bone text, 13px/550 with −0.005em tracking, a
  transparent 1px border reserved so the hover state can fill it in without shifting
  layout.
- **Primary (`.btn-primary`):** Bone fill, Blackout text. At most one per view.
- **Ghost (`.btn-ghost`):** No fill, Smoke text; hover steps to Graphite.
- **Danger (`.btn-danger`):** Ember Text on Graphite.
- **Danger fill (`.btn-danger-fill`):** Ember fill, Blackout text. Only as the confirm of an
  irreversible wipe, inside a dialog.
- **Icon-only (`.btn-icon`):** 36×36, still fully round, 17px icon. Add to `.btn` or
  `.btn-ghost`, and give it an `aria-label`.
- **Hover / Focus:** Background steps to Dry Slate and the border materializes at Hairline
  Soft, over 140ms. Focus-visible draws `0 0 0 2px rgba(255,255,255,.5)` — a white ring,
  never a colored one, at least 3:1 against every surface. A field inside a pill leaves the
  ring to the pill, so the square input never draws a box within the round.
- **Active:** `transform: scale(.96)` on `--ease-press`. This press is the system's
  signature feedback and should be extended to new controls, not dropped.
- **Disabled:** `opacity: .55`, cursor returns to default. No separate disabled color.

### Chips

- **Style:** Fully round, 34px tall, 14px padding, Graphite fill, 13.5px/500.
- **State:** Hover steps to Dry Slate. **Selected inverts completely** — Bone background,
  Blackout text. This inversion, not color, is how the system marks a chosen filter.

### Segmented control

`.segmented` holding `.segment` items: a Graphite pill row, 30px segments, selected inverted
to Bone. Works as a radio group (`_segmented(name, options, selected, label="")`) or with
buttons and links marked `aria-pressed="true"`, `aria-current` or `.active`.

### Filter pill

`_filter_menu(label, options, selected, href_for)` renders `.filter-menu`: a chip that opens a
menu of links, so a filter is a URL and needs no script. It reads as its label at the default
and as the chosen value, inverted, once moved off it. Sort and group on the channel list and
sort, date, length and features on search all use it. At phone width it is a bottom sheet like any menu.

### Cards / Containers

- **Corner Style:** 12px on the thumbnail; the card itself is unboxed.
- **Background:** None. Video cards have no fill, no border, and no padding — they are a
  thumbnail plus text on the page ground. The thumbnail's own 12px radius and hairline
  border provide all the containment.
- **Shadow Strategy:** None. See Elevation.
- **Internal structure:** 16:9 thumbnail over `#000`, then avatar and copy. Titles clamp at
  two lines; metadata truncates with ellipsis on one.
- **Actions:** With a mouse, hover or keyboard focus reveals a 32px overflow button and the
  `.card-quick` shortcuts on the thumbnail, and the title takes the freed column. Every
  other pointer keeps the overflow in the title row at `--hit` and has no quick actions, so
  nothing depends on hover. A quick action must repeat a row in the overflow menu.
- **Progress:** `percent_completed` is a 0 to 1 fraction. Below 85% (`_WATCHED_FRACTION`, the
  playback threshold) the card shows a 3px Ember `.progress-rail` flush to the thumbnail
  bottom, and "12m left" replaces the view count. Above it, or when marked, `.watched-mark`
  replaces the bar: a 20px Bone check bottom-left with the artwork at 62% opacity.
  `_progress_rail(video)` and `_time_left_text(video)` draw both.

### Shelves

`_shelf_html` renders a heading row and a horizontal `[data-shelf-track]` on the page ground:
no fill, no border. `.view-all` appears only when a page shows the whole set. Prev and next
are 32px Graphite circles at heading height, revealed on hover or focus, disabled at the
ends, and absent on touch or when everything fits. Columns leave the next card in view and a
44px end fade lands on it, removed at the end of the track or while a menu inside is open,
because a mask would clip the menu. Scroll snap is `x proximity`.

### Inputs / Fields

- **Search field:** Fully round, 36px, Cold Ash fill inside a Hairline border, 13.5px text
  at 16px padding, with a 46px transparent submit divided off by a Hairline Soft left
  border. A `/` keycap shows at rest with a pointer, and a 20px clear button replaces it
  once there is text; both are driven by `:placeholder-shown`. Hover lifts the fill to
  Graphite; `:focus-within` brightens the border to `rgba(255,255,255,.22)`.
- **Type-ahead (`.suggest-panel`):** a menu panel under the field, a combobox listbox. Your
  recent searches lead with a clock icon in Smoke and a remove button; YouTube's
  suggestions follow with a search icon, the typed part plain and the completion bold. In
  the phone takeover the same list replaces the recents while there is text.
- **Search takeover (900px and below):** The Search tab opens `dialog.search-sheet`, a
  full-screen layer with a back button, a 16px field and recent searches kept in
  `localStorage`. Back closes it, and submitting replaces its history entry.
- **Text field:** 8px radius, 36px tall, **Blackout fill** — inputs recede below their
  container rather than rising above it, which is the inverse of the button treatment and
  is deliberate.
- **Select (`.select`, `_select(...)`):** The native control with `appearance: none`, 8px
  radius, Blackout fill, Hairline border and a drawn chevron (`--select-chevron`). The
  page's `color-scheme` keeps the popup in the current theme. A custom popup is not worth the script it would take.
- **Toggle (`.switch`, `_toggle(...)`):** 34×20 over a real checkbox, 16px knob. Off is a
  Graphite track with a Smoke knob. **On is a Bone track with a Blackout knob, never green
  or Ember**: green and amber are backend status only.
- **Range (`.range` in `.range-field`, `_range_field(...)`):** 4px track with an Ember fill,
  16px Bone thumb, and a tabular `<output>` readout that the shared script keeps current.
  `.fc-slider` is an alias. The fill position is `--fill`, set by the same script.
- **Focus:** `box-shadow: 0 0 8px`-free; the white 2px ring only.
- **Labels:** 11px/600 uppercase in Deep Smoke, above the field.

### Navigation

- **Sidebar item:** 38px tall, 10px radius, 16px gap between a 20px icon and a 13.5px
  label. Resting icon is Smoke, label is Bone.
- **Hover:** Graphite fill. **Active:** Dry Slate fill, label weight to 500, and the icon
  — and only the icon — turns Ember.
- **Names:** Subscriptions is the feed of new uploads, Channels is the list of who you
  follow, and Collections is your grouping of them. Each must stay a distinct thing.
- **Subscription rows:** 34px, 22px avatar, 13px name. Channels that uploaded in the last 14
  days lead, newest first, under a "New" label; the rest follow under "Earlier". The order
  is in the query, because the shell only fetches a probe of the first rows.
- **Activity dot:** A 5px Ember circle pinned right on subscription rows with new uploads.
- **Rail:** 64px, icons and avatars, the name in a tooltip and for screen readers. The
  hamburger folds to it and persists above 1100px. From 901 to 1100px the rail is the
  resting state and the hamburger opens the full sidebar over the page, the same way it does
  on Watch and Shorts. `html.nav-rail` is set in the head so the first paint is right.
- **Tab bar (≤900px):** 56px plus the safe area: Home, Subscriptions, Shorts, Search, You.
  Icon 22px, label 10.5px/550, the current tab's icon in Ember. You opens the account menu,
  which lists every destination that has no tab and stays lit on those pages.
- **Filter row:** A sticky 48px bar with a hairline that only appears once it is stuck. The
  chips scroll under a 28px end fade, and the tune button stays pinned outside them. The
  filter lives in the address as `?filter=`.

### Channel rows

`.channel-row` is a subscription in the manager: 56px tall, 40px avatar, name, latest upload,
collection chips, bell, overflow menu. `.channel-result` is a channel in search: 56px avatar and
a Subscribe pill held at Graphite, because a results page would otherwise break the one-primary
rule twenty times. A bell only appears when the caller read the real notification level.

### Channel tabs

- **Style:** Text-only, 15px/550, 13px vertical padding, 28px gaps, horizontally
  scrollable with the scrollbar hidden.
- **State:** Resting Smoke → hover Bone → active Bone with a 2px underline. **The
  underline is Bone, not Ember** — consistent with the Panel Light Rule.

### Menu panel

- **Style:** `min(268px, calc(100vw - 24px))` wide, 14px radius, Overlay Ash fill, Hairline
  border, Overlay shadow, 5px padding with a 1px grid gap between rows. Max height is
  `min(620px, calc(100dvh - 90px))` with internal scrolling, so a long menu never runs off
  the viewport. At phone width it becomes a bottom sheet with no change to its markup.
- **Video card menu order:** play, save to Watch later, save to playlist, mark watched, share; then More like this, Less
  like this, Not interested and Don't recommend channel (both danger rows); then the
  external links. When DeArrow has replaced a card's title, "Show original title" is
  added as the first row, so the original is reachable on touch as well as on hover.

### Notifications

`.notifications-panel` is 360px wide, 520px tall at most. Rows are at least 56px: a 5px unread
dot, title and meta, a 72×40 thumbnail. "New" and "Earlier" group them against the
`notifications_seen_at` watermark. Opening the panel does not clear it; Mark all read does,
so the badge means what it says until you act on it. The footer stays pinned.

### Dialogs

`.feed-dialog`: 14px radius, Overlay Ash fill, Hairline border, Dialog shadow, flat
`--scrim` backdrop.

### Settings rows

Every setting uses one grammar: `_set_row(title, note, control)` or `_toggle(...)` inside
`_set_group(label, rows, foot)`. One Cold Ash panel per group, hairlines between rows, a 56px
minimum, the title and a line of description on the left and the control on the right. Below
600px the control drops under the copy. Long privacy text goes in the group's foot, in full,
next to the switch it belongs to.

The dialog is a 176px rail plus a scrolling column. At 600px and below it is a pushed
sub-screen pattern inside the sheet: `data-settings-view` is `list` or `pane`, Back returns to
the list, and the back gesture does the same through the sheet's cancelable `sheet-back`
event. Open it with `window.ofOpenSettings(paneKey, trigger)`, or with
`data-open-settings-dialog data-settings-target="paneKey"` on any control. Focus returns to
the trigger on close.

Saves are automatic. `saveControl` writes Saving, Saved or Not saved beside the control and
raises a toast. A failure reverts the control and the toast names the cause.

The ranking mode is the one `_select` in Settings → Content (group "Recommendations") and
in the feed controls dialog ("Ranking"): Automatic, Graph, Graph + popularity, Classic. In
Settings it saves on change like the switches; in the feed dialog it saves with the form.
Automatic stores an empty value and follows the shipped default. Its note says what the
topic, discovery and subscription-priority controls do in Graph.

Below it, "Help tune recommendations" is a `_toggle` (`data-setting-explore`, bound with
`bindPreferenceSwitch`, off by default) with a `set-foot` note that says what is recorded and
that nothing leaves the server, the same pattern as the integrations' privacy notes. Its
readout is four `_status_metric` tiles in Status → Personalization (Tuning comparison,
Interleaved impressions, Opens per side, Preference with its 95% interval), shown only while
it is on or once it has data.

### Status

`_status_page_html` is one body for both `/admin/status` and the Settings pane. It opens with
the verdict (one status dot and a sentence that names the state or cause), then a counter
strip, the source ladder, the hop walk, service checks and folded tables. Every number is
measured. No uptime, no history and no percentage that nothing recorded. The hop walk runs
after the panel lands, and a broken hop takes over the verdict's headline.

`.hop-table` in `.hop-frame` is the table for anything checked in order: a row marked
`is-broken` carries Ember Wash, and hops past the first break are `is-skipped`, never passed.
`_raw_output(label, text)` is the disclosure for raw output, with a Copy.

### Bottom sheet

At 600px and below every `.yt-menu` panel and `.feed-dialog` is a sheet: flush to the bottom
edge, 14px top corners, a grab handle, safe-area padding, body scroll locked. Esc, a scrim
tap, the browser back gesture and a downward drag on the handle all close it. Menu rows grow
to 52px. All of this hangs off the `open` attribute in `ui-kit.js`, so a new menu or dialog
gets it for free. Menu panels are promoted to the top layer with `popover` rather than moved
in the DOM, because a `backdrop-filter` on an ancestor such as `.app-header` would otherwise
trap a fixed panel. A popover's `::backdrop` takes no input, so `ui-kit.js` swallows any tap
outside the panel and only closes the sheet; nothing under the scrim is live.

### Menu and dialog focus

At every width, while a `.yt-menu` is open Tab and Shift+Tab cycle through its summary and
rows, and while a modal dialog is open they cycle through the dialog. Escape closes the menu
and returns focus to what opened it: the summary, or the You tab for the account sheet. `?`
opens `.shortcuts-dialog`, a `.feed-dialog` that lists only the keys the current page
handles; add a row there when a page gains a key.

### Account pages

The signed-out pages (`/auth/login`, `/auth/2fa`, `/setup`, invites, reset links) are one
400px Cold Ash card on the page background with the brand top left, like "Who's watching":
title, one line of lede, fields, one full-width primary button, then quiet underlined links.
Errors are an `.acct-notice.is-error` line above the form in Ember Text. Account & security
and Users are ordinary app pages built from the settings grammar (`_set_group`/`_set_row`).
"Confirm it's you" is a banner above the groups rather than a modal. Destructive actions are
`.btn-danger` (Ember Text) on the page and confirm through `ofConfirm`; deleting needs the
username typed. In Users, **Manage** opens the row's actions on a row of their own under the
name. The 2FA QR code stays dark on white in both themes, because that is what a camera reads.

### Page header

`_page_head(title, meta="", actions="")` renders `.page-head`: one line of Display title,
tabular `.page-meta`, and right-aligned `.page-actions`. A `.toolbar` may follow it. No
kicker and no blurb on library and manager pages; eyebrows stay only where they carry
signal, such as catch-up.

### Empty state

`_empty_state(icon, title, body, action_html="")` renders `.empty-state`: an icon name, a
title, one or two lines that say how things get here, and at most one action. Left-aligned
in flow. No illustrations.

### Loading

Skeletons that match the real layout, never spinners. `.skeleton` is Graphite with a 1.6s
shimmer, static under `prefers-reduced-motion`. `_skeleton_cards(n)` goes inside a
`.video-grid` and `_skeleton_rows(n)` inside a `.result-list`; both reuse the real card
classes so they cannot drift. At phone width a loader shows one placeholder, not a column of
four. Hide a list's loader with `window.ofHideLoader(el)`, never `hidden = true`: it holds
the page height so reaching the end of a list cannot move the viewport.

A navigation still running after 250ms draws `.nav-line`, a 2px Bone line under the header,
the same line the player shows while a source loads. `ui-kit.js` handles links and GET forms
on its own; a script that calls `form.submit()` calls `window.ofNavPending()` first.

Thumbnails and avatars sit on Graphite shimmer and fade in over 160ms. This is automatic for
any image directly inside `.thumb`, `.short-thumb`, `.playlist-cover`, `.notification-thumb`,
`.post-video-thumb` or `.avatar`. The text placeholder (`.thumb-fallback`, avatar initials)
stays hidden while an `<img>` exists and appears only after the image has failed and removed
itself, so keep the `onerror` that removes it.

### Library row

`_media_row(..., variant="library")` is the fixed anatomy for history, playlists, liked and
downloads: `--row-thumb` wide thumbnail (168px, 142px in a detail list or Downloads, 120px on
a phone), a two-line title, channel and meta on one line, an optional `note`, then `.row-actions`.
Actions appear on hover or focus and are always there without hover; at phone width the row's
own remove button goes and the same hook lives in the overflow sheet (`menu_rows`). Removal is
held for an undo toast by `ui-library.js`, except deleting a file, which uses `ofConfirm`.
Pair `_library_filter(placeholder)` with `data-filter-item` rows and `_filter_empty(noun)`.

### Toast

`window.ofToast(message, {kind, action, actionLabel, duration})`, or `showToast` inside the
page script. `kind` is `info`, `success` or `error`. Use `success` only for a real backend
acknowledgement, because it carries the green dot. Toasts last 4s, or 6s with an action;
errors stay until dismissed. At most three stack. Bottom-left on desktop, centered above
the tab bar on phones, and above any open dialog. `role="status"`, or `role="alert"` for
errors.

### Confirm

`await window.ofConfirm({title, body, confirmLabel, danger, typed})` resolves `true` only on
confirm. `window.confirm` and `window.alert` are not used. Focus starts on Cancel. The title
names the object and the body names the consequence. `danger: true` is Ember Text on
Graphite, `danger: 'filled'` is the one Ember fill, and `typed` is a phrase that must be
entered before the confirm button enables.

### Player

One flat scrim (`rgba(10,10,11,.72)`), never a gradient. A 3px scrubber thickens to 5px on
hover: Ember played, Dry Slate buffered, Blackout remaining. Controls are 36px, 44px on a
coarse pointer, with mask icons and tabular time. Speed is a one-tap segmented row. Chapter
boundaries are 2px cuts in the scrubber and the hover tooltip names the chapter. Buffering
is a 2px line along the bottom of the frame, not a spinner. None of this reaches a
cross-origin frame: a YouTube embed keeps YouTube's own chrome, and the page cannot read its
clock, so chapters do not follow it and a seek reopens the embed at that time.

Captions are OpenFeed's, not the browser's or video.js's: the tracks run hidden and the
active cue is drawn as Bone 550-weight text on a `rgba(10,10,11,.8)` plate with a 6px radius,
sized `clamp(13px, 2.3vw, 34px)` to the frame, near the bottom edge and lifted above the bar
whenever it shows. The CC button sits where video.js's captions button was (which is
hidden); "on" is a 2px Bone rule under the glyph, never Ember. Its menu is the player menu
panel with Off first, one row per language and "Auto-generated" in Smoke beside automatic
tracks; on a phone frame it anchors to the bar's right edge like the speed row. While it is
open the bar stays up and the neighbouring hover menus stand down. The cast button shares
the treatment and only exists when the browser reports a receiver and the source is a plain
URL. All of this lives in `assets/player/media.css` and `assets/player/kit/media-kit.js`.

The speed button only opens and closes its row (video.js would step the rate on a press,
which with hover-open menus set 1.25x before anything was chosen). The quality cog is named
"Quality: <choice>" and its menu marks the choice, with Auto meaning every rendition enabled.
Shortcuts are caught in the capture phase on the embed window (`assets/player/kit/media-kit.js`,
`setupKeys`), because video.js swallows keys pressed on its own controls; the watch page
forwards the same keys when it has focus (`assets/js/watch-media.js`).

A source that buffers but never plays gets a notice over the frame after 12 seconds of
asked-for playback: the source's name, "Try <next source>" and "Keep waiting", on the same
scrim and action pills as the failure card. With no source left it opens the failure card.

Failure is the frame's content, never a dialog. The failure card names the cause in Ember
Text over an Ember Wash panel, from `/api/playback/diagnose` when the chain is at fault and
from the source's own result when it is not. It carries the four-hop strip (status dots:
Live Green, Ember, hollow for not reached), one row per source with its result and time,
remedies, and Retry, Next source, Open diagnostics, Copy report.

### Chat panel

With Live chat on, watch pages carry a chat card at the top of the side panel, built from the
chapter card (`.chapter-card.chat-card`, collapsible summary). It stays hidden unless the
video has live chat or a replay. Rows are a 24px initials avatar (the same hue hash as
channel avatars; no photos are loaded), the author in Smoke at 550 (Bone for the creator and
moderators), small Graphite pills for Creator, Mod and Member, a Bone pill for a paid
amount, and the text. The list is a fixed-height scroller that follows the bottom unless the
reader has scrolled up. The state beside the title (Live, Loading, Partial, Ended) is plain
Smoke text; the foot says it is read-only and server-fetched.

### Touch

`--hit` is 32px on a fine pointer and 44px on a coarse one. Pills and chips keep their
designed size and take the touch through an invisible `::after`; icon buttons and menu
triggers size themselves from `--hit` directly.

## Implementation

Styles live in `app/openfeed/assets/css/`, joined in filename order, so the numeric prefix is
the cascade order. `_home_styles()` returns the joined text and picks up edits without a
restart. Shared behaviour lives in `app/openfeed/assets/js/` (`ui-kit.js`, `shell.js`, and the library surface's
`ui-library.js`, `ui-profiles.js` for Settings → Profiles, `accounts.js` for Account & security and Users, and `theme.js` for Appearance), which run ahead of the page script in `_home_script()`. Both bundles are served from `/assets/shell/<hash>` with
a one-year immutable cache, and the watch player iframe keeps its own styles.

| File | Holds |
| --- | --- |
| `00-tokens.css` | Every shared value |
| `10-base.css` | Reset, icon, focus, scrollbars |
| `20-shell.css` | Header, sidebar, rail, page frame, chips, avatars, search takeover |
| `25-feed.css` | Sections, shelves, video cards, card actions |
| `27-search-home.css` | Type-ahead, In your playlists, Watch later state, Not interested, pull to refresh, Back to top |
| `30-components.css` | Menus, dialogs, buttons, controls, skeletons, toast, confirm, touch |
| `35-settings.css` | Settings dialog, source list, pickers |
| `36-profiles.css` | Profile avatars and swatches, "Who's watching", the profile switcher, Settings → Profiles |
| `38-accounts.css` | Signed-out pages (sign in, two-step, setup, invites, reset), Account & security, Admin → Users |
| `39-signin-methods.css` | Passkey buttons and notes, the Passkeys group in Account & security, provider and proxy buttons (`passkeys.py`, `oidc.py`, `trusted_header.py`) |
| `37-appearance.css` | The account menu's Appearance page (the light tokens are in `00-tokens.css`) |
| `40-library.css` | Generic pages, timeline, channel rows, collections, playlists |
| `50-watch.css` | Shorts, notifications, comments, watch page, reactions |
| `60-channels.css` | Channel page, search results, filter pills |
| `65-feed-controls.css` | Feed controls dialog |
| `70-posts.css` | Community posts |
| `75-status-import.css` | SponsorBlock, status, Takeout, setup checklist, downloads |
| `80-watch-detail.css` | Theater, replies, description, tabs |
| `90-responsive.css` | Breakpoints and phone sheets. Always last |

`assets/player/kit/media-kit.js` (captions, Media Session, cast) is loaded only by the player
shell, which attaches it to whichever same-origin player document it mounted.
`assets/player/` is a separate bundle for the player documents, which never load the app
stylesheet. `chrome.css` is injected into the same-origin Invidious embed and inlined into
the `/local` and `/ytdlp` pages, whose `native.js` control bar uses video.js class names so
one stylesheet draws every player. `_player_script()` pastes it into a template literal, so
it holds no backticks, backslashes or dollar-brace sequences.

## Do's and Don'ts

### Do:

- **Do** move up the tonal ramp for emphasis — Graphite at rest, Dry Slate on hover — and
  let the 1px hairline do the containing.
- **Do** reach for `999px` first. If a new control is not a container, it is probably a
  pill.
- **Do** carry the `scale(.96)` press onto any new pressable control, paired with a
  `prefers-reduced-motion` guard that disables both the transition and the transform. The
  stylesheet already has nine such guards; match the pattern.
- **Do** use Ember Text (`#f2686c`) whenever red needs to be read as words.
- **Do** keep new type on the existing half-pixel steps (11, 11.5, 12, 12.5, 13, 13.5,
  14.5, 17, 19) and the existing weights (500, 550, 600, 650).
- **Do** invert — Bone background, Blackout text — to mark a selected state or the one
  primary action in a view.
- **Do** use the shared toast, confirm, skeleton and empty-state components rather than a
  local variant.
- **Do** put status color only where a backend is actually reporting status.

### Don't:

- **Don't** use Ember as a background fill for an ordinary control, an active tab, or a
  selected chip. The system marks those with tonal shift and inversion. The single
  permitted exception is the destructive confirm inside a dialog.
- **Don't** color a toggle, or use green or amber for anything a backend did not report.
- **Don't** add a spinner. Show a skeleton that matches the layout.
- **Don't** add `box-shadow` to anything that sits in normal flow. Shadows are for menus
  and dialogs.
- **Don't** introduce a second font family. The system is one platform-native stack tuned
  by weight and tracking.
- **Don't** use Cobalt Link (`#6ea8ff`) in product chrome — it means "a creator wrote
  this."
- **Don't** put borders wider than 1px, or borders in any color other than white at low
  alpha, in a resting state.
- **Don't** give video cards a fill, a border, or padding. The thumbnail is the container.
- **Don't** assume an 8pt grid. Spacing here is 2px-granular, and 6px/10px/14px are
  first-class values.
- **Don't** add a colored focus ring. Focus is always
  `0 0 0 2px rgba(255,255,255,.5)`.
