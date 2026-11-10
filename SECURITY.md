# Security Policy

## Reporting

**Do not disclose suspected vulnerabilities in a public issue.**

Use GitHub's [private vulnerability reporting](https://github.com/S0nrxse/openFeed/security/advisories/new)
for the repository. That keeps the report private until a fix exists and gives it a
tracked advisory. If it is unavailable, contact the maintainer privately through GitHub
instead.

Include reproduction steps, the version or commit affected, and the impact you believe it
has. Redact anything identifying — reproduction steps for this project tend to contain
hostnames, IP addresses and account details from your own deployment.

Expect an initial response within a week. This is a single-maintainer alpha project, so
there is no formal SLA beyond that.

## Deployment Guidance

- Keep `.env` readable only by the deployment user.
- Bind ports to loopback unless a trusted reverse proxy provides TLS and access control.
- Turn sign-in on (`OPENFEED_AUTH_MODE=accounts` or `authentik_oidc`) before exposing OpenFeed outside a trusted network.
- Do not publish database, Redis, Qdrant, or Typesense ports.
- Use independent random credentials for every service.
- Keep Docker images and host packages updated.
- Back up and test restoration of persistent data.

OpenFeed is in alpha. Only the latest revision receives security fixes, and there are no
backports to earlier revisions.
