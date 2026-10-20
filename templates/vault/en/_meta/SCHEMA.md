# Vault schema

This vault is a brain's memory: your notes, pages the agent writes, raw sources and saved answers. The folders below are the vault zones. Code always works on the logical names (`inbox`, `raw`, `calls`, ...), and the block at the bottom of this page says which folder each zone is in this language. If you rename a folder, change the matching line here too: nothing else in the code needs to change.

`raw/` holds other people's words: transcripts, mail, web pages, forwarded files. It is immutable after it lands and counts as untrusted, so a page derived from `raw/` carries `trust: untrusted`. Everything else is trusted unless it is marked otherwise.

Frontmatter is always in English: `type`, `created`, `updated`, `tags`, `domain`, `share`, `sources`, `confidence`, `contested`, `trust`, `status`, `date`, `start`, `participants`, `organizer`, `event`, `audio`, `transcript`, `category`, `parties`, `issued`, `expires`, `original`, `original_sha256`, `project`, `jira`, `due`, `index`, `job`. Folders, file names and prose follow the vault's language instead.

`types` is a closed taxonomy: a new value is added here, with a change that goes through an approval. `tags` lists the broad areas; a tag outside the list is fine when it is lowercase kebab-case (`api-gateway`, `house-hunt`), usually the slug of a topic page. Two tags that say the same thing (singular and plural, with or without a dash) become one. Pages under `archive/` carry `index: false` and stay out of automatic recall and out of the lint.

The pages the agent keeps about itself in `_brain/` are named in `brain_pages`: code and skills use the logical names (`commitments`, `initiative`, ...), so renaming a page is a change here alone.

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
  _brain: _brain
  _meta: _meta
types:
  capture: something captured and not processed yet, still in the inbox
  call: note of a call, with a link to its transcript
  transcript: full transcript of a call
  topic: topic page, updated from calls
  knowledge: knowledge page about a domain
  idea: an idea, with its state
  project: an active project
  listing: an ad or an offer collected during a search
  document: card for a document, with the code of the original
  daily: daily note
  task: a task, with the Jira reference when there is one
  person: one page per recurring person
  answer: a good answer, saved
  drawing: an Excalidraw drawing
  brain: agent memory page
tags:
  - work
  - personal
  - home
  - health
  - money
  - technology
  - study
  - travel
  - sport
  - people
untrusted_zones:
  - raw
brain_pages:
  commitments: commitments
  initiative: initiative
  policy: policy
  positions: positions
  preferences: preferences
  profile: profile
  lint: lint
```
