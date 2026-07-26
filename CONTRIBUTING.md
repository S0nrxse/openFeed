# Contributing

## Development

1. Create `.env` from `.env.example` and replace all placeholder credentials.
2. Build the application with `docker build -t openfeed-app:test app`.
3. Run tests with `docker run --rm openfeed-app:test python -m unittest discover -s openfeed/tests -v`.
4. Validate configuration with `./deploy.sh config --quiet`.

Keep changes focused and include tests for behavior changes.

## Repository Hygiene

Never commit:

- `.env` or encrypted production environments
- `compose.local.yml`
- database, cache, log, or backup files
- credentials, private domains, LAN addresses, or absolute deployment paths
- screenshots or media without redistribution rights

Run a secret scanner before opening a pull request.
