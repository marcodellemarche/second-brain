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

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from second_brain.cli.answers import Answers
from second_brain.cli.config import (
    TEMPLATES,
    export_registry,
    git_init,
    guard_config,
    render_deploy,
    render_hermes,
    sidecars_config,
    tools_config,
)
from second_brain.cli.secrets import SECRETS_DIR, plan, write_secrets

VAULT_GITIGNORE = ".gitkeep"


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

    generated, secrets_created = write_secrets(instance / SECRETS_DIR, plan(channel=answers.channel))
    result.created += [f"{SECRETS_DIR}/{name}" for name in secrets_created]

    config = tools_config(answers)
    registry = export_registry(config)
    files = {
        **render_hermes(answers, registry, generated),
        **render_deploy(answers, registry, generated),
        "config/brain-tools.yaml": _yaml(config.model_dump(mode="json", exclude_none=True)),
        "config/brain-guard.yaml": _yaml(_guard_dict(guard_config(answers, registry))),
        "config/sidecars.yaml": _yaml(sidecars_config(answers).model_dump(mode="json", exclude_none=True)),
        "README.md": _readme(answers, instance),
    }
    for relative, content in files.items():
        target = instance / relative
        if target.exists() and not force:
            result.kept.append(relative)
            continue
        _write(target, content)
        result.created.append(relative)

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
   `HF_HOME=models python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small')"`.
3. Build the three images with the repo root as context
   (`docker build -f templates/deploy/Dockerfile.hermes .`, same for `Dockerfile.brain` and
   `Dockerfile.terminal`) and tag them as `compose.yaml` expects.
4. Start it: `docker compose up -d`.
5. Check it: `sb check {values["instance_dir"]}`.

The vault is at `{values["vault_dir"]}` and is written only by brain_tools. The media folder is
`{values["media_dir"]}`. The config under `config/` is rendered: change the answers file and run
`sb init --force` rather than editing it by hand.
"""
