# Contributing to OpenFeed

Thanks for your interest. Contributions are welcome, and the process is deliberately
simple: **fork, branch, open a pull request, and it gets reviewed before it merges.**

OpenFeed is in **alpha** and has a single maintainer. Every pull request is reviewed by
[@S0nrxse](https://github.com/S0nrxse) before merging(will most likely change in the future but for the moment I would like to decide the direction we take on the project). Expect review to take a few days.

## Reporting issues

Use the [issue templates](https://github.com/S0nrxse/openFeed/issues/new/choose). Bug
reports need what you expected, what happened, and how to reproduce it; logs from
`docker compose logs openfeed-app` help.

Security vulnerabilities do not go in a public issue — see [`SECURITY.md`](SECURITY.md).

## Before you write code

**Open an issue first for anything non-trivial.** A bug fix or a typo can go straight to a
pull request. A new feature, a dependency, a schema change, or a refactor that touches
several files should start as an issue so the approach can be agreed before you spend time
on it. Rejecting a finished feature is a worse outcome for everyone and I would rather reject or redirect an idea early on.

Things most likely to be turned down:

- New third-party services that don't respect user privacy by default. Every integration apart of this app is opt-in,
  read-only and automatically off until the user asks — see SponsorBlock and RYD for the pattern I've used.
- Anything that writes to YouTube: posting, voting, commenting, or authenticated actions.
- Telemetry, analytics, or crash reporting of any kind.
- Large dependencies where a small amount of code would do.

## Development

Create `.env` from `.env.example`, then build and test:

```bash
docker build -t openfeed-app:test app
```

```bash
docker run --rm openfeed-app:test python -m unittest discover -s openfeed/tests -v
```

The suite needs no database and no network.

`app/` is **not** bind-mounted into the container, so rebuild the image to see your
changes. `openfeed-app` and `openfeed-worker` share one image — if you change worker
behaviour, recreate both.

## Pull requests

Open the pull request against `main` from a branch on your fork. In the description, say
what changed and why; if it fixes an issue, link it.

Before it can merge:

- **Tests pass**, and new behaviour has a test. A bug fix should have one that fails
  without the fix.
- **Focused scope.** One concern per pull request. Unrelated cleanups make review slow and leads to even more mistakes

Style follows the surrounding code rather than a linter. Comments should explain *why*
something is the way it is, especially where the obvious approach was wrong — most of the
comments in this codebase exist because something behaved unexpectedly, and that
context is the valuable part.

Maintainer review may ask for changes, and sometimes will decline a change that works.
If that happens you are welcome to keep it in your fork ofc.

## Licensing

OpenFeed is licensed under the **GNU Affero General Public License v3.0**. By opening a
pull request you agree that your contribution is licensed under the same terms. There is
no separate contributor licence agreement and no copyright assignment — you keep the
copyright to what you write.

If you contribute code you did not write, say so and identify its licence. Anything
incompatible with AGPL-3.0 cannot be merged.
