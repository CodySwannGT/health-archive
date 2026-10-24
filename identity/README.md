# Local identity profile

Command-line tools act as the profile's default user and patient. Put the profile at `~/.config/health-archive/local-profile.json`, or point `HEALTH_PROFILE` at it. Keep it out of any code checkout. Start from `local-profile.example.json`, and generate fresh identifiers in the same `usr_` and `pat_` 32-hex-digit format.

| Patient field | Purpose |
| --- | --- |
| `legacy_patient_scopes` | The record slug, first entry, plus any earlier labels used in records |
| `timezone` | IANA time zone for reports |
| `source_identity` | Optional. `printed_name_pattern` (a case-insensitive regular expression for your name as providers print it), `quest_printed_name` and `date_of_birth`. Provider importers use these to confirm a document is yours |
| `session_workspaces` | Optional, top level. Folders whose local assistant sessions `healthdb capture-conversation` may capture, besides the checkout itself |

The MCP server doesn't use this file. Its accounts and patients live in the database.
