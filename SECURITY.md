# Security policy

## Report a vulnerability

Please report a vulnerability privately, through GitHub's
[private vulnerability reporting](https://github.com/jarin-devoss/varTrack/security/advisories/new) of this
repository, and not in a public issue or pull request.

Say what you found, which version or commit, and how to reproduce it. You will get an answer, and a fix in the
next release (or on `main`, for a problem that cannot wait). You can choose to be credited in the advisory.

## Supported versions

Fixes go to the latest release and to `main`. An older release is not patched: upgrade to the latest
([Upgrading](https://jarin-devoss.github.io/varTrack/operations/upgrading/)).

## Disclosure

A vulnerability is disclosed on the repository's
[security advisories](https://github.com/jarin-devoss/varTrack/security/advisories) page, after a release
that fixes it is published. The advisory says what was wrong, what it let someone do, the fix, and a workaround when
there is one.

## Dependencies

- A dependency is kept up to date by Dependabot, in one grouped pull request per ecosystem each week
  ([`.github/dependabot.yml`](.github/dependabot.yml)).
- CI scans the images it builds with Trivy and the Go code with `govulncheck` on every pull request, and fails when
  it finds a vulnerability that has a fix and is rated critical or high. The same scans run every week on `main`, so a
  vulnerability published after the last change is found as well.
- A vulnerability that has no fix yet is not a CI failure; it is read and answered on the repository's
  code-scanning page until one exists.

## What varTrack protects

How the services authenticate each other, what the gateway checks, and what to set before production:
[Internal authentication](https://jarin-devoss.github.io/varTrack/operations/internal-auth/) and the
[production checklist](https://jarin-devoss.github.io/varTrack/operations/production-checklist/).
