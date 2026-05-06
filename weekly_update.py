#!/usr/bin/env python3
"""
Weekly Update Script
====================
Pulls Canadian legal data from HuggingFace datasets and GitHub config files,
loads into MongoDB and Elasticsearch with atomic swap to minimize downtime.

Run via cron on Sundays:
  0 15 * * 0  cd /home/ubuntu/a2aj-api-public && .venv/bin/python weekly_update.py
"""
import gc
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pyarrow.parquet as pq
import requests
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from gridfs import GridFS
from huggingface_hub import HfFileSystem
from pymongo import ASCENDING, MongoClient

load_dotenv()

# ────────── Configuration ────────────────────────────────────────────────── #
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017/")
MONGO_DB = "a2aj-api"
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
HF_TOKEN = os.getenv("HF_TOKEN", None)

# HuggingFace dataset repos
HF_CASES_REPO = "a2aj/canadian-case-law"
HF_LAWS_REPO = "a2aj/canadian-laws"

# GitHub raw URLs for config files
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/a2aj-ca/canadian-legal-data/main/UTILS"
GITHUB_FILES = {
    "case_coverage.json": f"{GITHUB_RAW_BASE}/case_coverage.json",
    "laws_coverage.json": f"{GITHUB_RAW_BASE}/laws_coverage.json",
    "search_boosting.json": f"{GITHUB_RAW_BASE}/search_boosting.json",
}

CACHE_DIR = Path("cache")
LOG_DIR = Path("logs")

# Parquet streaming — rows per pyarrow batch (memory-bounded)
PARQUET_BATCH_SIZE = 500

# ES settings
MAX_ANALYZED_OFFSET = 10_000_000
ES_CHUNK_SIZE = 200                       # smaller chunks = less buffered
ES_MAX_CHUNK_BYTES = 2 * 1024 * 1024
ES_REQUEST_TIMEOUT = 300

# GridFS threshold for large law fields (5 MB)
GRIDFS_THRESHOLD = 5_000_000

# Mongo batch size for inserts
MONGO_BATCH_SIZE = 1000

# ────────── Logging ───────────────────────────────────────────────────────── #
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format=">>> %(levelname)s | %(asctime)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "weekly_update.log", mode="a"),
    ],
)
# Quiet down chatty third-party loggers — they produce one INFO line
# per HTTP request, which means thousands per run.
for noisy in (
    "elastic_transport.transport",
    "elasticsearch",
    "httpx",
    "huggingface_hub",
    "urllib3",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ────────── Helpers: row → doc conversions ──────────────────────────────── #
def _clean_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return v
    return v


def _row_to_doc(row: Dict[str, Any]) -> Dict[str, Any]:
    """Strip nulls/empties, leave native Python types ready for Mongo."""
    doc = {}
    for k, v in row.items():
        cleaned = _clean_value(v)
        if cleaned is not None and cleaned != "":
            doc[k] = cleaned
    return doc


def _doc_to_es(doc: Dict[str, Any], exclude_fields: Optional[set] = None) -> Dict[str, Any]:
    """Convert a Mongo-ready doc to an ES-ready source dict."""
    es_doc = {}
    for k, v in doc.items():
        if exclude_fields and k in exclude_fields:
            continue
        if k.endswith("_file_id"):
            continue
        if isinstance(v, datetime):
            es_doc[k] = v.isoformat()
        else:
            es_doc[k] = v
    return es_doc


# ────────── HF parquet streaming ────────────────────────────────────────── #
def iter_hf_parquet_rows(repo_id: str) -> Iterator[Dict[str, Any]]:
    """Stream rows from every train.parquet shard of a HF dataset repo.

    Uses pyarrow's iter_batches for true row-group-level streaming, with
    explicit gc.collect() between shards to release decompression buffers
    before the next shard is loaded. This is what keeps Python memory flat
    instead of climbing toward an OOM kill.
    """
    fs = HfFileSystem(token=HF_TOKEN)
    pattern = f"datasets/{repo_id}/**/train.parquet"
    files = sorted(fs.glob(pattern))
    if not files:
        raise RuntimeError(f"No parquet shards matched pattern {pattern}")
    logger.info("Found %d parquet shard(s) for %s", len(files), repo_id)

    for path in files:
        logger.info("Opening shard %s", path)
        with fs.open(path, "rb") as f:
            pf = pq.ParquetFile(f)
            for batch in pf.iter_batches(batch_size=PARQUET_BATCH_SIZE):
                for row in batch.to_pylist():
                    yield row
            del pf
        gc.collect()


# ────────── GitHub Cache Refresh ─────────────────────────────────────────── #
def refresh_github_caches() -> bool:
    """Fetch GitHub JSON files and write to cache/. Returns True on success."""
    CACHE_DIR.mkdir(exist_ok=True)
    all_ok = True
    for filename, url in GITHUB_FILES.items():
        try:
            logger.info("Fetching %s from GitHub", filename)
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            cache_path = CACHE_DIR / filename
            with open(cache_path, "w") as f:
                json.dump(resp.json(), f)
            logger.info("Cached %s (%d bytes)", filename, len(resp.content))
        except Exception:
            logger.exception("Failed to fetch %s — will use stale cache if available", filename)
            all_ok = False
    return all_ok


# ────────── Mongo: indexes ──────────────────────────────────────────────── #
def create_mongo_indexes(db, collection_name: str):
    collection = db[collection_name]
    collection.create_index([("citation_en", ASCENDING)])
    collection.create_index([("citation_fr", ASCENDING)])
    collection.create_index([("dataset", ASCENDING)])
    collection.create_index([("document_date_en", ASCENDING)])
    collection.create_index([("document_date_fr", ASCENDING)])
    collection.create_index([
        ("dataset", ASCENDING),
        ("document_date_en", ASCENDING),
        ("document_date_fr", ASCENDING),
    ])
    logger.info("Created indexes on %s", collection_name)


# ────────── ES: index lifecycle ─────────────────────────────────────────── #
def create_es_index(es: Elasticsearch, index_name: str):
    """Create an ES index with bulk-friendly settings.
    refresh_interval=-1 and number_of_replicas=0 speed up ingest;
    best_compression shrinks the index on disk. These are restored
    by finalize_es_index after the import completes."""
    es.indices.create(
        index=index_name,
        body={
            "settings": {
                "index": {
                    "highlight": {
                        "max_analyzed_offset": MAX_ANALYZED_OFFSET,
                    },
                    "refresh_interval": "-1",
                    "number_of_replicas": 0,
                    "codec": "best_compression",
                }
            }
        },
    )
    logger.info("Created ES index %s", index_name)


def finalize_es_index(es: Elasticsearch, index_name: str):
    """Restore production settings, refresh, and force-merge after bulk import."""
    logger.info("Finalizing ES index %s: restoring refresh_interval and replicas", index_name)
    es.options(request_timeout=120).indices.put_settings(
        index=index_name,
        body={
            "index": {
                "refresh_interval": "1s",
                "number_of_replicas": 1,
            }
        },
    )
    logger.info("Refreshing ES index %s", index_name)
    es.options(request_timeout=600).indices.refresh(index=index_name)
    logger.info("Force-merging ES index %s to 1 segment (this may take a while)", index_name)
    es.options(request_timeout=3600).indices.forcemerge(
        index=index_name, max_num_segments=1
    )
    logger.info("Finalized ES index %s", index_name)


# ────────── Single-pass: cases ──────────────────────────────────────────── #
def import_cases(db, es: Elasticsearch, mongo_name: str, es_index: str):
    """Stream the cases dataset once, fanning out to Mongo and ES.
    Returns (mongo_count, es_success, es_fail)."""
    coll = db[mongo_name]
    es_t = es.options(request_timeout=ES_REQUEST_TIMEOUT)

    mongo_buf = []
    es_buf = []
    mongo_total = 0
    es_success = 0
    es_fail = 0

    def flush_mongo():
        nonlocal mongo_total
        if not mongo_buf:
            return
        coll.insert_many(mongo_buf, ordered=False)
        mongo_total += len(mongo_buf)
        mongo_buf.clear()

    def flush_es():
        nonlocal es_success, es_fail
        if not es_buf:
            return
        for ok, item in helpers.streaming_bulk(
            es_t,
            iter(es_buf),
            chunk_size=ES_CHUNK_SIZE,
            max_chunk_bytes=ES_MAX_CHUNK_BYTES,
            raise_on_error=False,
        ):
            if ok:
                es_success += 1
            else:
                es_fail += 1
                if es_fail <= 5:
                    logger.warning("Failed to index doc: %s", item)
        es_buf.clear()

    for row in iter_hf_parquet_rows(HF_CASES_REPO):
        doc = _row_to_doc(row)
        if not doc:
            continue
        mongo_buf.append(doc)
        es_buf.append({"_index": es_index, "_source": _doc_to_es(doc)})

        if len(mongo_buf) >= MONGO_BATCH_SIZE:
            flush_mongo()
            flush_es()
            if mongo_total % 10000 == 0:
                logger.info("Cases progress: %d mongo / %d ES", mongo_total, es_success)

    flush_mongo()
    flush_es()
    logger.info("Cases import complete: %d mongo / %d ES success / %d ES fail",
                mongo_total, es_success, es_fail)
    return mongo_total, es_success, es_fail


# ────────── Single-pass: laws ───────────────────────────────────────────── #
LAWS_GRIDFS_FIELDS = (
    "unofficial_text_en", "unofficial_text_fr",
    "unofficial_sections_en", "unofficial_sections_fr",
)
LAWS_ES_EXCLUDE = {"unofficial_sections_en", "unofficial_sections_fr"}


def _process_law_row(row: Dict[str, Any], fs: GridFS):
    """Convert a raw HF row into (mongo_doc, es_doc).

    - Parse unofficial_sections_{en,fr} from JSON string to dict.
    - Compute num_sections_{en,fr}.
    - Build ES doc BEFORE moving fields to GridFS so search content is preserved.
    - Move oversized fields to GridFS in the mongo doc.
    """
    doc = _row_to_doc(row)
    if not doc:
        return None, None

    # Parse sections from JSON string to dict (mongo side)
    for sec_field in ("unofficial_sections_en", "unofficial_sections_fr"):
        if sec_field in doc and isinstance(doc[sec_field], str):
            try:
                doc[sec_field] = json.loads(doc[sec_field])
            except (json.JSONDecodeError, TypeError):
                pass

    # Compute num_sections
    for lang in ("en", "fr"):
        sec_field = f"unofficial_sections_{lang}"
        num_field = f"num_sections_{lang}"
        if sec_field in doc and isinstance(doc[sec_field], dict):
            doc[num_field] = len(doc[sec_field])

    # Build ES doc BEFORE shoving fields into GridFS — we want the
    # text content searchable in ES, not just a file_id.
    es_doc = _doc_to_es(doc, exclude_fields=LAWS_ES_EXCLUDE)

    # Move large fields to GridFS for the mongo doc
    for field in LAWS_GRIDFS_FIELDS:
        if field not in doc:
            continue
        value = doc[field]
        if isinstance(value, dict):
            data = json.dumps(value).encode("utf-8")
        elif isinstance(value, str):
            data = value.encode("utf-8")
        else:
            continue
        if len(data) > GRIDFS_THRESHOLD:
            file_id = fs.put(data)
            doc[f"{field}_file_id"] = file_id
            del doc[field]

    return doc, es_doc


def import_laws(db, es: Elasticsearch, mongo_name: str, es_index: str):
    """Stream the laws dataset once, fanning out to Mongo (with GridFS) and ES."""
    coll = db[mongo_name]
    fs = GridFS(db)
    es_t = es.options(request_timeout=ES_REQUEST_TIMEOUT)

    mongo_buf = []
    es_buf = []
    mongo_total = 0
    es_success = 0
    es_fail = 0

    def flush_mongo():
        nonlocal mongo_total
        if not mongo_buf:
            return
        coll.insert_many(mongo_buf, ordered=False)
        mongo_total += len(mongo_buf)
        mongo_buf.clear()

    def flush_es():
        nonlocal es_success, es_fail
        if not es_buf:
            return
        for ok, item in helpers.streaming_bulk(
            es_t,
            iter(es_buf),
            chunk_size=ES_CHUNK_SIZE,
            max_chunk_bytes=ES_MAX_CHUNK_BYTES,
            raise_on_error=False,
        ):
            if ok:
                es_success += 1
            else:
                es_fail += 1
                if es_fail <= 5:
                    logger.warning("Failed to index doc: %s", item)
        es_buf.clear()

    for row in iter_hf_parquet_rows(HF_LAWS_REPO):
        mongo_doc, es_doc = _process_law_row(row, fs)
        if mongo_doc is None:
            continue
        mongo_buf.append(mongo_doc)
        es_buf.append({"_index": es_index, "_source": es_doc})

        if len(mongo_buf) >= MONGO_BATCH_SIZE:
            flush_mongo()
            flush_es()
            if mongo_total % 5000 == 0:
                logger.info("Laws progress: %d mongo / %d ES", mongo_total, es_success)

    flush_mongo()
    flush_es()
    logger.info("Laws import complete: %d mongo / %d ES success / %d ES fail",
                mongo_total, es_success, es_fail)
    return mongo_total, es_success, es_fail


# ────────── Atomic Swap ─────────────────────────────────────────────────── #
def swap_mongo_collection(db, live_name: str, new_name: str):
    """Atomically swap a new collection into the live name."""
    old_name = f"{live_name}-old"
    if live_name in db.list_collection_names():
        db[live_name].rename(old_name)
        logger.info("Renamed %s -> %s", live_name, old_name)
    db[new_name].rename(live_name)
    logger.info("Renamed %s -> %s", new_name, live_name)
    if old_name in db.list_collection_names():
        db.drop_collection(old_name)
        logger.info("Dropped %s", old_name)


def swap_es_index(es: Elasticsearch, alias_name: str, new_index: str):
    """Atomically swap an ES alias to point to a new index."""
    actions = []
    try:
        current = es.indices.get_alias(name=alias_name)
        for old_index in current:
            actions.append({"remove": {"index": old_index, "alias": alias_name}})
    except Exception:
        # Alias doesn't exist yet (first run)
        pass
    actions.append({"add": {"index": new_index, "alias": alias_name}})
    es.indices.update_aliases(body={"actions": actions})
    logger.info("Swapped ES alias %s -> %s", alias_name, new_index)
    for action in actions:
        if "remove" in action:
            old_idx = action["remove"]["index"]
            try:
                es.indices.delete(index=old_idx)
                logger.info("Deleted old ES index %s", old_idx)
            except Exception:
                logger.warning("Failed to delete old ES index %s", old_idx)


# ────────── Cleanup on Failure ──────────────────────────────────────────── #
def cleanup_temps(db, es: Elasticsearch, temp_names: Dict[str, list]):
    """Drop temporary collections and indices on failure.
    Only operates on names still in temp_names — the caller must remove
    names once they have been promoted to live, so this never destroys
    freshly-promoted production data."""
    for name in temp_names.get("mongo", []):
        if name in db.list_collection_names():
            db.drop_collection(name)
            logger.info("Cleaned up temp mongo collection %s", name)
    for name in temp_names.get("es", []):
        try:
            if es.indices.exists(index=name):
                es.indices.delete(index=name)
                logger.info("Cleaned up temp ES index %s", name)
        except Exception:
            pass


# ────────── Main ────────────────────────────────────────────────────────── #
def main():
    start_time = time.time()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    logger.info("=" * 60)
    logger.info("Weekly update started at %s", datetime.now(timezone.utc).isoformat())

    logger.info("Step 1: Refreshing GitHub caches")
    refresh_github_caches()

    mongo_client = MongoClient(MONGO_URL)
    db = mongo_client[MONGO_DB]
    es = Elasticsearch(ES_URL, request_timeout=ES_REQUEST_TIMEOUT)

    cases_mongo_temp = "canadian-case-law-new"
    laws_mongo_temp = "canadian-laws-new"
    cases_es_temp = f"canadian-case-law-{timestamp}"
    laws_es_temp = f"canadian-laws-{timestamp}"

    # Items in this dict will be deleted by cleanup_temps on failure.
    # As soon as a temp resource is swapped into live, we MUST remove it
    # from this dict so the cleanup handler doesn't destroy live data.
    temp_names = {
        "mongo": [cases_mongo_temp, laws_mongo_temp],
        "es": [cases_es_temp, laws_es_temp],
    }

    try:
        # Clean up any leftover temps from a previous failed run
        cleanup_temps(db, es, temp_names)

        # ── Cases ────────────────────────────────────────────────────────
        logger.info("Step 2: Importing cases (single pass: mongo + ES)")
        create_es_index(es, cases_es_temp)
        cases_mongo_count, cases_es_count, _ = import_cases(
            db, es, cases_mongo_temp, cases_es_temp,
        )
        gc.collect()

        logger.info("Step 2b: Finalizing ES cases index")
        finalize_es_index(es, cases_es_temp)

        logger.info("Step 2c: Building Mongo cases indexes")
        create_mongo_indexes(db, cases_mongo_temp)

        logger.info("Step 3: Swapping cases (mongo + ES)")
        swap_mongo_collection(db, "canadian-case-law", cases_mongo_temp)
        swap_es_index(es, "canadian-case-law", cases_es_temp)
        # Cases are now live — remove from cleanup list so a later
        # failure doesn't destroy the freshly-promoted live data.
        temp_names["mongo"].remove(cases_mongo_temp)
        temp_names["es"].remove(cases_es_temp)

        gc.collect()

        # ── Laws ─────────────────────────────────────────────────────────
        logger.info("Step 4: Importing laws (single pass: mongo + ES)")
        create_es_index(es, laws_es_temp)
        laws_mongo_count, laws_es_count, _ = import_laws(
            db, es, laws_mongo_temp, laws_es_temp,
        )
        gc.collect()

        logger.info("Step 4b: Finalizing ES laws index")
        finalize_es_index(es, laws_es_temp)

        logger.info("Step 4c: Building Mongo laws indexes")
        create_mongo_indexes(db, laws_mongo_temp)

        logger.info("Step 5: Swapping laws (mongo + ES)")
        swap_mongo_collection(db, "canadian-laws", laws_mongo_temp)
        swap_es_index(es, "canadian-laws", laws_es_temp)
        temp_names["mongo"].remove(laws_mongo_temp)
        temp_names["es"].remove(laws_es_temp)

        elapsed = time.time() - start_time
        logger.info(
            "Weekly update completed successfully in %.0f seconds. "
            "Cases: %d mongo / %d ES. Laws: %d mongo / %d ES.",
            elapsed, cases_mongo_count, cases_es_count,
            laws_mongo_count, laws_es_count,
        )
    except Exception:
        logger.exception("Weekly update FAILED — retaining old data")
        cleanup_temps(db, es, temp_names)
        mongo_client.close()
        sys.exit(1)
    finally:
        mongo_client.close()


if __name__ == "__main__":
    main()