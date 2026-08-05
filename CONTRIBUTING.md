# Contributing to OpenFeed

Thanks for your interest. Contributions are welcome, and the process is deliberately
simple: **fork, branch, open a pull request, and it gets reviewed before it merges.**

OpenFeed is in **alpha** and has a single maintainer. Every pull request is reviewed by
[@S0nrxse](https://github.com/S0nrxse) before merging(will most likely change in the future but for the moment I would like to decide the direction we take on the project). Expect review to take a few days.

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

1. Create `.env` from `.env.example` and replace all placeholder credentials.

2. Build the application image:

   ```bash
   docker build -t openfeed-app:test app
   ```

3. Run the tests. They need no database and no network:

   ```bash
   docker run --rm openfeed-app:test python -m unittest discover -s openfeed/tests -v
   ```

4. Validate the Compose configuration:

   ```bash
   ./deploy.sh config --quiet
   ```

Note that `app/` is **not** bind-mounted into the container. Rebuild the image to see
your changes, and remember that `openfeed-app` and `openfeed-worker` share one image —
if you change worker behaviour, recreate both.

## Pull requests

Open the pull request against `main` from a branch on your fork. In the description, say
what changed and why; if it fixes an issue, link it.

A pull request needs all of these before it can merge:

- **Tests pass.** `python -m unittest discover -s openfeed/tests` with no database and no
  network.
- **New behaviour has a test.** Bug fixes should have one that fails without the fix.
- **Focused scope.** One concern per pull request. Unrelated cleanups make review slow and leads to even more mistakes

Style follows the surrounding code rather than a linter. Comments should explain *why*
something is the way it is, especially where the obvious approach was wrong — most of the
comments in this codebase exist because something behaved unexpectedly, and that
context is the valuable part.

Maintainer review may ask for changes, and sometimes will decline a change that works.
If that happens you are welcome to keep it in your fork ofc.

## Reporting bugs

Include what you expected, what happened, and how to reproduce it. Relevant logs from
`docker compose logs openfeed-app` help.

**Do not report security vulnerabilities in a public issue.** See [`SECURITY.md`](SECURITY.md).

## Repository hygiene

Never commit:

- `.env` or encrypted production environments
- `compose.local.yml`
- database, cache, log, or backup files
- credentials, private domains, LAN addresses, or absolute deployment paths
- screenshots or media without redistribution rights

## Licensing of contributions

OpenFeed is licensed under the **GNU Affero General Public License v3.0**. By opening a
pull request you agree that your contribution is licensed under the same terms. There is
no separate contributor licence agreement and no copyright assignment — you keep the
copyright to what you write.

If you contribute code you did not write, say so and identify its licence. Anything
incompatible with AGPL-3.0 cannot be merged.
