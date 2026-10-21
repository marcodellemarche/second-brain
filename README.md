# second-brain

A self-hosted assistant that keeps your notes, calls, documents and commitments in a plain Markdown vault, answers you on Telegram or Slack, and works on its own at night. It runs on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) (pinned at `v0.22.2`) with any OpenAI-compatible model endpoint.

The project is built around one assumption: sooner or later a prompt injection will convince the model. So the question is not how to stop that, but what the model can do afterwards. The answer here is "very little, and nothing you cannot undo", enforced by the shape of the deployment and by deterministic code, never by a prompt.

## What it does

- Write a thought, forward a message, send a voice note or a photo of a document: it lands in the vault inbox with its source, and the nightly triage files it in the right page.
- A recorded call dropped in the inbox is split into a summary, the raw transcript (kept untrusted) and the commitments it contains, which become calendar reminders.
- A morning digest with weather, calendar, what is due and what the brain did overnight (with undo hints), and a brief before meetings with other people.
- Research questions, ideas to develop and open questions found in the vault go to a job queue and come back as notes.
- A nightly lint pass finds orphans, broken links, frontmatter outside the schema and contradictions.
- Every write the brain makes is a git commit in the vault, and `vault_undo` reverts it.
- One install per brain: a personal one, a team one. Each instance repo adds its own tools and skills through a small extension API.

## How it stays safe

```text
channel (Telegram / Slack)
   │
Hermes (container, pinned) ── plugin: brain_guard ──► policy decisions, audit
   │  vault mounted READ-ONLY
   │  MCP (HTTP, bearer token, internal network)
   ▼
brain_tools (container) ── the ONLY writer of the vault, the ONLY holder of credentials
sidecars ── vault watcher, call router, job runner, scheduler ─► Hermes webhooks (HMAC)
model_gate ── holds the model API key, refuses requests when brain_guard is not loaded
watchdog ── stops Hermes if the brain_guard heartbeat is missing or stale
```

Structure comes first. Hermes sees the vault read-only and holds no credentials. The only way to change anything is a call to `brain_tools`, a separate container with a typed tool for each action. The shell runs in its own container, the internet is reachable only through an egress proxy with an allowlist, and no port is published.

Then deterministic rules, in `brain_guard`, a Hermes plugin that sees every tool call:

- Every tool declares an effect: `read`, `internal` (changes only our own data, undoable), `visible` (other people see it, like a calendar invite or a shared expense) or `external` (leaves the system, like a web request).
- Every tool has an autonomy level: `auto`, `notify` (done, then reported in the digest with undo), `ask` (needs an approval bound to that exact call) or `never`. An instance can only make these stricter.
- The session remembers whether it has read untrusted content (mail, web pages, transcripts, notes marked `trust: untrusted`) and private content. With both, an `external` call needs an approval unless the exact URL or question came from you. This is the "rule of two".
- If a decision cannot be made (unknown tool, missing config, an exception), the call is blocked.

Classifier-style "injection detection" is not used as a control. The full reasoning, the attacker paths and what stays uncovered are in [docs/threat-model.md](docs/threat-model.md).

## The vault

The vault is a folder of Markdown files with YAML frontmatter, and it is the source of truth: nothing important lives in a database. You can open it in Obsidian, sync it with Nextcloud, or read it with `cat`.

Code always speaks of logical zones (`inbox`, `raw`, `calls`, `topics`, `knowledge`, `ideas`, `projects`, `people`, `work`, `documents`, `answers`, `archive`, ...) and each vault maps them to folder names in its own language through `_meta/SCHEMA.md`. Frontmatter keys are always English. Templates ship for an English vault, an Italian vault and a team vault (`templates/vault/`).

Optionally the brain pushes the vault into a folder of a git repository, and pulls from it too, so a team can edit the same pages on GitHub.

## Quick start

You need Docker, Python 3.12 and [uv](https://docs.astral.sh/uv/), a Telegram bot or a Slack app, and an OpenAI-compatible model endpoint.

```sh
uv sync
# write my-answers.yaml, starting from the sample in docs/configuration.md
uv run sb init ~/my-brain --answers my-answers.yaml
```

`sb init` renders the whole instance: the Hermes config, the compose file, the egress allowlist, the internal secrets and an empty vault from the template. Its `README.md` lists the next steps for that instance, which in short are:

1. Fill the external secrets listed in `~/my-brain/secrets/README.md` (model key, chat tokens, calendar credential).
2. Download the speech-to-text model into `models/` and build the three images (`second-brain/hermes`, `second-brain/brain`, `second-brain/terminal`) from `templates/deploy/`.
3. `sudo sh owners.sh`, then `uv run sb check ~/my-brain` until every static check passes.
4. `docker compose up -d` in the instance folder, then `uv run sb check ~/my-brain --live`.

[docs/operations.md](docs/operations.md) covers the first deploy in detail, backups, upgrades, approvals and troubleshooting.

## Building your own instance

An instance is a small Python package with a `second_brain.tools` entry point. Each tool is a `ToolSpec` with typed parameters, an effect, a default autonomy, the hosts it needs to reach and the names of the secrets it reads:

```python
ToolSpec(
    name="weather",
    description="Forecast for a place.",
    params=WeatherParams,
    effect="read",
    autonomy="auto",
    hosts=["api.open-meteo.com"],
    credentials=[],
)
```

`sb init` finds the instance tools, adds them to the guard table, opens their hosts in the egress proxy and lists their secrets. The instance image is just `FROM second-brain/brain` with the package installed on top. [docs/extending.md](docs/extending.md) explains how to pick effect and autonomy, how to add skills and what an instance must not do.

## Documentation

- [Architecture](docs/architecture.md): the components and how a request flows through them.
- [Threat model](docs/threat-model.md): assumptions, assets, attacker paths, defences and their tests.
- [Configuration](docs/configuration.md): the answers file and everything `sb init` renders.
- [Extending](docs/extending.md): instance tools and skills.
- [Operations](docs/operations.md): deploy, checks, secrets, backups, upgrades.

## Repository layout

```text
docs/                 architecture, threat model, configuration, extending, operations
src/second_brain/
  core/               config, vault schema, shared types
  guard/              Hermes plugin (brain_guard): policy engine, session state, budget, audit
  tools/              MCP server (brain_tools): registry, core tools, vault ops, secret scanner
  sidecars/           vault watcher, call router, job runner, scheduler
  gate/               model gate and watchdog
  cli/                `sb`: init an instance, check a deployment, verify a vault repo
templates/            vault (en, it, en-team), hardened Hermes config, compose and egress proxy
skills/               core Hermes skills: capture, triage, digest, brief, ingest-call, ...
tests/                unit tests per package; tests/security/ tries to break the rules
```

## Development

```sh
uv sync
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check
```

Every behaviour change comes with tests, and every security rule comes with a test in `tests/security/` that tries to break it. The rules for contributors (human or not) are in [AGENTS.md](AGENTS.md).

## Status

Version 0.1.0. It runs every day for a personal brain and a team brain, but interfaces and file formats can still change between versions.

## License

[MIT](LICENSE).
