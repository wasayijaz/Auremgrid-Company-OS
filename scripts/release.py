"""Release validation and tagging tool.

Validates the codebase is ready for release: compiled source,
passing tests, clean working tree. Then creates an annotated git tag.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print("FAIL: " + " ".join(cmd))
        if r.stderr:
            print(r.stderr.strip())
        sys.exit(1)
    return r.stdout.strip()


def validate(repo):
    print("Checking compiled source...")
    _run([sys.executable, "-m", "compileall", "-q", "src", "tests"], repo)
    print("  OK")
    print("Checking import structure...")
    _run([
        sys.executable, "-c",
        "import auremgrid.storage; import auremgrid.domain; "
        "import auremgrid.services; import auremgrid.api; "
        "import auremgrid.connectors; import auremgrid.adapters",
    ], repo)
    print("  OK")
    print("Running verification suite...")
    _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], repo)
    print("  OK")
    print("Checking working tree...")
    status = _run(["git", "status", "--porcelain"], repo)
    if status:
        print("FAIL: uncommitted changes:")
        print(status)
        sys.exit(1)
    print("  OK")


def tag(repo, version, dry_run=False):
    validate(repo)
    tag_name = "v" + version
    print("Tagging " + tag_name + "...")
    cmd = ["git", "tag", "-a", tag_name, "-m", "Release " + tag_name]
    if dry_run:
        print("  DRY RUN: " + " ".join(cmd))
    else:
        _run(cmd, repo)
        print("  Tagged " + tag_name)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="auremgrid-release")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="run all pre-release checks")
    t = sub.add_parser("tag", help="validate and create an annotated git tag")
    t.add_argument("--version", required=True, help="semver tag (e.g. 0.2.0)")
    t.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    if args.command == "validate":
        validate(repo)
    elif args.command == "tag":
        tag(repo, args.version, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())