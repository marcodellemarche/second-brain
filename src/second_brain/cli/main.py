"""The `sb` command line: `sb init` today, `sb check` once the deployment has something to check.

One subcommand per verb, argparse and no other dependency. `init` is deliberately not interactive:
everything it needs comes from an answers file, so the same file builds the same brain again.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.init import InitError, init


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sb", description="Build and check a second brain instance.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser(
        "init", help="create an instance directory from an answers file", description=init_help()
    )
    init_parser.add_argument("instance_dir", type=Path, help="directory the instance is created in")
    init_parser.add_argument(
        "--answers", type=Path, required=True, help="answers file describing the brain (YAML)"
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="re-render an existing instance directory; secrets are still never overwritten",
    )
    return parser


def init_help() -> str:
    return (
        "Create an instance directory from an answers file.\n\n"
        "Renders the Hermes config, the tools and guard configs, squid, SearXNG, the compose file\n"
        "and the secrets folder, and scaffolds the vault when vault_dir is missing or empty.\n"
        "An existing instance directory is refused without --force."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "init":
        return _init(args)
    return 2


def _init(args: argparse.Namespace) -> int:
    try:
        answers = Answers.from_yaml(args.answers)
    except AnswersError as exc:
        print(f"sb init: {exc}", file=sys.stderr)
        return 2
    try:
        result = init(answers, args.instance_dir, force=args.force)
    except (InitError, OSError) as exc:
        print(f"sb init: {exc}", file=sys.stderr)
        return 1

    for name in result.created:
        print(f"created {name}")
    for name in result.kept:
        print(f"kept    {name} (already there, --force to re-render)")
    if result.vault_created:
        print(f"vault   {answers.vault_dir} scaffolded from templates/vault/{answers.language}")
    elif result.vault_skipped:
        print(f"vault   {answers.vault_dir} is not empty, left untouched")
    print("")
    print("Next: fill in the empty files under secrets/, then `docker compose up -d`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
