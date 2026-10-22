# second-brain: rules for coding agents

This repo is **the product**: a self-hosted personal assistant ("brain") built on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent), pinned at `v0.22.2`. It is installed once per brain (for example a personal brain and a team brain). Instance-specific config, tools and skills live in separate instance repos, never here.

Write everything as if this repo were public: code, comments, docs and commit messages in English. No personal data, no company names, no real hostnames, emails or secrets. The model is "any OpenAI-compatible endpoint", never a named provider.

## Architecture in one page

```
channel (Telegram / Slack)
   │
Hermes (container, pinned) ── plugin: brain_guard ──► policy decisions, audit
   │  vault mounted READ-ONLY
   │  MCP (HTTP, bearer token, internal network)
   ▼
brain_tools (container) ── the ONLY writer of the vault, the ONLY holder of credentials
   core tools: vault_write / vault_move / vault_delete / vault_undo, calendar, weather,
               nextcloud_link, archive_store / archive_discard, job_enqueue
   extension API: instance repos add their own tools
sidecars ── vault watcher, call router, job runner, scheduler ─► Hermes webhooks (HMAC)
model_gate ── reverse proxy in front of the model endpoint: holds the API key, refuses
              requests when the watchdog says brain_guard is not loaded
watchdog ── stops Hermes if the brain_guard heartbeat is missing or stale
```

Security model, short version: assume a prompt injection will eventually convince the model. Limit what it can do afterwards with structure (read-only mounts, no credentials in Hermes, egress filtering, terminal in a separate container), then with deterministic rules in code (brain_guard, brain_tools), never with prompts. Classifier-style "injection detection" is never a control.

## Core concepts (use these names everywhere)

- **Zones**: logical vault areas, mapped to localized folder names by `_meta/SCHEMA.md` of each vault. Logical names: `inbox`, `raw`, `calls`, `topics`, `knowledge`, `ideas`, `projects`, `people`, `work`, `documents`, `answers`, `drawings`, `archive`, `_brain`, `_meta`. Code always uses logical names and resolves them through the schema map.
- **Frontmatter keys are always English**: `type`, `created`, `updated`, `tags`, `domain`, `share`, `sources`, `confidence`, `contested`, `trust`, `status`, `date`, `start`, `participants`, `organizer`, `event`, `audio`, `transcript`, `category`, `parties`, `issued`, `expires`, `original`, `original_sha256`, `project`, `jira`, `due`, `index`, and for the call router `participant_emails`, `organizer_email`, `calendar`, `generic`.
- **Effect classes** of a tool: `read`, `internal` (changes only our own data, undoable), `visible` (seen by other people: shared expenses, calendar invites, media requests), `external` (leaves the system: web fetch, questions to another brain, PRs to other repos).
- **Autonomy levels**: `auto` (just do it, audit only), `notify` (do it, report in the daily digest with undo), `ask` (needs an approval bound to the exact call), `never`.
- **Session flags** kept by brain_guard: `untrusted_seen` (the session read mail, web, call transcripts, another brain's answer, or a note with `trust: untrusted`) and `private_seen` (the session read private notes, `_brain`, documents or mail). Children (subagents, delegated runs) inherit their parent's flags and sender.
- **Rule of two**: an `external` call needs `ask` when the session has both `untrusted_seen` and `private_seen`, unless the exact URL or question came literally from the user. A `visible` call is `notify` only when the user asked for it in a session without `untrusted_seen`, otherwise `ask`.
- **Instance policy can only tighten** a tool's default autonomy, never loosen `never`.

## Tool spec (brain_tools extension API)

Every tool, core or instance, declares:

```python
ToolSpec(
    name="weather",
    description="Forecast for a place.",
    params=WeatherParams,  # pydantic model, typed, no free-form passthrough to services
    effect="read",  # read | internal | visible | external
    autonomy="auto",  # default level; instance policy may only tighten it
    hosts=["api.open-meteo.com"],  # egress needed by brain_tools for this tool
    credentials=[],  # names of secrets the tool needs, resolved at runtime
)
```

Instance repos register tools through a Python entry point group `second_brain.tools`.

## Repo layout

```
docs/                 architecture, threat model, configuration, extending
src/second_brain/
  guard/              Hermes plugin (brain_guard): policy engine, session state, budget, sanitizer, audit
  tools/              MCP server (brain_tools): registry, core tools, vault ops, secret scanner
  sidecars/           vault watcher, call router, job runner, scheduler
  gate/               model gate and watchdog
  cli/                `sb` command: init an instance, scaffold a vault, check a deployment
templates/
  vault/{it,en}/      initial vault structure and SCHEMA.md per language
  hermes/             hardened Hermes config, SOUL template
  deploy/             docker compose, egress proxy config
skills/               core Hermes skills (capture, digest, ingest, reminders, documents)
tests/                unit tests per package; tests/security/ for attack scenarios
```

## How to work here

- Python 3.12, `uv` for everything (`uv run pytest`, `uv run ruff check`, `uv run ruff format`). Type hints everywhere, pydantic for config and tool params.
- Small, readable modules. Default to no comments: add one only for a non-obvious why or a security constraint. Never narrate history in comments.
- Every behaviour change comes with tests. Security rules come with a test in `tests/security/` that tries to break them.
- Do not add dependencies without a reason written in the task summary.
- Fail closed: if a policy decision cannot be made (missing config, exception, unknown tool), block.
- Never write secrets, tokens or real personal data in code, tests, fixtures or docs. Fixtures use obviously fake data.
- Stay inside this repo. Do not touch other directories, do not run `git commit` or `git push`, do not install system packages.
- Never search the filesystem outside this repo (no `find /`, no `find ~`). When you need to check a Hermes API, read the Hermes Agent source at tag `v0.22.2`, never a newer one.
- Read only the files you need, and only the relevant parts of large files (use `grep -n` and line ranges).
- If `AGENTS.local.md` exists, read it: it says where the Hermes source and any reference material live on this machine. It is not tracked.
- At the end of each task, print a short summary: files changed, tests run with results, open questions.
