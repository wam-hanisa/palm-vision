# UI/UX design system — "agriculture modern minimalism"

Warm, natural, and quiet rather than a blue-tinted admin-template look. Reads as agricultural without literal icons (no cartoon plants); stays legible outdoors on a phone screen, since that's where it's used half the time.

## Color palette

| Token | Value | Use |
|---|---|---|
| Background | `#FAF8F3` (warm cream, not cold white) | Page background |
| Card surface | `#F0EDE2` | Cards, panels |
| Primary | `#2F4A38` (deep forest green) | Sidebar, primary actions, heading accents |
| Primary hover | `#3E6049` | Buttons, active states |
| Accent | `#D9A441` (harvest amber) | Used sparingly — pending/approval states and highlights only, never a dominant color |
| Text primary | `#2B2A25` (warm charcoal, not blue-black) | Body text |
| Text muted | `#656258` | Secondary/meta text |
| Success | `#5E7E63` (moss) | Confirmations |
| Warning | `#816029` on `#EFE3CE` | Pending-approval states |
| Danger | `#A65343` (muted clay red) | Errors — softer than a stock Bootstrap red, stays in-palette |

### Why four of these differ from the original draft

The surface colours above (background, card, primary, accent) are unchanged.
The four **text** colours were darkened after the first real browser run: used
at the 12–18px sizes they actually appear at, the original values failed WCAG
AA against the backgrounds they sit on — muted text measured 3.47:1 on cream
and 3.14:1 on the card fill, danger 4.24:1, warning 3.84:1, and white on the
original moss 3.63:1 (that one is the field PWA's sync chip, the single
element that most has to stay readable in direct sunlight).

Each is the same hue moved by the minimum amount that clears 4.5:1 against
every background it actually sits on, so the palette reads the same while
meeting this document's own stated goal — "stays legible outdoors on a phone
screen." Both surfaces (`apps/dashboard/static/dashboard/style.css` and
`pwa/css/app.css`) carry these values; they are the source of truth, and the
figures above were measured on rendered pixels rather than derived on paper.

### Map marker fills (Farm Map)

Tree markers encode two things at once, and both need a non-colour channel:

| State | Fill | Note |
|---|---|---|
| Healthy | `#99B29D` | lightest |
| Not yet assessed | `#8A8578` | mid |
| Yellowing | `#9B4D3E` | darkest — reads as "needs attention" |

These are deliberately separated on the **lightness** axis, not only by hue.
The `Success`/`Danger`/`Text muted` values used directly as fills collapsed in
greyscale — healthy against not-yet-assessed measured 1.015:1, meaning a
red-green colourblind viewer, or anyone on a washed-out screen in the field,
saw the two as identical markers. They now separate at 1.61:1 or better.

Harvest/fertilizer stage is drawn as the marker's **ring**, and carries a
distinct stroke pattern per state as well as a hue (solid-thick = needs
action, long-dash = overdue, short-dash = due soon, sparse-dots = never
recorded, solid-thin = OK). Colour alone is never the only signal: "overdue"
and "due soon" are red and amber, the pair red-green colourblindness
collapses, and they mean "act now" versus "act this week".

## Typography

A clean humanist sans (e.g. Inter or Public Sans) — neutral, modern, readable at small sizes on a phone in daylight.

Public Sans is **vendored into the repository** as a variable woff2 (latin
subset, ~26KB) and served same-origin, not linked from a font CDN: the field
PWA has to render correctly with no network at all, and a CDN `<link>` would
silently fall back to a system font in exactly that case. Both surfaces lead
with `"Public Sans"` in the same order — they previously disagreed, so the
same machine could render the dashboard and the PWA in different typefaces.

## Layout principles

- Flat surfaces, no drop shadows or gradients — separation comes from a slightly darker card fill against the cream page background.
- Generous whitespace over dense tables where possible — trim to columns people actually scan, detail on click-through rather than crammed into rows.
- Line icons only (no filled/glyph icons), used sparingly as navigation anchors, not decoration.
- Amber accent reserved for one meaning — "needs attention" (pending approvals, sync warnings) — so it stays meaningful instead of becoming wallpaper.

## Two distinct densities

- **Office dashboard** (Manager/Super Admin, mostly desktop): sidebar nav, metric cards, map panel.
- **Field PWA** (PIC Block, phone, sometimes gloves, sometimes sunlight): larger single-column forms, big numeric steppers instead of small text inputs, high-contrast text, an always-visible offline/sync status chip.
