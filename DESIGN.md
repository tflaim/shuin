# Shuin

Neon-noir control room. One vermilion seal. The rest of the night stays dark.

## Color

Near-black ground. Vermilion is the only hero. Cyan and magenta are status lamps, used once per signal, never as fills.

| Token | Hex | Use |
| --- | --- | --- |
| night | `#09090b` | Page ground |
| panel | `#101114` | Confirmed strip |
| paper | `#f4efe9` | Primary text |
| ash | `#d9cdc6` | Secondary text, pending seal stroke |
| dim | `#b7a8a2` | SHAs, clocks, hints |
| seal | `#ff3b2f` | Brand, stamped seal, queue index |
| cyan | `#5ef2ee` | Running lamp, done lamp, needs-rebase chip |
| magenta | `#ff4ec8` | Review lamp, blocked lamp, sample banner, agent-claim tag |

Hairline rules are paper at 14% opacity. The blocked bar is `#050505` with a thin vermilion edge.

## Type

- Headings: Shuin Display, Big Shoulders Display 700, condensed, tight tracking. Embedded as woff2.
- Data: Shuin Mono, JetBrains Mono 400. Task ids, SHAs, queue positions, lease time.
- Marks: small serif kanji (`朱`, `列`, `帳`, `証`, `重`, `阻`, `査`, `確`) and the kanji kicker `朱印`. These are labels, not decoration in the body.

## Seals

A seal sits on the left of every card. Ink is displaced with a light turbulence filter so the edge looks pressed, not drawn.

- Stamped: filled vermilion double ring, `朱` inside, slight rotation, soft red glow. Voice: `Stamped.` Shown only when the verifier passed and the Seal holds, so the Board never stamps what `shuin task done` or merge closing would refuse. `shuin stamp` and a verifier releasing the slot with `pass` answer the same way: `Stamped.` when the Seal holds, otherwise `Stamped. Not sealed yet: <next command>` with the command the card names, for example `Stamped. Not sealed yet: shuin seal --pr owner/repo#N`.
- Pending: hollow dashed ash ring. Voice: `Awaiting the verifier.` When the verifier passed but the Seal does not hold, the card stays pending and its voice is the Seal refusal with its next command: `Seal is missing. shuin seal --pr owner/repo#N` before a seal runs, or `PR is already merged. shuin task done T-0001 --merged-externally --reason landed` on a merged PR.
- Stale: vermilion ring broken into uneven dashes, a crack through the face. Voice: `Seal broken: head moved.` when a seal receipt exists and the head moved. Voice: `Stale verification.` when the verifier or evidence commit is not the head and there is no such receipt. A merged pull request is not stale.
- Blocked: a black bar. Voice: `Blocked.`

Page voice follows the worst card: a broken seal wins (`Seal broken: head moved.`), then stale verification (`Stale verification: N.`), then anything still waiting on the verifier, then a block, then a stamp. An empty ledger says `No cards. The night is quiet.`

## Board

`shuin render` and `GET /` share one static page. Dense two-column desk: queue on the left, cards on the right, then evidence, overlaps, blocked, and quoted claims. Confirmed GitHub cache sits in a darker strip under the desk. A 1px scanline sits over the page and does not catch clicks. Running lamps pulse; reduced-motion turns that off. The layout stacks under 860px.

Claims are escaped, prefixed, and tagged `agent claim`. Evidence URIs are text, not links. A sample night prints `Sample night. Not a live sync.` in magenta.

## CLI

`shuin queue`, `shuin board`, and `shuin seal-status` print the same deadpan lines in monospace. A `朱` marks the queue and the board voice. No color. `--json` is the machine path.
