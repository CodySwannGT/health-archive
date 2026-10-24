# Health Archive plugin

Skills plus an OAuth-protected MCP connector for a source-backed personal health archive. Each person who signs in gets their own private archive; the database is its system of record. The plugin lets Claude, ChatGPT and Codex query it and record new reports as reviewed, quote-backed facts.

The package follows both the Claude Code plugin format and the vendor-neutral [Agent Plugins 1.0](https://agent-plugins.org) format used by ChatGPT and Codex:

| File | Read by |
| --- | --- |
| `.claude-plugin/plugin.json`, `.mcp.json` | Claude Code and Claude plugins |
| `plugin.json` (with `extensions.com.openai`), `mcp.json` | ChatGPT, Codex and other Agent Plugins clients |
| `skills/*/SKILL.md` | Both. Agent Skills format |
| `assets/` | Icon for both directories |

## Connector

`https://health-mcp-production-91ec.up.railway.app/mcp` (Streamable HTTP)

The server implements the MCP authorization specification:
- protected resource metadata;
- authorization server metadata;
- dynamic client registration;
- authorization code with PKCE S256;
- refresh token rotation;
- RFC 7009 revocation.

Clients discover this automatically. On first connection your browser opens a sign-in page that shows the requesting client and where it will return. Sign in with your username and password, or create an account with an invitation from the operator. Each account owns one patient chart, and tools act only on it. Five failed sign-ins lock that username for 15 minutes.

Scopes: `health.read` for queries, `health.write` for `record_narrative`, `record_medication` and `capture_conversation`.

## Install

| Client | Steps |
| --- | --- |
| **Claude Code** | `/plugin marketplace add CodySwannGT/health-archive`, then `/plugin install health-archive@health-archive-marketplace`. Run `/mcp`, select `health-archive`, and authenticate |
| **Claude.ai or Claude Desktop** | Settings → Connectors → Add custom connector. Enter the connector URL and connect. Upload the skills from `dist/skills/*.zip`, built by `plugins/package.sh`, under Settings → Capabilities → Skills |
| **ChatGPT** | Install the plugin from the repository marketplace, or add the connector directly: Settings → Apps & Connectors → Advanced → Developer mode → Create. Enter the connector URL with OAuth authentication |
| **Codex** | Add this repository's marketplace (`.agents/plugins/marketplace.json`) and enable `health-archive@health-archive-marketplace` in `.codex/config.toml`. Or add only the connector: `codex mcp add health-archive --url https://health-mcp-production-91ec.up.railway.app/mcp` |

## Tools

| Tool | Purpose |
| --- | --- |
| `search`, `fetch` | Search and fetch source documents. These follow the ChatGPT connector search/fetch shape |
| `search_archive` | Search with excerpts and options for research, conversation or history |
| `labs`, `medications`, `symptoms`, `vitals`, `anthropometrics`, `activity`, `profile`, `timeline` | Domain queries |
| `get_record`, `get_document`, `graph`, `ontology`, `status`, `query` | Records, documents, graph, ontology, status and bounded read-only SQL |
| `record_narrative`, `record_medication`, `capture_conversation` | Writes. Each runs in one transaction against the database archive |

## Skills

| Skill | Use |
| --- | --- |
| `health-archive-grounding` | Query the archive before answering, and keep evidence kinds distinct |
| `health-conversation-audit` | Capture each turn and preserve corrections and clarifications |
| `health-record-narrative` | Record symptoms, vitals, weights, exposures and hypotheses as quote-backed records |
| `health-record-medication` | Record regimens, changes, holds and single doses |
| `health-ontology-extension` | Extend schemas and the registered ontology additively |
| `health-source-archive` | Add, retire and back up source files |

## Operate

The server code is in `mcp_server/`, and [mcp_server/README.md](../../mcp_server/README.md) covers deployment, invitations and accounts. Sign-up requires a one-time invitation the operator creates with `python -m health_mcp invite`, so the service never holds data from people the operator did not invite.
