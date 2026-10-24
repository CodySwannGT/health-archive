# Health archive MCP server

A Streamable HTTP MCP server with a built-in OAuth 2.1 authorization server. Each account owns one patient chart, and every tool call acts only on the signed-in account's chart. The [plugin README](../plugins/health-archive/README.md) covers client installation, tools and skills.

## How accounts work

- **Invitation-only sign-up.** People sign up on the authorization page with a one-time invitation. Without invitations, nobody can create an account, so the service holds no health data from people the operator did not invite.
- **A private archive per account.** Sign-up creates a user, a patient chart and owner access, then seeds that chart's archive with the generic template in `template/`: the ontology and schemas.
- **Credentials.** Passwords are stored as scrypt hashes. Codes, tokens and invitations are stored as SHA-256 hashes in the `health_oauth` schema, which the read-only role cannot access. Five failed sign-ins lock a username for 15 minutes, and 50 failures across all accounts lock sign-in globally.
- **Isolation.** Tokens are bound to the user who approved them. Read tools run as that user through the read-only role, whose row-level security admits only that user's patients. Write tools reject another patient's record slug.

## Operate

| Command | Effect |
| --- | --- |
| `python -m health_mcp serve` | Serve HTTP with OAuth. Needs `HEALTH_MCP_PUBLIC_URL` and database settings |
| `python -m health_mcp stdio` | Serve locally over stdio as the local profile's account |
| `python -m health_mcp invite --days 14 --out FILE` | Create a one-time sign-up invitation |
| `python -m health_mcp hash-password < FILE \| python -m health_mcp bind-account NAME USER_ID` | Give an existing user, such as the first owner created by `healthdb bootstrap`, a sign-in account |

Run the commands from `mcp_server/` with the project environment, for example `../database/.venv/bin/python -m health_mcp invite`. Administrative commands use the machine's database administrator profile.

| Path | Purpose |
| --- | --- |
| `health_mcp/app.py` | Tools, using the shared `healthdb` query functions and single-transaction write path |
| `health_mcp/oauth.py` | OAuth provider, sign-in, sign-up and revocation |
| `health_mcp/accounts.py` | Accounts, invitations and new patient archives |
| `Dockerfile`, `railway/` | Image built from a staged context of code, the template archive and the embedding model files |
| `tests/test_server.py` | Unit tests |
| `tests/connector_flow.py` | End-to-end check for an existing account: discovery, registration, PKCE, sign-in, tools, rotation, revocation |
| `tests/multi_user_flow.py` | End-to-end isolation check with two invited accounts. Run it only against a test database |

## Deploy

The image needs no patient data or local profile; it reads everything from the database. The container entrypoint writes the connection profile from these variables:
- `HEALTH_DB_HOST` and `HEALTH_DB_PORT`;
- `HEALTH_READER_PASSWORD` and `HEALTH_ADMIN_PASSWORD`;
- `HEALTH_DB_TLS_CERT_PEM`;
- `HEALTH_MCP_PUBLIC_URL`.

On Railway:

```sh
stage=$(mktemp -d) && mcp_server/railway/stage.sh "$stage" && railway up "$stage" --path-as-root --service health-mcp --ci
```

Verify a deployment with `HEALTH_MCP_TEST_USERNAME=NAME HEALTH_MCP_TEST_PASSWORD_FILE=FILE python mcp_server/tests/connector_flow.py https://YOUR-SERVER`.
