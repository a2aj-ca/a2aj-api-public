# Canadian Legal Data API & MCP

API and MCP server for searching and retrieving Canadian case law and legislation. Maintained by [Sean Rehaag](https://www.osgoode.yorku.ca/faculty-and-staff/rehaag-sean/) as part of [A2AJ](https://a2aj.ca) (Access to Algorithmic Justice).

A2AJ hosts a public instance of this API at [api.a2aj.ca/docs](https://api.a2aj.ca/docs) on infrastructure provided by the Digital Research Alliance of Canada. This repository is provided so that individuals and organizations who prefer to host their own infrastructure — for data privacy or other reasons — can do so.

## Architecture

```
HuggingFace datasets ──weekly pull──> MongoDB + Elasticsearch
GitHub config JSON ────weekly pull──> cache/ (local JSON files)

main_api.py (FastAPI)
  ├── /health         → health check
  ├── /coverage       → dataset coverage (from cached GitHub JSON)
  ├── /fetch          → fetch document by citation (from MongoDB)
  ├── /search         → full-text / name search (from Elasticsearch)
  └── /mcp            → embedded MCP (FastApiMCP, for Anthropic/Claude)

main_mcp.py (fastmcp standalone)
  └── Proxies to API via HTTP (for OpenAI-compatible clients)
```

**Data flow:** HuggingFace datasets (`a2aj/canadian-case-law`, `a2aj/canadian-laws`) are pulled weekly and loaded into MongoDB and Elasticsearch. Coverage metadata and search boosting config are fetched from GitHub and cached locally.

## Setup

### Prerequisites

- Python 3.11+
- MongoDB 8.0 (local, Docker recommended)
- Elasticsearch 9.x (on a separate VM or the same host)

Both MongoDB and Elasticsearch must be running before starting the API or running the weekly update. See `a2aj_internal_instructions/` for how we set these up (Docker Compose configs, memory tuning, etc.).

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `MONGO_URL` | MongoDB connection string (e.g., `mongodb://localhost:27017/`) |
| `ELASTICSEARCH_URL` | Elasticsearch URL (e.g., `http://<hostname>:9200`) |
| `HF_TOKEN` | HuggingFace token (optional, for private datasets) |

### Initial Data Load

Run the weekly update script to populate MongoDB and Elasticsearch:

```bash
python weekly_update.py
```

This will:
1. Fetch coverage and boosting config from GitHub → `cache/`
2. Download case law and laws datasets from HuggingFace
3. Load into MongoDB collections with indexes
4. Index into Elasticsearch with proper settings
5. Perform atomic swap to make data live

### Run the API

```bash
uvicorn main_api:app --host 0.0.0.0 --port 8000
```

### Run the standalone MCP server

```bash
python main_mcp.py --port 8001
```

## Weekly Update

The weekly update pulls fresh data from HuggingFace and refreshes all caches. It uses atomic swaps (MongoDB collection rename + Elasticsearch alias swap) to avoid downtime.

### Cron setup

```bash
crontab -e
# Add:
0 15 * * 0  cd /home/sr/a2aj-api-public && .venv/bin/python weekly_update.py >> logs/weekly_update.log 2>&1
```

Runs every Sunday at 3 PM. Logs to `logs/weekly_update.log`.

### Failure handling

If any step fails, the old data remains live and queryable. Temporary collections/indices are cleaned up automatically. Check `logs/weekly_update.log` for details.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Health check |
| `GET /coverage?doc_type=cases\|laws` | Dataset coverage metadata |
| `GET /fetch?citation=...&doc_type=...&output_language=...` | Fetch document by citation |
| `GET /search?query=...&doc_type=...&search_type=...` | Search documents |
| `GET /docs` | Interactive API documentation (Swagger UI) |

See `/docs` for full parameter documentation.

## Rate Limits

- 1,000 requests/hour per IP
- 5,000 requests/day per IP
- Exempt: `/mcp`, `/docs`, `/redoc`, `/openapi.json`, `/health`

For bulk access, use the HuggingFace datasets directly: https://a2aj.ca/data/

## Acknowledgements

This research output is supported in part by funding from the Law Foundation of Ontario and the Social Sciences and Humanities Research Council of Canada, by in-kind compute from the Digital Research Alliance of Canada and by administrative support from the Centre for Refugee Studies, the Refugee Law Lab, and Osgoode Hall Law School.
