# Security Policy

## Reporting

Do not disclose suspected vulnerabilities in a public issue. Contact the repository owner privately with reproduction steps, affected versions, and potential impact.

## Deployment Guidance

- Keep `.env` readable only by the deployment user.
- Bind ports to loopback unless a trusted reverse proxy provides TLS and access control.
- Enable OIDC login before exposing OpenFeed outside a trusted network.
- Do not publish database, Redis, Qdrant, or Typesense ports.
- Use independent random credentials for every service.
- Keep Docker images and host packages updated.
- Back up and test restoration of persistent data.

OpenFeed is in alpha. Only the latest revision receives security fixes, and there are no
backports to earlier revisions.
