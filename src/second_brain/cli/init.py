"""`sb init`: one answers file in, one instance directory out.

The order matters. The secrets are written first, because the rendered config has to carry the
token that is actually on disk: on a second run the existing file wins over the freshly generated
value, or a `--force` re-render would hand Hermes a token brain_tools never sees. The instance
directory is then created, refusing an existing one unless `--force`, and the vault scaffold is
copied only into a directory that does not exist or is empty, whatever `--force` says.

`--force` re-renders the config and the compose file. It never touches `secrets/`: rotating a live
token or a model key is not something a re-render can decide. The product skills are copied on
every run, `--force` or not: they are product code, and compose mounts them read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from second_brain.cli.answers import Answers
from second_brain.cli.config import (
    SECRETS_IN_CONTAINER,
    TEMPLATES,
    capture_dir,
    export_registry,
    git_init,
    guard_config,
    instance_credentials,
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
    TERMINAL_HOST_KEY,
    TERMINAL_HOST_PUBKEY,
    TERMINAL_SSH_KEY,
    TERMINAL_SSH_PUBKEY,
    TOOLS_TOKEN,
    WEBHOOK_IDEA,
    WEBHOOK_INGEST,
    WEBHOOK_RESEARCH,
    WEBHOOK_SCHEDULE,
    SecretUnreadable,
    plan,
    write_secrets,
)

VAULT_GITIGNORE = ".gitkeep"
OWNERS_SCRIPT = "owners.sh"
OWNERS_MODE = 0o755
SKILLS_DIR = "skills"
SCAFFOLD_AUTHOR = "sb-init"
SCAFFOLD_MESSAGE = "scaffold: vault created by sb init"
BRAIN_UID = 23002
HERMES_UID = 23001
# sshd runs as `agent` in the terminal image, and it is the one that reads the host key.
TERMINAL_UID = 1000


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
    warnings: list[str] = field(default_factory=list)


def init(answers: Answers, instance_dir: Path, *, force: bool = False) -> InitResult:
    instance = instance_dir.resolve()
    result = InitResult(instance_dir=instance)
    _check_instance_dir(instance, force)
    result.vault_skipped = not _vault_is_free(answers.vault_dir)

    # The registry is built before the secrets, because the secrets are the ones its instance
    # tools declared: the guard table and the egress allowlist come from the same build.
    config = tools_config(answers)
    registry = export_registry(config)
    credentials = instance_credentials(registry)

    try:
        generated, secrets_created = write_secrets(
            instance / SECRETS_DIR,
            plan(
                channel=answers.channel,
                calendar=answers.tools.calendar is not None,
                mail=answers.tools.mail is not None,
                instance_credentials=credentials,
            ),
        )
    except SecretUnreadable as exc:
        raise InitError(_unreadable_secret_message(exc.name, instance)) from exc
    result.created += [f"{SECRETS_DIR}/{name}" for name in secrets_created]
    result.warnings += _keypair_warnings(instance)

    files = {
        **render_hermes(answers, registry, generated),
        **render_deploy(answers, registry, generated),
        "config/brain-tools.yaml": _yaml(config.model_dump(mode="json", exclude_none=True)),
        "config/brain-guard.yaml": _yaml(_guard_dict(guard_config(answers, registry))),
        "config/sidecars.yaml": _yaml(sidecars_config(answers).model_dump(mode="json", exclude_none=True)),
        **_instance_configs(answers),
        OWNERS_SCRIPT: _owners_script(answers, credentials),
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
    _copy_skills(instance, result)
    if answers.instance_skills_dir is not None:
        _copy_instance_skills(answers.instance_skills_dir, instance, result)

    _ensure_host_dirs(answers)
    if not result.vault_skipped:
        _copy_vault(answers, result)
    return result


def _instance_configs(answers: Answers) -> dict[str, str]:
    """The settings files of the services an instance adds: the audio worker and the mail sidecar.

    Only the ones this brain has. The paths in them are the containers' own, so they are rendered
    from the answers and never typed by hand.
    """
    files: dict[str, str] = {}
    worker = answers.tts_worker_config()
    if worker is not None:
        files["config/tts-worker.json"] = _json(worker)
    if answers.tools.mail is not None:
        files["config/mail.json"] = _json(answers.mail_sidecar_config(SECRETS_IN_CONTAINER / WEBHOOK_INGEST))
    return files


def _json(values: dict[str, object]) -> str:
    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


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


def _unreadable_secret_message(name: str, instance: Path) -> str:
    return (
        f"cannot read {SECRETS_DIR}/{name}: the secrets belong to the containers since owners.sh "
        f"ran, so run `sudo sb init {instance} --force`, then `sudo sh {OWNERS_SCRIPT}` again"
    )


def _keypair_warnings(instance: Path) -> list[str]:
    """An empty private key means ssh-keygen was missing: the terminal would come up without it."""
    warnings: list[str] = []
    for name in (TERMINAL_SSH_KEY, TERMINAL_HOST_KEY):
        path = instance / SECRETS_DIR / name
        if not path.exists() or path.stat().st_size:
            continue
        warnings.append(
            f"{SECRETS_DIR}/{name} is empty, so the terminal has no key: run "
            f"`rm {path} && ssh-keygen -t ed25519 -N '' -f {path}` before `sudo sh {OWNERS_SCRIPT}`"
        )
    return warnings


def _vault_is_free(vault: Path) -> bool:
    """True when the vault can be scaffolded: the directory is missing or holds nothing."""
    if not vault.exists():
        return True
    if not vault.is_dir():
        raise InitError(f"vault_dir {vault} exists and is not a directory")
    return not any(vault.iterdir())


# Folders a shape removes from the language template it starts from.
_SHAPE_WITHOUT: dict[str, tuple[str, ...]] = {"team": ("_brain",)}


def _copy_vault(answers: Answers, result: InitResult) -> None:
    source = TEMPLATES / "vault" / answers.language
    overlay = TEMPLATES / "vault" / f"{answers.language}-{answers.shape}"
    answers.vault_dir.mkdir(parents=True, exist_ok=True)
    # A shape that has its own files starts from the language template and then overwrites what it
    # changes, so the two vaults stay one template with one difference instead of two forks.
    files: dict[Path, Path] = {}
    for root in (source, overlay) if overlay.is_dir() else (source,):
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_dir():
                (answers.vault_dir / relative).mkdir(parents=True, exist_ok=True)
                continue
            files[relative] = path
    for relative, path in files.items():
        if path.name == VAULT_GITIGNORE:
            continue
        (answers.vault_dir / relative).write_bytes(path.read_bytes())
    # A team vault has no agent memory of its own: the overlay drops the folders it does not want.
    for relative in _SHAPE_WITHOUT.get(answers.shape, ()):
        target = answers.vault_dir / relative
        if target.is_dir():
            shutil.rmtree(target)
    git_init(answers.vault_dir)
    capture = answers.vault_dir / capture_dir(answers)
    capture.mkdir(parents=True, exist_ok=True)
    (capture / VAULT_GITIGNORE).write_text("", encoding="utf-8")
    _commit_scaffold(answers.vault_dir)
    result.vault_created = True


def _copy_skills(instance: Path, result: InitResult) -> None:
    """Copy the product skills into the instance, overwriting whatever is there.

    They are product code, not user data, so a rerun refreshes them: compose mounts the folder
    read-only into Hermes, which is what keeps a skill write approved from the chat away from them.
    """
    source = TEMPLATES.parent / SKILLS_DIR
    copied = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = Path(SKILLS_DIR) / path.relative_to(source)
        target = instance / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied += 1
    if copied:
        result.created.append(f"{SKILLS_DIR}/")


def _copy_instance_skills(source: Path, instance: Path, result: InitResult) -> None:
    """Copy the skills this instance brings, next to the product ones.

    They are the instance's own code, so a rerun refreshes them like the product skills: compose
    mounts the whole `skills/` folder read-only into Hermes. A folder without a `SKILL.md` is a
    mistake, not something to copy silently, and the name has to match the folder the way Hermes
    looks it up.
    """
    if not source.is_dir():
        raise InitError(f"instance_skills_dir is not a folder: {source}")
    folders = [path for path in sorted(source.iterdir()) if path.is_dir() and not path.name.startswith(".")]
    if not folders:
        raise InitError(f"instance_skills_dir has no skill folder: {source}")
    for folder in folders:
        skill = folder / "SKILL.md"
        if not skill.is_file():
            raise InitError(f"{folder.name} has no SKILL.md")
        target = instance / SKILLS_DIR / folder.name
        if target.exists():
            raise InitError(
                f"the instance skill {folder.name} would replace the product skill of the same name"
            )
        target.mkdir(parents=True)
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                destination = target / path.relative_to(folder)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(path.read_bytes())
        result.created.append(f"{SKILLS_DIR}/{folder.name}/")


def _commit_scaffold(vault: Path) -> None:
    """Commit the scaffold so the human-commit timer does not read it as the owner's edits.

    The vault ops refuse to undo a commit the brain did not author, so this one is inert history.
    """
    subprocess.run(["git", "-C", str(vault), "add", "-A"], capture_output=True, text=True, check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(vault),
            "-c",
            f"user.name={SCAFFOLD_AUTHOR}",
            "-c",
            f"user.email={SCAFFOLD_AUTHOR}@localhost",
            "commit",
            "-q",
            "-m",
            SCAFFOLD_MESSAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


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


def _owners_script(answers: Answers, credentials: dict[str, str]) -> str:
    """The one-shot fixup for file ownership: bind-mounted secrets keep the host's uid.

    Every service of the brain image runs as 23002 and Hermes as 23001, so a secret file owned by
    the operator is unreadable in the container however correct its mode is. The script is
    regenerated on every run, so it always lists the files this instance has.
    """
    brain_secrets = [TOOLS_TOKEN, MODEL_API_KEY, OUTBOX_SECRET]
    brain_secrets += [WEBHOOK_INGEST, WEBHOOK_RESEARCH, WEBHOOK_IDEA, WEBHOOK_SCHEDULE]
    if answers.tools.calendar is not None:
        brain_secrets.append(CALENDAR_TOKEN)
    for name in credentials:
        if name not in brain_secrets:
            brain_secrets.append(name)
    hermes_secrets = [HERMES_ENV, TERMINAL_SSH_KEY]
    pubkey = f"{SECRETS_DIR}/{TERMINAL_SSH_PUBKEY}"
    host_key = f"{SECRETS_DIR}/{TERMINAL_HOST_KEY}"
    host_pubkey = f"{SECRETS_DIR}/{TERMINAL_HOST_PUBKEY}"

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
        f"# The terminal's host key: sshd runs as agent (uid/gid {TERMINAL_UID}) and must read it.",
        _chown_line(host_key, TERMINAL_UID),
        f'if [ -e "{host_pubkey}" ]; then chmod 644 "{host_pubkey}"; fi',
        "",
        "# brain_tools is the only writer of the vault.",
    ]
    if answers.vault_group_gid is None:
        lines.append(f"chown -R {BRAIN_UID}:{BRAIN_UID} '{answers.vault_dir}'")
    else:
        # The vault belongs to another service (a Nextcloud data directory) and is written by both.
        # Handing it to the brain would make that service stop serving it, so only the group is set,
        # and the containers join it through `vault_group_gid`.
        lines += [
            "# The vault belongs to the service that serves it; the brain joins its group instead.",
            f"chgrp -R {answers.vault_group_gid} '{answers.vault_dir}'",
            f"chmod -R g+rwX '{answers.vault_dir}'",
            f"find '{answers.vault_dir}' -type d -exec chmod g+s {{}} +",
        ]
    if answers.archive_dir is not None:
        lines += [
            "",
            "# The archive tool moves files in and out of here.",
            f"chown -R {BRAIN_UID}:{BRAIN_UID} '{answers.archive_dir}'",
        ]
    # The vault and the archive are deliberately left out: they belong to the brain, and a blanket
    # `chown` over the instance directory would hand them back to the operator, which is exactly the
    # state git refuses to work in ("dubious ownership").
    keep_owned = ["secrets", answers.vault_dir.name, (answers.archive_dir or answers.vault_dir).name]
    skip = " || ".join(f'[ "$entry" = "{name}" ]' for name in keep_owned)
    lines += [
        "",
        "# A `sudo sb init --force` leaves root-owned files the operator still has to edit.",
        "owner=$(stat -c %u:%g .)",
        "for entry in *; do",
        f"    if {skip}; then continue; fi",
        '    chown -R "$owner" "$entry"',
        "done",
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
