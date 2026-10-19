# second-brain

A self-hosted personal assistant ("brain") built on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent), pinned at `v0.22.2`. One install per brain: a personal one, a team one, and so on.

The source of truth is a plain Markdown vault. Notes, calls, documents and answers live there as files with English frontmatter, organized in logical zones that each vault maps to localized folder names. Nothing important is hidden in a database.

Security model in one paragraph: assume a prompt injection will eventually convince the model. What it can do afterwards is limited by structure first (read-only vault mount, no credentials inside Hermes, egress filtering, terminal in a separate container), then by deterministic rules in code (brain_guard decides, brain_tools is the only writer), and never by prompts. Classifier-style injection detection is not a control.

Status: early development. Interfaces and file formats still move.

```
docs/                 architecture, threat model, configuration, extending
src/second_brain/
  core/               config, vault schema, shared types
  guard/              Hermes plugin (brain_guard): policy engine, session state, audit
  tools/              MCP server (brain_tools): registry, core tools, vault ops
  sidecars/           vault watcher, transcript watcher, job runner
  gate/               model gate and watchdog
  cli/                `sb` command: init an instance, scaffold a vault, check a deployment
templates/            vault/{it,en}, hardened Hermes config, deploy compose and egress proxy
skills/               core Hermes skills (capture, digest, ingest, reminders, documents)
tests/                unit tests per package; tests/security/ for attack scenarios
```

Python 3.12, `uv` for everything: `uv sync`, `uv run pytest -q`, `uv run ruff check`.
