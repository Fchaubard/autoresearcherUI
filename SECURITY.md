# Security

## Deployment model

By default, `setup.sh` exposes the dashboard through a **public cloudflared
quick-tunnel URL**. The user-set passcode is the whole perimeter for dashboard
control. Anyone with the URL and passcode has full control of the node through
the dashboard, including its terminals, files, agents, and settings.
This is a single-user tool, not a multi-user service with separate permissions.

A blank passcode disables the gate. Fresh installs are unprotected until a
passcode is set during onboarding. Some routes remain public, including login,
health checks, onboarding, and separately token-gated paper sharing.

## Use it safely

- Set a strong, unique passcode before using a public tunnel. Rotate it in
  Settings periodically and immediately if it is shared or exposed.
- Treat the tunnel URL and passcode as private. Avoid passcodes in URLs: they can
  appear in browser history and logs.
- For sensitive work, use `bash setup.sh --no-tunnel` and access the dashboard
  through a private Tailscale connection or an SSH tunnel. Keep the backend port
  off the public internet; disabling cloudflared alone is not a firewall rule.
- Never commit API keys or credentials. Settings stores Claude/Anthropic,
  Gemini, and OpenAI tokens, along with other configured credentials. Protect
  the node, settings database, backups, and exported logs accordingly.
- Redact credentials, passcodes, tunnel URLs, and private research data before
  sharing screenshots, recordings, issue reports, or support logs.

## Report a vulnerability

Do not post credentials or exploitable details in a public issue. Use
[GitHub's private vulnerability reporting page](https://github.com/Fchaubard/autoresearcherUI/security/advisories/new)
when available, or email the maintainer at [fchaubard@gmail.com](mailto:fchaubard@gmail.com).
Include the affected version or commit, reproduction steps, expected impact,
and any suggested fix. Send a minimal reproduction without real credentials.
