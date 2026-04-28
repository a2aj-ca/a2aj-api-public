# Elasticsearch via Docker Compose

A single-node Elasticsearch instance. Security disabled and no `ports` mapping; container shares the host's network so ES is reachable on whatever interfaces the host has — typically loopback and Tailscale. Public exposure is blocked at the cloud security group; cross-VM access happens over Tailscale (e.g. `http://<tailnet-hostname>:9200`).

Do not adopt this pattern if the host has a public-facing interface or is on an untrusted network.

Up to date as of April 28, 2026.

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

## 2. Set kernel parameter required by Elasticsearch

```bash
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.d/99-elasticsearch.conf
```

The first command applies it now; the second persists across reboots.

## 3. Disable swap

ES performance degrades catastrophically if any heap page is swapped. Disable swap if any is present:

```bash
free -h | grep Swap
# If non-zero:
sudo swapoff -a
sudo sed -i.bak '/ swap / s/^/#/' /etc/fstab
```

## 4. Project directory

```bash
mkdir -p /home/ubuntu/elasticsearch && cd /home/ubuntu/elasticsearch
```

## 5. `docker-compose.yml`

```yaml
services:
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:9.3.3
    container_name: elasticsearch
    network_mode: host
    environment:
      - node.name=elasticsearch
      - cluster.name=local-cluster
      - discovery.type=single-node
      - bootstrap.memory_lock=true
      - "ES_JAVA_OPTS=-Xms12g -Xmx12g"
      - xpack.security.enabled=false
    ulimits:
      memlock:
        soft: -1
        hard: -1
    volumes:
      - es_data:/usr/share/elasticsearch/data
    mem_limit: 14g
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9200/_cluster/health || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  es_data:
```

Memory tuning rules:

- Heap (`-Xms`/`-Xmx`) ≈ 50% of host RAM, capped at ~30 GB. Lower it if other workloads share the host.
- `mem_limit` ≈ heap + 25% to cover JVM overhead. Leaves the rest of host RAM for the OS filesystem cache, which is what makes ES queries fast.
- Heap min and max must be identical; ES requires a fixed heap.

Values above suit a 16 GB host where ES is the primary workload (8 GB heap, 10 GB container limit, ~6 GB free for OS cache).

## 6. Launch

```bash
docker compose up -d
docker compose ps
```

The container takes 30–60 seconds to become healthy on first boot.

## 7. Verify

```bash
# Cluster reachable
curl http://localhost:9200

# memlock active (should be true)
docker exec elasticsearch curl -s http://localhost:9200/_nodes/process?pretty | grep mlockall

# Cluster health (yellow is normal for single-node; green never reachable without replicas)
curl http://localhost:9200/_cluster/health?pretty
```

From another host on the tailnet (using MagicDNS):

```bash
curl http://<tailnet-hostname>:9200
```

## Gotchas

- `network_mode: host` exposes ES on every interface the host has. This pattern depends on the host having no public-facing interface — confirm with `ip -4 addr` that there is no globally-routable IP attached.
- `vm.max_map_count` must be at least 262144 or ES fails to start with mmap errors.
- Swap must be disabled. `bootstrap.memory_lock=true` is a backstop, not a substitute.
- Heap must be set to identical `-Xms` and `-Xmx`.
- `bootstrap.memory_lock=true` plus `ulimits.memlock` prevents heap from swapping. Keep both.
- `logging` block is required to prevent unbounded log file growth.
- Single-node clusters report `yellow` health, not `green` — replicas can't be assigned without a second node. This is expected.