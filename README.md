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

Tested on Ubuntu 24.04 (Noble) with Python 3.12. These instructions assume the project lives at `/home/ubuntu/a2aj-api-public/`.

### Prerequisites

- Python 3.11+ (tested on 3.12)
- MongoDB 8.0 — we run it in Docker (see `a2aj_internal_instructions/` for the compose file and tuning notes)
- Elasticsearch 9.x — usually on a separate VM, reachable from this host (see 'a2aj_internal_instructions/' for details about setting up elastic search)

### 1. Clone the repository

```bash
sudo apt update && sudo apt install -y git
cd /home/ubuntu
git clone https://github.com/a2aj-ca/a2aj-api-public.git
cd /home/ubuntu/a2aj-api-public
```

### 2. Install the Python venv package

```bash
sudo apt install -y python3.12-venv
```

### 3. Create the virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

To exit the venv later, run `deactivate`.

### 4. Add a swap file

The weekly update can spike memory while loading parquet shards. A swap file turns transient spikes into "slow but completes" instead of OOM kills.

```bash
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

Persist across reboots:

```bash
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Lower swappiness so MongoDB's hot cache stays in RAM under normal conditions:

```bash
sudo sysctl vm.swappiness=10
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf
```

### 5. Configure environment variables

```bash
cp .env.example .env
# then edit .env
```

| Variable | Description |
|---|---|
| `MONGO_URL` | MongoDB connection string (e.g., `mongodb://localhost:27017/`) |
| `ELASTICSEARCH_URL` | Elasticsearch URL (e.g., `http://<hostname>:9200`) |
| `HF_TOKEN` | HuggingFace token (optional, for private datasets) |

### 6. Initial data load

Populate MongoDB and Elasticsearch from the HuggingFace datasets:

```bash
.venv/bin/python weekly_update.py
```

This will:

1. Fetch coverage and boosting config from GitHub → `cache/`
2. Stream the case-law and laws datasets from HuggingFace, writing to both MongoDB and Elasticsearch in a single pass (memory-bounded via pyarrow row-group iteration)
3. Build MongoDB indexes
4. Finalize Elasticsearch indices (refresh, force-merge)
5. Atomically swap the new collections / aliases into the live `canadian-case-law` and `canadian-laws` names

### 7. Run as systemd services (auto-restart on reboot)

This repo runs as two services: the FastAPI app (`uvicorn`) and the standalone MCP server (`mcp-server`). 

#### 7a. API service

```bash
sudo nano /etc/systemd/system/uvicorn.service
```

```ini
[Unit]
Description=Uvicorn daemon for A2AJ Canadian Legal Data API
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/a2aj-api-public
Environment="PATH=/home/ubuntu/a2aj-api-public/.venv/bin"
ExecStart=/home/ubuntu/a2aj-api-public/.venv/bin/uvicorn main_api:app --host localhost --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

#### 7b. Standalone MCP service

```bash
sudo nano /etc/systemd/system/mcp-server.service
```

```ini
[Unit]
Description=FastMCP daemon for Canadian Legal Data MCP Server
After=network.target uvicorn.service
Requires=uvicorn.service

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/a2aj-api-public
Environment="PATH=/home/ubuntu/a2aj-api-public/.venv/bin"
ExecStart=/home/ubuntu/a2aj-api-public/.venv/bin/python main_mcp.py --port 8001
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

#### 7c. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now uvicorn mcp-server
sudo systemctl status uvicorn mcp-server
```

> If you set up the project as `root` and only later switched to running services as `ubuntu`, fix file ownership first:
> ```bash
> sudo chown -R ubuntu:ubuntu /home/ubuntu/a2aj-api-public
> ```


### 8. Schedule the weekly update

```bash
crontab -e
```

Add:

```
0 15 * * 0  cd /home/ubuntu/a2aj-api-public && .venv/bin/python weekly_update.py >> logs/weekly_update.log 2>&1 && sudo systemctl restart uvicorn
```

Runs every Sunday at 15:00 UTC. Logs append to `logs/weekly_update.log`.

> NOTE: restart will require passwordless sudo for ubuntu user:
>```bash
>ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart uvicorn, /usr/bin/systemctl restart mcp-server
>```

## Day-to-day operations

### Returning to the project

```bash
cd /home/ubuntu/a2aj-api-public
source .venv/bin/activate
# ... do work ...
deactivate
```

`.venv/` is persistent — you don't reinstall packages. After a `git pull` that changes `requirements.txt`, re-run `pip install -r requirements.txt`.

### Restart after code changes

```bash
sudo systemctl restart uvicorn      # after main_api.py changes
sudo systemctl restart mcp-server   # after main_mcp.py changes
```

### View logs

```bash
journalctl -u uvicorn.service -n 100 -f
journalctl -u mcp-server.service -n 100 -f
tail -f logs/weekly_update.log
tail -f logs/api.log
```

### Inspect Elasticsearch state

```bash
export ES_URL="http://<your-es-host>:9200"

# All indices and which alias they're behind
curl -s "$ES_URL/_cat/indices/canadian-*?v&s=index"
curl -s "$ES_URL/_cat/aliases/canadian-*?v"

# Find orphan indices (not pointed to by any alias)
comm -23 \
  <(curl -s "$ES_URL/_cat/indices/canadian-*?h=index" | sort) \
  <(curl -s "$ES_URL/_cat/aliases/canadian-*?h=index" | sort)

# Delete an orphan
curl -X DELETE "$ES_URL/canadian-case-law-YYYYMMDD"
```

### Inspect MongoDB state

```bash
docker exec sr-mongodb mongosh a2aj-api --quiet --eval '
  db.getCollectionInfos().forEach(c => {
    const s = db.getCollection(c.name).stats();
    print(c.name.padEnd(30), String(s.count).padStart(10), (s.size/1024/1024).toFixed(1) + " MB");
  })
'
```

Expected collections: `canadian-case-law`, `canadian-laws`, `fs.files`, `fs.chunks` (GridFS internals for laws >5 MB). Anything with `-new` or `-old` in the name is an orphan from a failed run and can be safely dropped:

```bash
docker exec sr-mongodb mongosh a2aj-api --quiet --eval '
  db.getCollection("canadian-case-law-new").drop()
'
```

## Weekly Update — failure handling

The weekly update uses atomic swaps (MongoDB collection rename + Elasticsearch alias swap) to avoid downtime. If any step fails, the old data remains live and queryable, and temporary collections/indices are cleaned up automatically. Check `logs/weekly_update.log` for details.

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