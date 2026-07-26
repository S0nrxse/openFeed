# Security Policy

## Reporting

Do not disclose suspected vulnerabilities in a public issue. Contact the repository owner privately with reproduction steps, affected versions, and potential impact.

## Deployment Guidance

- Keep `.env` readable only by the deployment user.
- Bind ports to loopback unless a trusted reverse proxy provides TLS and access control.
- Enable OIDC login before exposing OpenFeed outside a trusted network.
- Do not publish database, Redis, Gorse, Qdrant, or Typesense ports.
- Use independent random credentials for every service.
- Keep Docker images and host packages updated.
- Back up and test restoration of persistent data.

Only the latest revision is expected to receive security fixes while the project is in active development.
