# Health archive MCP connector. Built from a staged context holding code, the
# new-patient template archive and model files; patient data stays in the database.
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HEALTH_ARCHIVE_ROOT=/app
WORKDIR /app
RUN useradd --create-home --uid 10001 health
COPY database/requirements.lock mcp_server/requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.lock -r /tmp/requirements.txt
COPY template ./template
COPY database/tools ./database/tools
COPY database/sql ./database/sql
COPY symptoms/tools ./symptoms/tools
COPY meds/tools ./meds/tools
COPY mcp_server ./mcp_server
# The exact embedding model files recorded with the indexed embeddings. Search
# runs locally, so health text is never sent to an embedding service.
COPY database/models ./database/models
RUN mkdir -p /app/database/.secrets /app/database/.cache && chown -R health /app/database
USER health
WORKDIR /app/mcp_server
ENTRYPOINT ["/app/mcp_server/railway/entrypoint.sh"]
