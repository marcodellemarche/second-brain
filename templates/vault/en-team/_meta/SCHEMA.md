# Vault schema

This is the schema of the team brain: one shared vault, written only through pull requests. The folders below are the zones. Code always works on the logical names, and the block at the bottom of this page says which folder each zone is in. If you rename a folder, change the matching line here too: nothing else in the code needs to change.

`raw/` holds other people's words: transcripts, imported pages, forwarded files. It is immutable after it lands and counts as untrusted, so a page derived from `raw/` carries `trust: untrusted`. Everything else is trusted unless it is marked otherwise.

Frontmatter is always in English: `type`, `created`, `updated`, `tags`, `domain`, `share`, `sources`, `confidence`, `contested`, `trust`, `status`, `date`, `start`, `participants`, `organizer`, `event`, `audio`, `transcript`, `category`, `parties`, `issued`, `expires`, `original`, `original_sha256`, `project`, `jira`, `due`, `index`.

`types` and `tags` are closed taxonomies: a new value is added here, in the same pull request that needs it. Pages under `archive/` carry `index: false` and stay out of automatic recall and out of the lint.

This vault has no `_brain/`: agent memory belongs to a person, not to a team. It has no `documents/` either: personal documents never enter a shared repository.

## The four zones a team vault has and a personal one does not

`systems/`, `incidents/`, `decisions/` and `runbooks/` exist because "where is the postmortem" has to have one answer. A personal vault leaves them out entirely rather than carrying empty folders.

- `systems/` one page per component, with what it does, who owns it and where it runs.
- `incidents/` one page per incident, in the postmortem shape.
- `decisions/` one page per decision, with the date and the context it was taken in.
- `runbooks/` reference commands and queries. No secrets, ever, and no customer data.

## How a page gets here

A page is a pull request, never a direct write. The contribution rules are in `_meta/CONTRIBUTING.md`: read them before the first change.

```yaml
language: en
zones:
  inbox: inbox
  raw: raw
  calls: calls
  topics: topics
  knowledge: knowledge
  ideas: ideas
  projects: projects
  people: people
  work: work
  documents: documents
  answers: answers
  drawings: drawings
  archive: archive
  systems: systems
  incidents: incidents
  decisions: decisions
  runbooks: runbooks
  _brain: _brain
  _meta: _meta
types:
  capture: a proposal waiting in the inbox, still a pull request
  call: note of a team call, with a link to its transcript
  transcript: full transcript of a team call, only when everyone in it agreed
  topic: topic page with current status, timeline, open CTAs, questions and sources
  system: one page per component of the platform
  incident: postmortem of an incident
  decision: a decision with its date and the context it was taken in
  runbook: reference commands and queries, no secrets and no customer data
  knowledge: knowledge page about a domain
  idea: an idea, with its state
  project: an active project
  person: one page per colleague or recurring contact
  task: a task, with the Jira reference
  answer: a good answer, saved
  drawing: an Excalidraw drawing
tags:
  - backend
  - frontend
  - api
  - cache
  - kubernetes
  - postgres
  - kafka
  - observability
  - process
  - hiring
untrusted_zones:
  - raw
brain_pages: {}
```
