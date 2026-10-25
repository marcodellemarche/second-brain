# Architecture

One brain is a Hermes Agent container plus the containers around it: the tool server that owns the vault, the sidecars that notice and trigger, and the gate that holds the model key. Nothing in Hermes can write the vault, and nothing in Hermes can reach the model without the policy plugin loaded.

## The picture

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
watchdog ── tells the gate whether the brain_guard heartbeat is fresh; holds no Docker socket
```

The deployment has three Docker networks: `internal` for everything that must not reach the internet, `terminal-net` for the shell container, and `egress-net` for the three services allowed to see the internet. No port is published. The way out is an egress proxy.

## Components

### Hermes and the brain_guard plugin

Hermes runs the conversation, the tools and the skills. Its config is mounted read-only from the deployment, so the agent cannot edit its own tool surface, and its cron is off, so the model never decides when work happens.

brain_guard is a Hermes plugin that sees every tool call, every tool result, every model call and every reply. It keeps one in-memory state per session, and turns what a session reads into flags the policy checks later. Children point at their parent and every lookup walks the chain, so a flag set on a parent after a child started still applies. Anything that means "cannot decide" is raised and never swallowed, because Hermes blocks the tool on a hook exception and a plugin that cannot read its config stops, which the watchdog notices.

### brain_tools

brain_tools is an MCP server over streamable HTTP. It is the only writer of the vault and the only container holding credentials. Every registered tool declares an effect class, a default autonomy, the hosts it needs and the secrets it uses, and its input schema is a pydantic model with no free-form passthrough.

Every tool call carries one extra optional `guard` object that brain_guard fills before the call leaves Hermes. A call that arrives without it is refused, so a caller that skipped the guard cannot write. The HTTP layer refuses a request without the bearer token before it reaches the handler. The one route outside the token is `POST /outbox`, where Hermes posts the reply of a research run: Hermes cannot send a bearer token, so that route verifies the outbound-hook signature instead.

Instance repos add their own tools through the `second_brain.tools` entry point group.

### The vault and its git history

The vault is a folder of markdown notes with a `_meta/SCHEMA.md` that maps logical zone names to localized folder names. Hermes mounts it read-only. brain_tools mounts it read-write.

Every change is an atomic file operation followed by one git commit. The trailers record who asked, why, from which session, whether the session had seen untrusted content and whether a human approved. Paths are vault-relative and resolved inside the root, so a symlink that leaves the root is refused. A write whose content looks like a credential is refused before it touches the disk.

The user also edits the vault in an editor, and file sync drops those changes in on its own. Those are committed separately, authored by `human`, before the brain touches anything, so a brain commit only ever contains the paths the brain changed. A user file that looks like it holds a credential is left out of that commit and stays in the working tree until the secret is gone, and the brain then refuses to touch it.

Git is also the undo. `vault_undo` applies the inverse of a change, and it refuses with `conflict` when a later change makes the inverse impossible to apply cleanly. The other error codes are `outside_vault`, `zone_denied`, `quiet_window`, `hash_mismatch`, `secret_detected`, `not_found`, `exists` and `git_failed`.

There is no `_meta/log.md` and no `_meta/index.md` to keep up to date: the history of the vault is its git log, read with `vault_log`.

### Sidecars

The sidecars are separate containers on the internal network. Each one opens no port, mounts the vault read-only or not at all, and talks to Hermes only through a signed webhook. An instance package can add its own sidecars (a mail poller, an audio worker): they get the same hardening, and only the hosts, secrets and volumes their spec declares.

The vault watcher keeps a snapshot of every file's size and mtime and sends `{kind, path}` for what moved. It polls instead of using inotify, because the vault is a network-synced folder where inotify misses what the sync client writes underneath and fires on temporary files that never become notes. It never reads a note's content and never writes. It skips the bookkeeping folders, the temporary files of a sync in progress, and the paths the brain itself changed, recognised by a commit authored by `brain@localhost` in the last two polls. A sync conflict copy is reported immediately, because it is the one thing a human has to look at. A changed note waits until its size and mtime stop moving, because a half-written note is worse than a late event.

The call router decides whether a new call note stays in the personal brain or goes to the team brain, before anything is ingested. The decision is made with ordered deterministic rules, not by a model: a wrong guess towards personal costs nothing, a wrong guess towards the team cannot be taken back. Anything it cannot decide is `ask`, and the note stays personal until the digest asks the user.

The job runner takes a job from the queue, signs the body and POSTs it to the Hermes webhook route that does that kind of work. It never talks to the model and never writes the vault. A 2xx means Hermes accepted the run, not that the work is done, so the job becomes `dispatched` and only `finished` when its result note exists. It does not import research results: that needs a vault write, so brain_tools does it.

The scheduler fires the tasks Hermes is not allowed to schedule itself: the morning and evening digest, the nightly lint and the brief before a call. Its payload names the task and nothing else, no vault content and no instructions. Each entry fires at most once per due moment, and the last due moment that succeeded is kept in a small JSON file, so a restart does not lose the day and a brain that was off all night does not send yesterday's lint at noon.

### Model gate and watchdog

The model key lives in the gate, never in Hermes. The gate is a reverse proxy in front of an OpenAI-compatible endpoint: it forwards only `/v1/...` paths, and before every request it asks the watchdog's `/ok`. If that is not a 200, the answer is 503 and the upstream is never contacted, so a Hermes running without brain_guard cannot reach the model at all. Responses are relayed chunk by chunk, because the model answers with SSE streams that must reach the client as they are produced.

The watchdog is the last line of defence. Hermes loads plugins fail open, so the deployment needs something outside it to check. The watchdog reads the heartbeat the plugin writes and answers the gate's `/ok`: a missing or stale heartbeat closes the gate, and without the model Hermes cannot decide to call a tool. A file left over from a previous run cannot vouch for a new one, because the Hermes image deletes it before Hermes starts. No container in the deployment holds the Docker socket.

### Egress proxy and SearXNG

The egress proxy is the one door out. Its allowlist is rendered from the model endpoint host, the chat API and the hosts the enabled brain_tools tools declare. Hermes sends every outbound request through it, with the internal names in `NO_PROXY` so the call to brain_tools and the call to the gate stay off the proxy.

SearXNG is a local search instance, so `web_search` reaches it instead of an external search API. It sits on `egress-net` because it needs the internet to reach its engines.

### Terminal container

The shell runs in its own container, reached by Hermes over ssh and never started by it. It is on `terminal-net` alone, an internal network, because on the shared internal network it could still open a CONNECT through the egress proxy to any allowlisted host, and the allowlist is not a place to trust a shell. It is read-only with a tmpfs workspace, so nothing a command writes survives a restart, and it has capped cpu, memory and pids.

## How a request flows

### A chat message that writes a note

1. The message arrives on the channel. Hermes accepts it only from the owner's id.
2. `pre_llm_call` runs in brain_guard, which opens the session and records the sender.
3. Hermes calls the model through the gate. The gate asks the watchdog `/ok` first, then forwards to the upstream with the real key.
4. The model asks for `mcp__brain__vault_write`. `pre_tool_call` classifies the name against the instance table, applies the session flags and the policy, and either allows the call, blocks it or turns it into an approval.
5. The call goes to brain_tools with the bearer token and a `guard` object rebuilt from the session store. A forged `guard` in the model's own arguments is overwritten.
6. brain_tools resolves the path inside the vault root, scans the content for secrets, writes the file atomically and commits with the trailers.
7. `transform_llm_output` cleans the reply, removing filesystem paths and attachment tags, and the audit records what was removed.

### A research job round trip

1. The model calls `research_enqueue`. The kind has `web` set, so the tool is `external` and the rule of two applies: if the session has seen both untrusted and private content, the call needs an approval bound to that exact call.
2. The job lands in the SQLite queue with a title, a question and an optional note path. The payload never carries vault content, so a research run cannot see the vault.
3. The job runner claims the job inside an immediate transaction, signs the body with `X-Webhook-Signature-V2` and `X-Webhook-Timestamp`, and POSTs it to the `background-research` route. The job becomes `dispatched`.
4. That route's toolset is `[web, no_mcp]`: web and no vault, no brain_tools, so the run cannot write its result anywhere.
5. The run finishes. Hermes fires the `post_llm_call` outbound hook, which POSTs the reply to brain_tools `/outbox` with an HMAC signature over the raw body.
6. `OutboxReceiver` verifies the signature, refuses any delivery that is not the result of a job kind whose route has the web, and stores the payload as a JSON file.
7. brain_tools sweeps the outbox every 30 seconds, since it is the only writer of the vault. `OutboxImporter` writes the reply as a note in `raw/web/`, with `trust: untrusted` and the sources, and queues a follow-up job of the vault kind with the original question and the raw note path. The research job is finished.
8. The follow-up job goes to the `background-idea` route, which is `[file, skills, brain]` and no web: it can read the raw note and write through brain_tools, and it cannot send what it reads anywhere.
9. The final note appears in the vault through brain_tools, and the follow-up job is finished when that note exists.

### A scheduled digest

1. The scheduler compares the local time against its entries. Local times are built with `zoneinfo`, so a time that does not exist fires once that day and an ambiguous one fires once, at its first occurrence.
2. It signs a payload naming the task and the due moment, and POSTs it to the `scheduled-digest` route.
3. The route loads the `digest` skill with `[file, skills, brain]` and no web, and its `deliver` is the chat platform of this brain, because the digest is the message to the owner.
4. The skill reads what it needs from the vault through brain_tools and composes the message. Every tool call it makes passes brain_guard like any other.
5. The reply is delivered to the chat. The scheduler records the due moment as succeeded, so a restart does not send it twice.

## Core concepts

- Zones: logical vault areas, mapped to localized folder names by `_meta/SCHEMA.md` of each vault. Logical names: `inbox`, `raw`, `calls`, `topics`, `knowledge`, `ideas`, `projects`, `people`, `work`, `documents`, `answers`, `drawings`, `archive`, `_brain`, `_meta`. Code always uses logical names and resolves them through the schema map.
- Frontmatter keys are always English: `type`, `created`, `updated`, `tags`, `domain`, `share`, `sources`, `confidence`, `contested`, `trust`, `status`, `date`, `start`, `participants`, `organizer`, `event`, `audio`, `transcript`, `category`, `parties`, `issued`, `expires`, `original`, `original_sha256`, `project`, `jira`, `due`, `index`.
- Effect classes of a tool: `read`, `internal` (changes only our own data, undoable), `visible` (seen by other people: shared expenses, calendar invites, media requests), `external` (leaves the system: web fetch, questions to another brain, PRs to other repos).
- Autonomy levels: `auto` (just do it, audit only), `notify` (do it, report in the daily digest with undo), `ask` (needs an approval bound to the exact call), `never`.
- Session flags kept by brain_guard: `untrusted_seen` (the session read mail, web, call transcripts, another brain's answer, or a note with `trust: untrusted`) and `private_seen` (the session read private notes, `_brain`, documents or mail). Children inherit their parent's flags and sender.
- Rule of two: an `external` call needs `ask` when the session has both `untrusted_seen` and `private_seen`, unless the exact URL or question came literally from the user. A `visible` call is `notify` only when the user asked for it in a session without `untrusted_seen`, otherwise `ask`.
- Instance policy can only tighten a tool's default autonomy, never loosen `never`.
