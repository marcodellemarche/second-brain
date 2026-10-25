# Operations

How to deploy a brain, read what `sb check` says, handle secrets and backups, upgrade Hermes, use approvals, and debug a brain that stopped answering.

## First deploy

The order matters: the static checks pass before anything starts, and the live checks come after.

1. Build the three images: `second-brain/hermes`, `second-brain/terminal`, `second-brain/brain`.
2. Run `sb init <instance_dir> --answers answers.yaml`. It renders the config, scaffolds the vault and generates the internal secrets.
3. Fill the external secrets listed in `secrets/README.md` of the instance: the model API key, the Telegram bot token or the Slack bot and app tokens, and the calendar credential (a Google service-account key, or an OAuth access token).
4. Run `sb check <instance_dir>`. Every static check must pass before you start anything.
5. Run `docker compose up -d` inside the instance directory.
6. Run `sb check <instance_dir> --live`. This asks docker compose what is running and probes it.

`sb init` refuses to overwrite an existing instance unless you pass `--force`. Force re-renders every file from the answers and the current secrets. Secrets are never overwritten, not even with `--force`.

## What sb check tells you

`sb check <instance_dir>` reads the rendered instance and the templates. It does not need Docker. Each check has one job:

| Check | Fails when |
| --- | --- |
| `files` | A rendered file is missing. |
| `placeholders` | A template marker was left in a rendered file. |
| `secrets` | A secret file is missing, empty, or readable by anyone but its owner. |
| `compose.vault-read-only` | Hermes mounts the vault read-write, or brain_tools does not. |
| `compose.docker-socket` | Any service mounts the Docker socket. |
| `compose.no-published-ports` | A service publishes a port on the host. |
| `compose.internal-network` | Hermes is reachable from a network other than the internal one. |
| `compose.hermes-proxy` | Hermes can leave the machine without going through the egress proxy. |
| `compose.slice` | With `stack_limits`, a service lacks the shared `cgroup_parent` or the `systemd/<slice>` file is missing. |
| `hermes.approvals` | Approvals are not manual, or cron and unattended runs are not set to deny. |
| `hermes.terminal` | The terminal does not run in its own container over ssh. |
| `hermes.owner` | Someone other than the owner is allowed, or the owner is not admin. |
| `hermes.plugin` | The brain-guard plugin is not enabled. |
| `hermes.research-routes` | A research route has MCP or lacks web. |
| `sidecars_config` | `config/sidecars.yaml` does not load. |
| `sidecar_routes` | A sidecar route is missing in Hermes, or its secret file is missing. |
| `team_domains` | Team domains are set in a `personal` brain, where a team call has nowhere to go. |
| `scheduled_routes` | A scheduled route has web, mirrors into chat, or something other than the digest delivers. |
| `tools.listen` | brain-tools listens on loopback, so Hermes cannot reach it. |
| `guard.tools` | The guard tool table and the tools registry disagree. |
| `squid.registries` | A package registry is not blocked. |

`--live` adds checks that need a running stack. Without Docker these are skipped, not failed.

| Check | Fails when |
| --- | --- |
| `live.services` | A service is not running or not healthy. |
| `live.vault-read-only` | A write from Hermes into the vault succeeds. |
| `live.no-docker-socket` | Hermes can see the Docker socket. |
| `live.heartbeat` | The guard heartbeat is missing or stale. |
| `live.gate-stale-heartbeat` | Always skipped: stopping Hermes to prove the gate refuses is too invasive for a live check, and unit tests cover it. |

The live checks are the ones that catch a config that looks right and behaves wrong, so run them after every change, not only on the first deploy.

## Secrets

Internal secrets are the MCP token, the webhook HMAC secrets, the outbox secret and the SearXNG key. `sb init` generates them. Hermes receives them through its managed env file, `secrets/hermes.env`, mounted read-only.

Do not regenerate them to start clean. A new MCP token or webhook secret invalidates the running Hermes until it is re-rendered and restarted, so the brain is broken in between and the failure looks like a plugin problem.

To rotate one:

1. Replace the secret file.
2. Run `sb init --force` to re-render `hermes.env`.
3. Restart the stack.

## Backups

Back up the vault, the archive directory and the `data` volume.

The vault is a git repository. Every brain write and every undo is a commit, and human edits are committed as `human`. It also syncs through the file sync you already use, which is not a backup: take one anyway.

The `data` volume holds the job queue, the notification counters, the outbox and the scheduler state. Losing it loses queued work and the digest state.

The archive directory holds the original documents. It is append-only, so a backup is cheap and a restore is a copy back.

The `guard` volume holds the audit log and the heartbeat. It is not part of a restore, but keep a copy of the audit log with the backups.

## Upgrades

Hermes is pinned at v0.22.2. Bump it deliberately, one version at a time, and treat it as a change to the security model rather than a dependency refresh.

Before the bump, read the Hermes changelog for the versions you cross, looking at approvals, webhooks and plugin hooks. Those three are what brain_guard and the sidecars depend on, and a change in any of them changes what the checks mean.

After the bump, rerun the test suite and `sb check --live`. A green static check on an upgraded Hermes proves little, because the config keys can still be accepted and ignored.

## Approvals

Never press "Approve Always" on a Hermes approval prompt. It allows the whole pattern category for the life of the process, so approving one `python -c` approves every `python -c` afterwards. Hermes v0.22.2 has no setting to hide that button for terminal approvals.

Approve once, or deny. If a prompt keeps coming back, the fix is in the tool or the policy, not in the button.

## Troubleshooting

Start with `docker compose ps` in the instance directory, then `docker compose logs <service>` for anything not healthy.

"The bot answers only with errors" usually means the guard plugin failed to load. The model gate refuses requests while the guard heartbeat is missing or older than its limit, so Hermes is up but has no model. That is deliberate: check the Hermes logs for the plugin error, fix it, and restart.

Guard decisions are in the hash-chained audit log in the `guard` volume. If you need to know why something was blocked or allowed, that is the place to look.

A sidecar that cannot start logs the missing config section or secret and restarts. Read the log line before touching the config.
