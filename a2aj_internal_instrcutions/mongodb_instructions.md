# Ephemeral MongoDB via Docker Compose

A local, ephemeral MongoDB for use by services on the same VM only. No auth; the port is bound to localhost so nothing off-host can reach it. Do not use this pattern if mongo needs to be reachable from other hosts.

Up to date as of April 28, 2026.

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

## 2. Project directory

```bash
mkdir -p /home/ubuntu/mongodb && cd /home/ubuntu/mongodb
```

## 3. `docker-compose.yml`

```yaml
services:
  mongodb:
    image: mongo:8.0
    container_name: mongodb
    command: ["mongod", "--wiredTigerCacheSizeGB", "4"]
    volumes:
      - mongo_data:/data/db
    ports:
      - "127.0.0.1:27017:27017"
    mem_limit: 8g
    restart: unless-stopped
    healthcheck:
      test: echo 'db.runCommand("ping").ok' | mongosh localhost:27017 --quiet
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  mongo_data:
```

Tune `wiredTigerCacheSizeGB` and `mem_limit` to host RAM. Values above suit a 16 GB host.

## 4. Launch

```bash
docker compose up -d
docker compose ps
```

Connect from the host: `mongodb://127.0.0.1:27017`.

## Gotchas

- Do not change the port binding to `0.0.0.0:27017` without adding authentication.
- `logging` block is required to prevent unbounded log file growth.