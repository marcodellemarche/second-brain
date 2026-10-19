"""The `sb` command line: `sb init` to build an instance, `sb check` to verify it.

One subcommand per verb, argparse and no other dependency. `init` is deliberately not interactive:
everything it needs comes from an answers file, so the same file builds the same brain again.
`check` reads what `init` rendered and, with `--live`, asks Docker what is actually running.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from second_brain.cli.answers import Answers, AnswersError
from second_brain.cli.check import FAIL, SKIP, CheckResult, live_checks, static_checks
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

    check_parser = subcommands.add_parser(
        "check", help="verify an instance, before and after deployment", description=check_help()
    )
    check_parser.add_argument("instance_dir", type=Path, help="directory `sb init` created")
    check_parser.add_argument(
        "--live",
        action="store_true",
        help="also check the running deployment through docker compose (skipped without docker)",
    )
    return parser


def init_help() -> str:
    return (
        "Create an instance directory from an answers file.\n\n"
        "Renders the Hermes config, the tools and guard configs, squid, SearXNG, the compose file\n"
        "and the secrets folder, and scaffolds the vault when vault_dir is missing or empty.\n"
        "An existing instance directory is refused without --force."
    )


def check_help() -> str:
    return (
        "Verify an instance and print one line per check.\n\n"
        "The static checks read the rendered files: every file `sb init` writes is there, no template\n"
        "placeholder survived, the secrets are owner-only and filled in, the vault is mounted\n"
        "read-only everywhere but brain-tools, only the watchdog has the docker socket, no port is\n"
        "published, approvals are manual, the owner is the only allowed user, the guard knows every\n"
        "enabled tool and squid denies the package registries. `--live` adds what only a running\n"
        "deployment can answer. The exit code is 1 when anything fails."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "init":
        return _init(args)
    if args.command == "check":
        return _check(args)
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


def _check(args: argparse.Namespace) -> int:
    instance = args.instance_dir
    if not instance.is_dir():
        print(f"sb check: {instance} is not a directory", file=sys.stderr)
        return 2
    results = static_checks(instance)
    if args.live:
        results += live_checks(instance)
    return report(results)


def report(results: Sequence[CheckResult]) -> int:
    """One line per check, then the count. Any FAIL, or nothing checked at all, is exit 1."""
    width = max((len(result.name) for result in results), default=0)
    for result in results:
        print(f"{result.status.upper():4}  {result.name.ljust(width)}  {_one_line(result.reason)}")
    failed = [result for result in results if result.status == FAIL]
    skipped = [result for result in results if result.status == SKIP]
    passed = len(results) - len(failed) - len(skipped)
    summary = f"{passed} passed, {len(failed)} failed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    print("")
    print(summary)
    return 1 if failed else 0


def _one_line(reason: str) -> str:
    """A YAML parser error is several lines and indented: the report keeps one line per check."""
    return " ".join(reason.split())


if __name__ == "__main__":
    raise SystemExit(main())
