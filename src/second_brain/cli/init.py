"""`sb init`: one answers file in, one instance directory out.

The order matters. The secrets are written first, because the rendered config has to carry the
token that is actually on disk: on a second run the existing file wins over the freshly generated
value, or a `--force` re-render would hand Hermes a token brain_tools never sees. The instance
directory is then created, refusing an existing one unless `--force`, and the vault scaffold is
copied only into a directory that does not exist or is empty, whatever `--force` says.

`--force` re-renders the config and the compose file. It never touches `secrets/`: rotating a live
token or a model key is not something a re-render can decide.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from second_brain.cli.answers import Answers
from second_brain.cli.config import (
    TEMPLATES,
    capture_dir,
    export_registry,
    git_init,
    guard_config,
    render_deploy,
    render_hermes,
    sidecars_config,
    tools_config,
)
from second_brain.cli.secrets import (
    CALENDAR_TOKEN,
    HERMES_ENV,
    MODEL_API_KEY,
    OUTBOX_SECRET,
    SECRETS_DIR,
    TERMINAL_SSH_KEY,
    TERMINAL_SSH_PUBKEY,
    TOOLS_TOKEN,
    WEBHOOK_IDEA,
    WEBHOOK_INGEST,
    WEBHOOK_RESEARCH,
    WEBHOOK_SCHEDULE,
    plan,
    write_secrets,
)

VAULT_GITIGNORE = ".gitkeep"
OWNERS_SCRIPT = "owners.sh"
OWNERS_MODE = 0o755
BRAIN_UID = 23002
HERMES_UID = 23001


class InitError(RuntimeError):
    """`sb init` refused to do something, and the instance is as it was."""


@dataclass
class InitResult:
    """What `sb init` did, for the summary the command prints."""

    instance_dir: Path
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    vault_created: bool = False
    vault_skipped: bool = False


def init(answers: Answers, instance_dir: Path, *, force: bool = False) -> InitResult:
    instance = instance_dir.resolve()
    result = InitResult(instance_dir=instance)
    _check_instance_dir(instance, force)
    result.vault_skipped = not _vault_is_free(answers.vault_dir)

    generated, secrets_created = write_secrets(
        instance / SECRETS_DIR, plan(channel=answers.channel, calendar=answers.tools.calendar is not None)
    )
    result.created += [f"{SECRETS_DIR}/{name}" for name in secrets_created]

    config = tools_config(answers)
    registry = export_registry(config)
    files = {
        **render_hermes(answers, registry, generated),
        **render_deploy(answers, registry, generated),
        "config/brain-tools.yaml": _yaml(config.model_dump(mode="json", exclude_none=True)),
        "config/brain-guard.yaml": _yaml(_guard_dict(guard_config(answers, registry))),
        "config/sidecars.yaml": _yaml(sidecars_config(answers).model_dump(mode="json", exclude_none=True)),
        OWNERS_SCRIPT: _owners_script(answers),
        "README.md": _readme(answers, instance),
    }
    for relative, content in files.items():
        target = instance / relative
        if target.exists() and not force:
            result.kept.append(relative)
            continue
        _write(target, content)
        result.created.append(relative)
    os.chmod(instance / OWNERS_SCRIPT, OWNERS_MODE)

    _ensure_host_dirs(answers)
    if not result.vault_skipped:
        _copy_vault(answers, result)
    return result


def _ensure_host_dirs(answers: Answers) -> None:
    """Create the host folders compose mounts, so Docker does not create them as root."""
    for directory in (answers.media_dir, answers.archive_dir):
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)


def _check_instance_dir(instance: Path, force: bool) -> None:
    if not instance.exists():
        return
    if not instance.is_dir():
        raise InitError(f"{instance} exists and is not a directory")
    if any(instance.iterdir()) and not force:
        raise InitError(f"{instance} already exists and is not empty; pass --force to re-render it")


def _vault_is_free(vault: Path) -> bool:
    """True when the vault can be scaffolded: the directory is missing or holds nothing."""
    if not vault.exists():
        return True
    if not vault.is_dir():
        raise InitError(f"vault_dir {vault} exists and is not a directory")
    return not any(vault.iterdir())


def _copy_vault(answers: Answers, result: InitResult) -> None:
    source = TEMPLATES / "vault" / answers.language
    answers.vault_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = answers.vault_dir / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if path.name == VAULT_GITIGNORE:
            continue
        target.write_bytes(path.read_bytes())
    git_init(answers.vault_dir)
    capture = answers.vault_dir / capture_dir(answers)
    capture.mkdir(parents=True, exist_ok=True)
    (capture / VAULT_GITIGNORE).write_text("", encoding="utf-8")
    result.vault_created = True


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _guard_dict(config) -> dict[str, object]:
    """The guard config as YAML, with the tool table left as the mapping it already is."""
    data = config.model_dump(mode="json", exclude_none=True)
    data["mcp_tools"] = {name: tool.model_dump(mode="json") for name, tool in config.mcp_tools.items()}
    return data


def _yaml(data: object) -> str:
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True)


def _owners_script(answers: Answers) -> str:
    """The one-shot fixup for file ownership: bind-mounted secrets keep the host's uid.

    Every service of the brain image runs as 23002 and Hermes as 23001, so a secret file owned by
    the operator is unreadable in the container however correct its mode is. The script is
    regenerated on every run, so it always lists the files this instance has.
    """
    brain_secrets = [TOOLS_TOKEN, MODEL_API_KEY, OUTBOX_SECRET]
    brain_secrets += [WEBHOOK_INGEST, WEBHOOK_RESEARCH, WEBHOOK_IDEA, WEBHOOK_SCHEDULE]
    if answers.tools.calendar is not None:
        brain_secrets.append(CALENDAR_TOKEN)
    hermes_secrets = [HERMES_ENV, TERMINAL_SSH_KEY]
    pubkey = f"{SECRETS_DIR}/{TERMINAL_SSH_PUBKEY}"

    lines = [
        "#!/bin/sh",
        "# Bind-mounted secrets belong to the uid that reads them, not to whoever ran `sb init`.",
        f"# Run it once with sudo, from anywhere: sh {OWNERS_SCRIPT}",
        "set -eu",
        'cd "$(dirname "$0")"',
        "",
        f"# The brain image services read these (uid/gid {BRAIN_UID}).",
    ]
    for name in brain_secrets:
        lines.append(_chown_line(f"{SECRETS_DIR}/{name}", BRAIN_UID))
    lines += ["", f"# Hermes reads these (uid/gid {HERMES_UID})."]
    for name in hermes_secrets:
        lines.append(_chown_line(f"{SECRETS_DIR}/{name}", HERMES_UID))
    lines += [
        "",
        "# The public half is not a secret, and sshd in the terminal reads it as its own user.",
        f'if [ -e "{pubkey}" ]; then chmod 644 "{pubkey}"; fi',
        "",
        "# brain_tools is the only writer of the vault.",
        f"chown -R {BRAIN_UID}:{BRAIN_UID} '{answers.vault_dir}'",
    ]
    if answers.archive_dir is not None:
        lines += [
            "",
            "# The archive tool moves files in and out of here.",
            f"chown -R {BRAIN_UID}:{BRAIN_UID} '{answers.archive_dir}'",
        ]
    lines.append("")
    return "\n".join(lines)


def _chown_line(relative: str, uid: int) -> str:
    """Only the files that exist: a brain without a calendar has no calendar token."""
    return f'if [ -e "{relative}" ]; then chown {uid}:{uid} "{relative}"; chmod 600 "{relative}"; fi'


WHISPER_DOWNLOAD = (
    'HF_HOME=models python -c "from huggingface_hub import snapshot_download; '
    "snapshot_download('Systran/faster-whisper-small')\""
)


def _readme(answers: Answers, instance: Path) -> str:
    """The next steps, written from the answers so they name the paths of this instance."""
    values = {
        "instance_name": answers.instance_name,
        "instance_dir": str(instance),
        "vault_dir": str(answers.vault_dir),
        "media_dir": str(answers.media_dir),
    }
    return f"""# {values["instance_name"]}

A brain built by `sb init`. Next steps:

1. Fill in the files under `secrets/` that are still empty. `secrets/README.md` says what each
   one is and where it comes from. `hermes.env` needs the same chat token, under the name Hermes
   reads (`TELEGRAM_BOT_TOKEN` or `SLACK_BOT_TOKEN`).
2. In the second-brain repo root, download the faster-whisper model into `models/` as a Hugging
   Face cache (`models/hub/models--Systran--faster-whisper-small`), which is where Hermes looks
   with no network:
   `{WHISPER_DOWNLOAD}`.
3. Build the three images with the repo root as context
   (`docker build -f templates/deploy/Dockerfile.hermes .`, same for `Dockerfile.brain` and
   `Dockerfile.terminal`) and tag them as `compose.yaml` expects.
4. Give the secrets to the containers: `sudo sh owners.sh`. Compose mounts them as plain files,
   so each one has to belong to the uid that reads it, and on the host it belongs to you.
   After this your own `git` refuses the vault as "dubious ownership": read it with
   `git -c safe.directory={values["vault_dir"]} ...`.
5. Check the instance before starting it: `sb check {values["instance_dir"]}`.
6. Start it: `docker compose up -d`.
7. Check what is actually running: `sb check {values["instance_dir"]} --live`.

The vault is at `{values["vault_dir"]}` and is written only by brain_tools. The media folder is
`{values["media_dir"]}`. The config under `config/` is rendered: change the answers file and run
`sb init --force` rather than editing it by hand.
"""
