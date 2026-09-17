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
  smoke: "#8b8b93"
  deep-smoke: "#6a6a72"
  ember: "#e5484d"
  ember-text: "#f2686c"
  ember-wash: "rgba(229,72,77,.12)"
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
  pill-action:
    backgroundColor: "{colors.graphite}"
    textColor: "{colors.bone}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "0 15px"
    height: "36px"
  pill-action-hover:
    backgroundColor: "{colors.dry-slate}"
    textColor: "{colors.bone}"
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
- Accent red used 33 times across 1,371 lines of CSS, almost all of it status
- Press feedback is physical: controls scale to 96% on `:active`

## Colors

A functionally monochrome cold-grey system, tinted very slightly blue-violet, with a
single red reserved for signal and three status hues that only backends may trigger.

### Primary

- **Ember** (`#e5484d`): The brand red. Fills the unread-notification badge and the
  subscription "new content" dots in the sidebar. Marks the active nav item's icon (never
  its background), the "catch-up" section eyebrow, progress-bar fills, and range-slider
  tracks. The 26px logo tile used to be an Ember plate behind a drawn glyph; it now holds
  the real artwork (`/assets/openfeed-logo.png`) on an empty background, so Ember's only
  above-the-fold appearances are the notification badge and the subscription dots.
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
  descriptions, channel about-text, channel link lists, `@mentions` in comments, and
  timestamp jump-links. It is the one color the product does not control, because it marks
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
- **Bone** (`#ededef`): All primary text. Also, inverted, the *background* of an active
  filter chip.
- **Smoke** (`#8b8b93`): Secondary text — channel names, view counts, timestamps, resting
  icons.
- **Deep Smoke** (`#6a6a72`): Tertiary text — section eyebrows, placeholders, disabled
  labels. The floor of legibility in this system.
- **Hairline** (`rgba(255,255,255,.09)`) and **Hairline Soft**
  (`rgba(255,255,255,.055)`): Every border in the product. Structural dividers use
  Hairline; incidental seams use Hairline Soft.

### Named Rules

**The Panel Light Rule.** Ember means *something happened here* or *this will destroy
something*. It is never a decorative fill, never a default button background, and never an
active-state background. One exception is permitted: a single filled Ember primary action
per screen, and only where the user is making a real commitment (deleting data, wiping
history, confirming an irreversible import). If two red fills appear on one screen, one of
them is wrong.

**The Read-It-Don't-See-It Rule.** When red must be read as text, use Ember Text
(`#f2686c`), not Ember. Ember is for shapes you notice; Ember Text is for words you read.

**The Borrowed Blue Rule.** Cobalt Link belongs to creator-authored content. Product chrome
never uses it — a blue button would read as someone else's link.

## Typography

**Display Font:** system stack — `-apple-system`, `BlinkMacSystemFont`, `SF Pro Text`,
Inter, `Segoe UI`, Roboto
**Body Font:** the same stack; there is no second family
**Label/Mono Font:** none

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
- **Label** (600, 11px, +0.07em, uppercase): Section eyebrows ("FOR YOU") and field
  labels, always in Deep Smoke.

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

The video grid is intrinsically sized:
`repeat(auto-fill, minmax(max(288px, (100% - 40px) / 3), 1fr))` with `34px 20px` gaps.
The `max()` sets a hard 288px floor while the `(100% - 40px) / 3` term pulls toward
three-up at typical widths — so the grid targets a column count rather than merely
wrapping. Row gap exceeds column gap (34 vs 20) because card titles run two lines and need
the vertical breathing room to separate rows.

Spacing is a fine-grained continuum rather than a scale: 4, 6, 8, 10, 12, 14, 16 all
appear frequently, with 8px and 10px dominant. Sections separate at 40px. This is a
2px-granular system, and treating it as a rigid 8pt grid would be a misreading.

**Responsive.** The primary break is 900px, where `--sidebar` collapses to `0px`,
`--header` drops to `52px`, the desktop sidebar is removed, the header search field is
hidden in favor of a compact control, and nav items reflow from horizontal 38px rows into
60px vertical stacks with 9.5px labels. Nine further breakpoints (1300, 1100, 1000, 760,
720, 700, 560, 430) handle individual surfaces. These are per-surface adjustments, not a
device ladder — do not expect a named breakpoint scale.

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
- **Dialog** (`box-shadow: 0 24px 64px rgba(0,0,0,.7)`): Modal dialogs only. Deeper and
  darker because a modal must separate from an entire page rather than a local panel.
- **Thumbnail lift** (`box-shadow: 0 1px 3px rgba(0,0,0,.5)`): A single hairline shadow
  under overlaid thumbnail badges, so duration text survives a light frame behind it.
- **Status glow** (`box-shadow: 0 0 8px rgba(<status>,.4–.45)`): An 8px halo under health
  dots, in the dot's own color. The only glow in the system, and it is diagnostic.

### Named Rules

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

- **Shape:** Fully round (`999px`), 36px tall, 15px horizontal padding.
- **Primary (pill-action):** Graphite fill, Bone text, 13px/550 with −0.005em tracking, a
  transparent 1px border reserved so the hover state can fill it in without shifting
  layout.
- **Hover / Focus:** Background steps to Dry Slate and the border materializes at Hairline
  Soft, over 140ms. Focus-visible draws `0 0 0 2px rgba(255,255,255,.3)` — a white ring,
  never a colored one.
- **Active:** `transform: scale(.96)` on a `cubic-bezier(.2,.8,.3,1)` curve. This press is
  the system's signature feedback and should be extended to new controls, not dropped.
- **Disabled:** `opacity: .55`, cursor returns to default. No separate disabled color.
- **Icon-only variant:** 36×36 square footprint, still fully round, 17px icon.
- **Danger variant:** Text and icon shift to Ember Text; the fill stays Graphite.

### Chips

- **Style:** Fully round, 34px tall, 14px padding, Graphite fill, 13.5px/500.
- **State:** Hover steps to Dry Slate. **Selected inverts completely** — Bone background,
  Blackout text. This inversion, not color, is how the system marks a chosen filter.

### Cards / Containers

- **Corner Style:** 12px on the thumbnail; the card itself is unboxed.
- **Background:** None. Video cards have no fill, no border, and no padding — they are a
  thumbnail plus text on the page ground. The thumbnail's own 12px radius and hairline
  border provide all the containment.
- **Shadow Strategy:** None. See Elevation.
- **Internal structure:** 16:9 thumbnail over `#000`, then a 3-column row of avatar /
  copy / overflow-menu. Titles clamp at two lines; metadata truncates with ellipsis on one.

### Inputs / Fields

- **Search field:** Fully round, 36px, Cold Ash fill inside a Hairline border, 13.5px text
  at 16px padding, with a 52px-wide submit button divided off by a Hairline Soft left
  border. Hover lifts the fill to Graphite; `:focus-within` brightens the border to
  `rgba(255,255,255,.22)`.
- **Text field:** 8px radius, 36px tall, **Blackout fill** — inputs recede below their
  container rather than rising above it, which is the inverse of the button treatment and
  is deliberate.
- **Focus:** `box-shadow: 0 0 8px`-free; the white 2px ring only.
- **Labels:** 11px/600 uppercase in Deep Smoke, above the field.

### Navigation

- **Sidebar item:** 38px tall, 10px radius, 16px gap between a 20px icon and a 13.5px
  label. Resting icon is Smoke, label is Bone.
- **Hover:** Graphite fill. **Active:** Dry Slate fill, label weight to 500, and the icon
  — and only the icon — turns Ember.
- **Activity dot:** A 5px Ember circle pinned right on subscription rows with unread
  content.
- **Mobile (≤900px):** Becomes a bottom bar; items reflow to vertical stacks, 60px tall,
  with 9.5px labels clipped at 58px width.

### Channel tabs

- **Style:** Text-only, 15px/550, 13px vertical padding, 28px gaps, horizontally
  scrollable with the scrollbar hidden.
- **State:** Resting Smoke → hover Bone → active Bone with a 2px underline. **The
  underline is Bone, not Ember** — consistent with the Panel Light Rule.

### Menu panel

- **Style:** `min(268px, calc(100vw - 24px))` wide, 14px radius, Overlay Ash fill, Hairline
  border, Overlay shadow, 5px padding with a 1px grid gap between rows. Max height is
  `min(620px, calc(100vh - 90px))` with internal scrolling, so a long menu never runs off
  the viewport.

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
- **Do** invert — Bone background, Blackout text — to mark a selected filter.
- **Do** put status color only where a backend is actually reporting status.

### Don't:

- **Don't** use Ember as a background fill for an ordinary control, an active tab, or a
  selected chip. The system marks those with tonal shift and inversion. The single
  permitted exception is one filled Ember confirm action per screen, on a genuinely
  destructive commitment.
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
  `0 0 0 2px rgba(255,255,255,.3)`.
