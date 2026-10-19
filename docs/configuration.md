# Configuration

A brain is one answers file and one instance directory. `sb init` reads the answers file, renders the config of every container, generates the secrets it can and leaves a file for the ones only a human can provide. The rendered files are not meant to be edited: change the answers and run `sb init --force`.

## The answers file

`sb init` is not interactive, so one YAML file describes the brain it builds. The same file builds the same brain again. An unknown key is refused: a misspelled `vault_dir` would otherwise be dropped and the vault scaffold would land in a directory nobody named.

| Key | Default | What it changes |
| --- | --- | --- |
| `instance_name` | required, `^[a-z0-9][a-z0-9_-]{0,62}$` | Container names and the generated README. |
| `language` | `en` (`it` or `en`) | Which vault template is scaffolded and which language the model is told to write in. |
| `owner_name` | required | Who the brain belongs to, in `USER.md` and the prompts. |
| `owner_telegram_id` | none | The Telegram chat the brain answers. Set this or `slack`, not both and not neither. |
| `slack.owner_id`, `slack.home_channel` | none | The same for a team brain on Slack: member id and default channel. |
| `timezone` | `UTC` | IANA name the schedule and the daily budget are read in. An unknown name is refused. |
| `vault_dir` | required, absolute | Where the vault lives on the host. Scaffolded only when missing or empty. |
| `media_dir` | required, absolute | Host folder for media, created if missing. |
| `archive_dir` | none | Host folder of the document archive. Required when `tools.archive` is set. |
| `model_endpoint` | required | Base URL of an OpenAI-compatible endpoint. http(s), no query and no fragment. Its host is the first entry of the egress allowlist. |
| `model_name` | required | Model name passed to the endpoint. |
| `model_context_length` | `131072` | Context window the Hermes config is rendered with, at least 1024. |
| `tools` | all off | Which optional tool groups exist. See below. |
| `job_kinds` | none | Background job kinds the model may queue. See below. |
| `schedule.digest` | `["08:00", "20:00"]` | Local times of the digest, one entry per time. |
| `schedule.lint` | `"03:30"` | Local time of the nightly lint. |
| `schedule.brief` | `["07:00", "13:00"]` | Local times of the brief before a call. |
| `calls.capture_dir` | the inbox calls folder | Vault-relative folder the call router watches. |
| `calls.team_domains` | `[]` | Email domains that make a call a team call. |
| `calls.personal_markers` | `[]` | Strings in the event or the domain that keep a call personal. |

Every time in `schedule` is `HH:MM`, 24 hours, and is validated before anything is written.

The `tools` section is a group per key. A group left out is not built at all, so the tool is not in the registry and the model never sees it.

| Key | Default | What it changes |
| --- | --- | --- |
| `tools.calendar` | off | `assistant_calendar_id` and `main_calendar_id`. The token is a secret file, not a value here. |
| `tools.weather` | `false` | The weather tool and its host in the egress allowlist. |
| `tools.links` | off | `base_url` (https only), `vault_folder` and `archive_folder` inside the Nextcloud files. |
| `tools.archive` | off | `discard_dir` (`_discard`) and `allowed_extensions` (`pdf`, `jpg`, `jpeg`, `png`, `heic`, `webp`). |
| `tools.jobs` | `false` | The job queue. Needs at least one job kind. |
| `tools.notify` | off | `initiative_path` (the schema's initiative page when empty) and `default_daily` (`6`), the messages a day when the budget note cannot be read. |

A job kind says what the model may queue and whether the route behind it reaches the web:

```yaml
job_kinds:
  research:
    route: background-research
    description: Search the web for an answer and write it into the vault.
    web: true
  idea:
    route: background-idea
    description: Develop an idea from what the vault already knows.
    web: false
```

`web` has no default. The guard needs it to decide whether enqueueing that kind is an internal call or an external one. A kind whose route is not listed cannot be queued at all, so the model cannot invent a route. Three combinations are refused: `tools.jobs` with no job kind, job kinds with `tools.jobs` off, and a set of kinds where every one can reach the web, because a web run cannot write its result back and needs a kind that cannot.

The full sample file, with every value fake:

```yaml
# A sample answers file for `sb init`, every value fake. Secrets are never here: `sb init`
# generates the internal ones and leaves an empty file under secrets/ for the rest.
instance_name: sample-brain
language: en
owner_name: Sample Owner
owner_telegram_id: "100000001"
timezone: Europe/Rome

# Absolute paths on the host. The vault is scaffolded only when it is missing or empty.
vault_dir: /srv/brain/vault
media_dir: /srv/brain/media
archive_dir: /srv/brain/archive

model_endpoint: https://model.example.com/v1
model_name: sample-model
model_context_length: 131072

tools:
  calendar:
    assistant_calendar_id: assistant@example.com
    main_calendar_id: main@example.com
  weather: true
  links:
    base_url: https://cloud.example.com
    vault_folder: Vault
    archive_folder: Archive
  archive:
    discard_dir: _discard
  jobs: true
  notify:
    initiative_path: _brain/initiative.md
    default_daily: 6

job_kinds:
  research:
    route: background-research
    description: Search the web for an answer and write it into the vault.
    web: true
  idea:
    route: background-idea
    description: Develop an idea from what the vault already knows.
    web: false

# Local times the scheduler fires at, on every weekday. HH:MM, 24 hours.
schedule:
  digest: ["08:00", "20:00"]
  lint: "03:30"
  brief: ["07:00", "13:00"]

calls:
  # Vault-relative folder the call router watches. Left out it is <inbox>/calls.
  # capture_dir: inbox/calls
  # Stays empty until there is a team brain: it widens what counts as a team call.
  team_domains: []
  personal_markers: []
```

## What sb init writes

```
<instance_dir>/
  compose.yaml
  README.md
  config/
    brain-tools.yaml
    brain-guard.yaml
    sidecars.yaml
    squid.conf
    searxng/settings.yml
    hermes/
      config.yaml
      SOUL.md
      USER.md
  secrets/
```

`sb init` refuses a non-empty instance directory unless `--force`, and `--force` re-renders the config and the compose file only: it never touches `secrets/`. It also creates `media_dir` and `archive_dir` on the host, so Docker does not create them as root.

Which container reads what:

| File | Read by | How |
| --- | --- | --- |
| `compose.yaml` | Docker | The deployment itself. |
| `config/hermes/config.yaml` | Hermes | Mounted at `/etc/hermes/config.yaml`. The managed config scope, merged on top of the user config. |
| `config/hermes/SOUL.md`, `config/hermes/USER.md` | Hermes | The agent's identity and the owner's profile. |
| `config/brain-guard.yaml` | Hermes, as the brain_guard plugin | Path in `BRAIN_GUARD_CONFIG`. |
| `secrets/hermes.env` | Hermes | Mounted at `/etc/hermes/.env`, the managed env scope. |
| `config/brain-tools.yaml` | brain_tools | Path in `BRAIN_TOOLS_CONFIG`. |
| `config/sidecars.yaml` | vault watcher, call router, job runner, scheduler | Path in `SIDECARS_CONFIG`. |
| `config/squid.conf` | the egress proxy | The allowlist of hosts anything in the internal network may reach. |
| `config/searxng/settings.yml` | SearXNG | The search instance Hermes' `web_search` queries. |
| `secrets/model_api_key` | model gate | The only container that holds the real key. |

The guard config is rendered from the same registry as the tools config, so the two cannot disagree about a tool. The egress allowlist is the model endpoint host, the chat API and the hosts the enabled tools declare.

`README.md` is generated from the answers and lists the next steps with the paths of this instance.

## Secrets

Secrets are always files under `secrets/`, never values in a config. Each one is mounted read-only into the single container that reads it, and a config field that carries a secret value is refused before anything is read.

Two kinds, handled differently on purpose:

- Generated by `sb init`: `tools_token`, `webhook_ingest`, `webhook_research`, `webhook_idea`, `webhook_schedule`, `outbox_secret`, `searxng_key`. These are values only this brain uses, so nobody has to invent them. The same brain initialised twice gets two different sets.
- Filled in by a human: `model_api_key`, `calendar_token`, `telegram_bot_token` or `slack_bot_token`, `slack_app_token` for a Slack brain, and `terminal_ssh_key`. The file is created empty and named in `secrets/README.md` with where it comes from. `terminal_ssh_key` is generated when `ssh-keygen` is available.

No file in `secrets/` is written twice, not by a second `sb init` and not with `--force`. A regenerated token would silently invalidate a running deployment, and a regenerated model key would not exist on the other side at all. The rendered config uses the value that is on disk, so a re-render never hands Hermes a token brain_tools does not have.

`secrets/hermes.env` is the one place where generated values are written as values rather than read from a file: Hermes takes the MCP token, the chat token and the webhook secrets from the environment. It is generated once, and the chat token in it is filled in by hand.

## brain_tools config

`config/brain-tools.yaml` is read once when the server starts. It says which tool groups this brain has and where their inputs live. A typo stops the server from starting, because a silently missing section would look like a tool that is simply not there.

- `vault.root`: the vault path inside the container. brain_tools is the only writer, and the vault is mounted read-write only here.
- `token_file`: file holding the bearer token Hermes presents.
- `listen`: `host:port`, written as one string, default `127.0.0.1:8765`.
- `human_commit_seconds`: how often the background timer commits the owner's edits, so history shows them even when the brain wrote nothing.
- `calendar`: `assistant_calendar_id`, `main_calendar_id`, `timezone`, and `token_file`. The token is a path, never a value.
- `weather.enabled`.
- `links`: `base_url`, `vault_folder`, `archive_folder`.
- `archive`: `root`, `inbox_media_dir`, `discard_dir`, `allowed_extensions`.
- `jobs`: `store_path` of the queue file and `kinds`, each with `route`, `description` and `web`.
- `notify`: `store_path`, `initiative_path`, `default_daily`, `timezone`.
- `outbox`: `dir`, `secret_file` and `research_followup_kind`, rendered only when there is a kind without web access.

A field whose name is one of `token`, `access_token`, `refresh_token`, `client_secret`, `secret`, `password`, `api_key` is refused anywhere in the file. The way to give a tool a secret is a `*_file` field pointing at the file under `/run/secrets`.

## brain_guard config

`config/brain-guard.yaml` is read once when the plugin loads, and it is the only file an instance has to provide for the guard to start. A typo must stop the guard from loading, not silently widen a rule.

- `vault_root`, `guard_dir`: the vault and the folder the heartbeat is written to.
- `hermes_config_path`: Hermes' own config. The guard refuses to start unless it says `approvals.mode: manual`, because the guard stamps `approval: human:<sender>` when it escalates a call and that is only true while a person answers the prompt. Under `smart` an auxiliary model can approve it, under `off` nothing is asked.
- `private_prefixes`, `untrusted_prefixes`: vault folders that mark a session as having read private or untrusted material. Rendered from the schema, so a renamed folder follows.
- `media_dir`: where media written by Hermes lives.
- `admin_senders`: the sender ids that are the owner. `sb check` fails when this is not exactly the owner, or when the Hermes config allows anyone else.
- `search_tools`: the tools counted as searches by the budget, default `web_search`, `search_files`, `session_search`.
- `mcp_tools`: one entry per brain_tools tool, with `effect` (`read`, `internal`, `visible`, `external`), `autonomy` (`auto`, `notify`, `ask`, `never`) and the two taints `taints_untrusted` and `taints_private`. The table is exported from the tools registry, not written by hand.
- `policy`: per-tool autonomy overrides and the run budget.

The policy section looks like this:

```yaml
policy:
  tools:
    weather:
      autonomy: ask
  max_steps: 25
  max_seconds: 600
  max_repeat: 2
  max_empty_searches: 3
  proactive_per_day: 6
```

Instance policy can only tighten the autonomy a tool declares. A tool that defaults to `notify` can be made `ask`, a tool that defaults to `never` stays `never`, and nothing here can loosen either. The same section holds the budget: steps and seconds per run, how many identical calls and empty searches are allowed, and how many proactive messages a day.

## Sidecars config

`config/sidecars.yaml` is read by the four sidecars. It says which sidecars this brain runs and which Hermes webhook route each one posts to.

- `vault_dir`: the vault inside the container.
- `webhook_base_url`: where Hermes receives webhooks.
- `route_secrets`: a route name to the file holding its HMAC secret. A route a section uses but this map does not name stops the sidecar at startup, because Hermes would refuse every event it sends. As with the tools config, a secret value in the file is refused.
- `vault_watcher`: `route` (`ingest`), `quiet_seconds` (`20`), `ignore`, `brain_email`, `poll_seconds` (`5`). The ignore list already carries the call capture folder, so a call note produces one event from the call router and not two.
- `call_router`: `route` (`ingest`), `watch_dir` (vault-relative), `rules` and `poll_seconds` (`10`). The rules are `team_domains`, `team_min_participants` (`3`) and `personal_markers`.
- `job_runner`: `jobs` (the same store path and kinds as brain_tools), `max_concurrent` (`1`), `dispatch_timeout` (`3600`).
- `scheduler`: `schedule`, `state_file`, `poll_seconds` (`30`).

Every section is optional. A sidecar whose section is missing refuses to start and says so in its log, so leave its service out of the compose file too.

The scheduler exists because Hermes' own cron is off: the model never decides when work happens. The digest, the lint and the brief still need a trigger, so the sidecar sends a signed webhook at fixed local times instead. The payload names the task and nothing else, no vault content and no instructions.

```yaml
scheduler:
  schedule:
    timezone: Europe/Rome
    catch_up_minutes: 30
    entries:
      - name: digest-1
        route: scheduled-digest
        at: "08:00"
        days: [mon, tue, wed, thu, fri]
  state_file: /data/scheduler.json
  poll_seconds: 30
```

An entry has a `name` (unique, sent to Hermes as-is), a `route`, an `at` in `HH:MM` and the `days` it runs on, all weekdays by default.

`catch_up_minutes` is how late a task may still fire after its due moment, default 30, at most 720. Every entry fires at most once per due moment, and the last due moment that succeeded is kept in `state_file`, so a restart does not lose the day and a failed send is retried on the next poll. A restart inside the window still sends a task that was due minutes ago; a brain that was off all night does not send yesterday's lint at noon. Local times are built with `zoneinfo`, so a time that does not exist (spring forward) fires once that day and an ambiguous one (fall back) fires once, at its first occurrence.

## The vault schema

Every vault carries `_meta/SCHEMA.md`, and that page is the map the code reads. It holds five things:

- `language`: the language of the vault.
- `zones`: the logical name of each zone to its folder in this vault. Code always works on the logical names (`inbox`, `raw`, `calls`, `topics`, `knowledge`, `ideas`, `projects`, `people`, `work`, `documents`, `answers`, `drawings`, `archive`, `_brain`, `_meta`) and resolves them through this map.
- `types` and `tags`: closed taxonomies. A new value is added here, and the change goes through an approval.
- `untrusted_zones`: the zones holding other people's words. `raw` holds transcripts, mail, web pages and forwarded files; it is immutable after it lands and counts as untrusted, so a page derived from it carries `trust: untrusted`.
- `brain_pages`: the logical name of each page the agent keeps about itself in `_brain` (`commitments`, `initiative`, `policy`, `positions`, `preferences`, `profile`, `lint`) to its file name.

An abridged example (a real schema must map every zone):

```yaml
language: en
zones:
  inbox: inbox
  knowledge: knowledge
  raw: raw
  _brain: _brain
types:
  knowledge: knowledge page about a domain
  answer: a good answer, saved
tags:
  - work
  - study
untrusted_zones:
  - raw
brain_pages:
  initiative: initiative
  profile: profile
```

Renaming a folder or a page is a change in `SCHEMA.md` alone. Nothing else in the code needs to change, and the guard prefixes and the tool paths rendered by `sb init` are derived from the same map.
