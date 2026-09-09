#!/usr/bin/env python3
"""Prepare a self-contained Git bundle for scripts/remote-test.sh's transfer step.

Resolves HEAD and (if present) the merge-base target ``refs/remotes/
origin/main`` in the given repository, writes a ``git bundle`` containing
their reachable history, and prints one line of fields separated by ASCII
Unit Separator (0x1F): ``<head_sha><US><branch><US><origin_main_sha>``
(``branch`` empty for a detached HEAD; ``origin_main_sha`` empty when that
ref doesn't resolve). 0x1F is used instead of a tab or comma because git
forbids ASCII control characters below 0x20 in ref names (see
git-check-ref-format(1)) and a commit id is always hex, so neither field
can ever contain it — unlike a tab, which bash's own ``IFS=$'\\t' read``
collapses across a run of consecutive occurrences, silently misparsing an
empty middle field.

Uses only ``git`` porcelain/plumbing commands against the working
repository — never reads or copies ``.git/config``, hooks, or credential
helpers, and behaves identically whether ``.git`` is an ordinary directory
or a linked worktree's file pointing at another checkout's administrative
directory, since nothing here ever inspects or copies that file itself.
The resulting bundle is a completely separate, self-contained object,
transferable to a host that has no access to the source checkout at all.

``git bundle create`` must be given a named ref (``HEAD``/``refs/remotes/
origin/main``) rather than a bare commit id — a bare id has no name to
advertise, so git refuses to create the bundle at all ("refusing to create
empty bundle"). That leaves a window between resolving ``head_sha``/
``origin_main_sha`` above and ``git bundle create`` itself reading those
same refs, during which something could move them. Closing that window by
race-proof pinning isn't available through this interface, so instead the
bundle is verified immediately afterward: ``git bundle list-heads`` is
compared against the ids already resolved, and any mismatch — a ref moved
mid-bundle — is a hard failure rather than a silently mislabeled transfer.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

FIELD_SEP = "\x1f"


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--bundle-out", required=True)
    args = parser.parse_args(argv)

    head_sha = _git(args.repo, "rev-parse", "--verify", "-q", "HEAD")
    if not head_sha:
        print("no HEAD commit in this repository", file=sys.stderr)
        return 1

    branch = _git(args.repo, "branch", "--show-current")
    origin_main_sha = _git(args.repo, "rev-parse", "--verify", "-q", "refs/remotes/origin/main")

    expected = {"HEAD": head_sha}
    refs = ["HEAD"]
    if origin_main_sha:
        expected["refs/remotes/origin/main"] = origin_main_sha
        refs.append("refs/remotes/origin/main")

    result = subprocess.run(
        ["git", "-C", args.repo, "bundle", "create", args.bundle_out, *refs],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return 1

    heads = subprocess.run(
        ["git", "bundle", "list-heads", args.bundle_out],
        capture_output=True, text=True,
    )
    bundled: dict[str, str] = {}
    for line in heads.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            bundled[parts[1]] = parts[0]
    if bundled != expected:
        print(
            f"bundle advertises {bundled!r}, expected {expected!r} — "
            "a ref moved while the bundle was being created",
            file=sys.stderr,
        )
        return 1

    print(FIELD_SEP.join((head_sha, branch, origin_main_sha)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
