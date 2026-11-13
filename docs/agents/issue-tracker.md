# Issue tracker: Local Markdown (in the Obsidian vault)

Issues, specs and wayfinder maps for this repo live as markdown files in `vault/maps/`. You can see and edit them in Obsidian.
This is the stock local-markdown tracker with the root moved from `.scratch/` to `vault/maps/`.

## Conventions

- One feature or effort per directory: `vault/maps/<feature-slug>/`
- The spec is `vault/maps/<feature-slug>/spec.md`
- Implementation issues get one file per ticket, at `vault/maps/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`. Never put several tickets in one combined file.
- Triage state is a `Status:` line near the top of each issue file (see `triage-labels.md` for the role strings).
- Comments and conversation history are appended to the bottom of the file under a `## Comments` heading. Slack thread replies on a ticket are appended here too, prefixed with `(slack)`.

## When a skill says "publish to the issue tracker"

Create a new file under `vault/maps/<feature-slug>/`, creating the directory if needed.

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user normally passes the path or the issue number directly.

## Wayfinding operations

`/wayfinder` uses these. The **map** is a file with one **child** file per ticket.

- **Map:** `vault/maps/<effort>/map.md` holds the Destination / Notes / Decisions-so-far / Not-yet-specified / Out-of-scope body.
- **Child ticket:** `vault/maps/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body.
  - A `Type:` line records the ticket type: `research`, `prototype`, `grilling` or `task`.
  - A `Status:` line records `claimed` or `resolved`.
- **Blocking:** a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier:** scan `vault/maps/<effort>/issues/` for files that are open, unblocked and unclaimed. The lowest number wins.
- **Claim:** set `Status: claimed` and save before doing any work.
- **Resolve:**
  1. Append the answer under an `## Answer` heading.
  2. Set `Status: resolved`.
  3. Append a context pointer (gist plus `[[wikilink]]`) to the Decisions-so-far section of `map.md`.
