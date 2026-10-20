# Contributing

This vault is a shared repository. Everything in it is a pull request, and the rules below are what a reviewer checks.

## Who can change what

| Area | Who writes | Approvals |
|---|---|---|
| `runbooks/`, `decisions/` | anyone on the team | two |
| `systems/`, `incidents/` | the owning team | one, from someone who was there |
| `topics/`, `calls/` | the brain, as a proposal | one |
| `knowledge/`, `ideas/` | anyone | one |
| `_meta/` | a maintainer | two |

The brain opens the pull request and says which kind it is. It never merges.

## What never enters

- A secret, of any kind, in any form. The scanner refuses the write and the reviewer looks anyway.
- A customer name in a `runbook/` or `systems/` page. A customer case is a `topics/` page, and only if the customer cannot be identified from it.
- Personal data of a colleague: a `people/` page says what someone works on, not where they live.
- A transcript, unless every participant agreed to it. A transcript carries `trust: untrusted`, and so does every page derived from one.
- A page that quotes a private conversation. If it matters, it becomes a `decisions/` page that says what was decided, without the room.

## How a page reads

- One page, one thing. A page that answers two questions is two pages.
- The first paragraph says what the page is for, in plain words, before any detail.
- A `runbook/` page says what it is for, the exact commands, and what to check afterwards. It says nothing about who ran it or why.
- A `topics/` page keeps a timeline, oldest first, with dates. A status that changed is a new line, not a rewrite of the old one.
- A `decisions/` page says what was decided, when, what the alternatives were, and what would make us revisit it.
- An `incidents/` page follows `incidents/_TEMPLATE.md`.

## What the brain does on its own

It writes `topics/` pages and proposes `calls/` notes. Everything else it proposes as a pull request, and the reviewer decides. It never merges, never force-pushes, and never writes to `main`.
