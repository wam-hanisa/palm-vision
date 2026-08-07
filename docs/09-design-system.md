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
| Text muted | `#8A8578` | Secondary/meta text |
| Success | `#6B8F71` (moss) | Confirmations |
| Warning | `#8F6B2E` on `#EFE3CE` | Pending-approval states |
| Danger | `#B85C4A` (muted clay red) | Errors — softer than a stock Bootstrap red, stays in-palette |

## Typography

A clean humanist sans (e.g. Inter or Public Sans) — neutral, modern, readable at small sizes on a phone in daylight.

## Layout principles

- Flat surfaces, no drop shadows or gradients — separation comes from a slightly darker card fill against the cream page background.
- Generous whitespace over dense tables where possible — trim to columns people actually scan, detail on click-through rather than crammed into rows.
- Line icons only (no filled/glyph icons), used sparingly as navigation anchors, not decoration.
- Amber accent reserved for one meaning — "needs attention" (pending approvals, sync warnings) — so it stays meaningful instead of becoming wallpaper.

## Two distinct densities

- **Office dashboard** (Manager/Super Admin, mostly desktop): sidebar nav, metric cards, map panel.
- **Field PWA** (PIC Block, phone, sometimes gloves, sometimes sunlight): larger single-column forms, big numeric steppers instead of small text inputs, high-contrast text, an always-visible offline/sync status chip.
