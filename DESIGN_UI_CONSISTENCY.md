# Common elements of all Sovereign applications

Core owns the shell — header, dialogs, panes — so consistency across
applications is Core's concern rather than each application's. The first
half of this document is the original wish list; the **Decisions** section
below it is what was accepted and built, and is the part to read as
binding.

## Disagreement and Reactions

- Changes waiting for sync could show a small lamp on the top right of the field (like in a card on Kanban)
- highlighting nodes not in agreement + showing explaination text on mouseover
- Reaction buttons, dependent on who made the change and what kind of alignment is there

## Header on top

The top header should be have a consistent format for all apps. (e.g. Agreement lacks the line separating header from content and has a different format/position of elements). Here what a setup could be, left to right:
Left aligned:

- Collaboration icon (where there is a definite topic), opening the collab pane
- Name of selected topic
- pull-down button to select another topic
- Overall status of this topic (in agreement, synced..)

Middle:

- Icon and Name of the app
- Button(s) to navigate to another view or app (e.g. the four quadrant button for the overview)
- A [+] button to create an new topic, opening a modal for specifying what and how (topic specific)

Right aligned:

- a connect-area showing "local" when disconnected or the avatars of people connected with the current topic, clicking on the connect-area opens a right pane to show details of current connected peers, create and enter tokens (channel + current topic) to add/drop connections, to add/edit/delete channels (e.g. relay targets),
- My own avatar - clicking on it opens my avatar settings

## Colour Themes

- I like the idea of a general dark coulor theme (Kanban gray, cockpit blue) what color for agreements ?

---

# Decisions

Status: **U1-U4 accepted and implemented.**

## U1 — chrome is Core's, content is the application's

**ACCEPTED.** The shell's chrome — header, dialogs, panes — looks identical in
every application. Each application owns only its content area: the board, the
document, the overview.

Rationale: the shell was first written colour-neutral so each page could set
the surrounding palette. That guaranteed divergence. The same relay-target
dialog rendered three different ways, because Kanban styles `input`, Personal
Cockpit styles `dialog input`, and S-Agreement styles neither and fell through
to browser defaults. Core owns the functionality, so Core owns the appearance;
otherwise every application must re-style Core's markup, and a minimal one
never will.

Shell colours are exposed as tokens on `.shell-dialog` / `.shell-bar` so an
application *can* re-theme deliberately. Doing nothing yields the same chrome
everywhere, which is the point.

## U2 — reserved colours

**ACCEPTED.** These carry meaning and must not become an application's identity:

| Colour | Means |
|---|---|
| red | divergence |
| amber | in transition |
| teal (`--teal`) | the shared accent, identical everywhere |
| green | reads as "agreed" / success |

An application palette picks from what is left.

## U3 — header layout

**ACCEPTED**, per the layout above. Fixed regions, left to right:

| Region | Holds |
|---|---|
| Left | collaboration button (opens the agreement pane), topic name, topic switcher, topic status |
| Middle | application icon and name, navigation to other applications, `[+]` new topic |
| Right | connect area (peers, or "local"), own avatar |

Notes from implementing it:

- The topic region is filled by the application until Core owns topic
  selection. When Core owns it, the shell fills the same region and nothing
  else moves.
- `[+]` is a shell button with an application handler: a board needs default
  columns, an agreement a title. The button hides itself when an application
  supplies no handler.
- Channel management moved *into* the connection pane, as the layout implies.
  There is no separate "Relay targets" button in the header any more.
- An application showing many topics at once has no single topic status, so
  the left region collapses rather than claiming one.

## U4 — one theme for 0.1

**ACCEPTED: dark everywhere.** Personal Cockpit's dark half was applied
unconditionally, and all three applications now declare `color-scheme: dark`
so native controls, scrollbars, and form widgets match instead of rendering
light on a dark surface.

U1 forced this. Once the shell's chrome became unconditionally dark, an
application that followed `prefers-color-scheme` put a dark dialog on a light
page - measured on a light-mode machine: page `#f4f5f7`, dialog `#161b22`.
Following the machine per application is only coherent if *everything*
follows it, including Core's chrome.

Cost of the alternative, for when this is revisited: four palettes, of which
one existed. Kanban carries about ten tokens in `:root`, the shell seven, and
S-Agreement is mostly literal hex and would need tokenizing first.

Light mode returns as a themed pass across Core and all three applications,
with the open question of whether the machine chooses (`prefers-color-scheme`)
or the person does - a toggle stored in the Core profile would follow the user
across every application, since the profile is already Core.

## Current palettes

| Surface | Colour | Note |
|---|---|---|
| Shell chrome | `#161b22` on `#0d1117` | Core's, identical everywhere (U1) |
| S-Kanban | `#171818` warm gray | |
| Personal Cockpit | `#0d1117` blue-gray | |
| S-Agreement | `#1c1a17` warm ink | frames its light `#f9fafb` document page |

S-Agreement was `#111827` until U2, almost exactly the Cockpit's `#0d1117`, so
two of the three applications looked alike. The warm neutral separates it and
avoids every reserved colour.

## Modal Conventions

- X top-right, click-outside-to-close
