# Security

This project stores personal health information. Report vulnerabilities privately through GitHub's private vulnerability reporting for this repository. Don't open a public issue.

Include the affected component (`mcp_server`, `database`, or `plugins`), steps to reproduce, and the impact. Never include real health data in a report; use synthetic data.

Areas of particular interest:
- account isolation and row-level security;
- the OAuth authorization server: registration, PKCE, sign-in, invitations, rotation and revocation;
- the write path's guards against stale or record-dropping archive versions;
- anything that could place archive data in this public repository.
