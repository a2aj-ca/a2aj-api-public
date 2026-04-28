#!/usr/bin/env python3
"""
Weekly Update Script
====================
Pulls Canadian legal data from HuggingFace datasets and GitHub config files,
loads into MongoDB and Elasticsearch with atomic swap to minimize downtime.

Run via cron on Sundays:
  0 15 * * 0  cd /home/sr/a2aj-api-public && .venv/bin/python weekly_update.py
"""
import os
import sys
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from datasets import load_dataset
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from gridfs import GridFS
from pymongo import MongoClient, ASCENDING

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

# ES settings
MAX_ANALYZED_OFFSET = 10_000_000
ES_CHUNK_SIZE = 500
ES_REQUEST_TIMEOUT = 300

# GridFS threshold for large law fields (5 MB)
GRIDFS_THRESHOLD = 5_000_000

# Mongo batch size for inserts
MONGO_BATCH_SIZE = 500

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
logger = logging.getLogger(__name__)

# ────────── Helpers ───────────────────────────────────────────────────────── #


def _clean_value(v: Any) -> Any:
    """Convert HF dataset values to MongoDB-ready native Python types."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, float):
        import math
        if math.isnan(v):
            return None
        return v
    return v


def _row_to_doc(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a HF dataset row to a MongoDB-ready document, stripping nulls/empties."""
    doc = {}
    for k, v in row.items():
        cleaned = _clean_value(v)
        if cleaned is not None and cleaned != "":
            doc[k] = cleaned
    return doc


def _doc_to_es(doc: Dict[str, Any], exclude_fields: Optional[set] = None) -> Dict[str, Any]:
    """Convert a mongo-ready doc to an ES-ready source dict."""
    es_doc = {}
    for k, v in doc.items():
        if exclude_fields and k in exclude_fields:
            continue
        if k.endswith("_file_id"):
            continue
        # Convert datetime to ISO string for ES
        if isinstance(v, datetime):
            es_doc[k] = v.isoformat()
        else:
            es_doc[k] = v
    return es_doc


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


# ────────── MongoDB Import ────────────────────────────────────────────────── #


def import_cases_to_mongo(db, collection_name: str) -> int:
    """Stream HF case-law dataset into a MongoDB collection. Returns doc count."""
    collection = db[collection_name]
    total = 0

    logger.info("Loading HF dataset %s (streaming)", HF_CASES_REPO)
    ds = load_dataset(HF_CASES_REPO, split="train", streaming=True, token=HF_TOKEN)

    batch_docs = []
    for row in ds:
        doc = _row_to_doc(row)
        if doc:
            batch_docs.append(doc)

        if len(batch_docs) >= MONGO_BATCH_SIZE:
            collection.insert_many(batch_docs)
            total += len(batch_docs)
            batch_docs = []
            if total % 10000 == 0:
                logger.info("Cases mongo: %d docs inserted", total)

    if batch_docs:
        collection.insert_many(batch_docs)
        total += len(batch_docs)

    logger.info("Cases mongo import complete: %d documents", total)
    return total


def import_laws_to_mongo(db, collection_name: str) -> int:
    """Stream HF laws dataset into a MongoDB collection with GridFS for large fields.
    Returns doc count."""
    collection = db[collection_name]
    fs = GridFS(db)
    total = 0

    gridfs_fields = (
        "unofficial_text_en", "unofficial_text_fr",
        "unofficial_sections_en", "unofficial_sections_fr",
    )

    logger.info("Loading HF dataset %s (streaming)", HF_LAWS_REPO)
    ds = load_dataset(HF_LAWS_REPO, split="train", streaming=True, token=HF_TOKEN)

    batch_docs = []
    for row in ds:
        doc = _row_to_doc(row)
        if not doc:
            continue

        # Parse unofficial_sections from JSON string to dict
        for sec_field in ("unofficial_sections_en", "unofficial_sections_fr"):
            if sec_field in doc and isinstance(doc[sec_field], str):
                try:
                    doc[sec_field] = json.loads(doc[sec_field])
                except (json.JSONDecodeError, TypeError):
                    pass

        # Compute num_sections if sections exist
        for lang in ("en", "fr"):
            sec_field = f"unofficial_sections_{lang}"
            num_field = f"num_sections_{lang}"
            if sec_field in doc and isinstance(doc[sec_field], dict):
                doc[num_field] = len(doc[sec_field])

        # Move large fields to GridFS
        for field in gridfs_fields:
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

        batch_docs.append(doc)

        if len(batch_docs) >= MONGO_BATCH_SIZE:
            collection.insert_many(batch_docs)
            total += len(batch_docs)
            batch_docs = []
            if total % 5000 == 0:
                logger.info("Laws mongo: %d docs inserted", total)

    if batch_docs:
        collection.insert_many(batch_docs)
        total += len(batch_docs)

    logger.info("Laws mongo import complete: %d documents", total)
    return total


def create_mongo_indexes(db, collection_name: str):
    """Create standard indexes on a collection."""
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


# ────────── Elasticsearch Import ──────────────────────────────────────────── #


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


def import_cases_to_es(es: Elasticsearch, index_name: str) -> int:
    """Stream HF case-law dataset into an ES index. Returns doc count."""
    logger.info("Loading HF dataset %s for ES (streaming)", HF_CASES_REPO)
    ds = load_dataset(HF_CASES_REPO, split="train", streaming=True, token=HF_TOKEN)

    def generate_actions():
        for row in ds:
            doc = _row_to_doc(row)
            if not doc:
                continue
            es_doc = _doc_to_es(doc)
            yield {"_index": index_name, "_source": es_doc}

    es_with_timeout = es.options(request_timeout=ES_REQUEST_TIMEOUT)
    success_count = 0
    fail_count = 0
    for ok, item in helpers.streaming_bulk(
        es_with_timeout,
        generate_actions(),
        chunk_size=ES_CHUNK_SIZE,
        max_chunk_bytes=10 * 1024 * 1024,
        raise_on_error=False,
    ):
        if ok:
            success_count += 1
        else:
            fail_count += 1
            if fail_count <= 5:
                logger.warning("Failed to index doc: %s", item)
        if success_count % 10000 == 0 and success_count > 0:
            logger.info("Cases ES: %d docs indexed", success_count)

    logger.info("Cases ES import complete: %d succeeded, %d failed", success_count, fail_count)
    return success_count


def import_laws_to_es(es: Elasticsearch, index_name: str) -> int:
    """Stream HF laws dataset into an ES index. Returns doc count."""
    exclude = {"unofficial_sections_en", "unofficial_sections_fr"}

    logger.info("Loading HF dataset %s for ES (streaming)", HF_LAWS_REPO)
    ds = load_dataset(HF_LAWS_REPO, split="train", streaming=True, token=HF_TOKEN)

    def generate_actions():
        for row in ds:
            doc = _row_to_doc(row)
            if not doc:
                continue

            # Compute num_sections for ES scoring
            for lang in ("en", "fr"):
                sec_field = f"unofficial_sections_{lang}"
                num_field = f"num_sections_{lang}"
                if sec_field in doc and isinstance(doc[sec_field], str):
                    try:
                        sections = json.loads(doc[sec_field])
                        doc[num_field] = len(sections)
                    except (json.JSONDecodeError, TypeError):
                        pass

            es_doc = _doc_to_es(doc, exclude_fields=exclude)
            yield {"_index": index_name, "_source": es_doc}

    es_with_timeout = es.options(request_timeout=ES_REQUEST_TIMEOUT)
    success_count = 0
    fail_count = 0
    for ok, item in helpers.streaming_bulk(
        es_with_timeout,
        generate_actions(),
        chunk_size=ES_CHUNK_SIZE,
        max_chunk_bytes=10 * 1024 * 1024,
        raise_on_error=False,
    ):
        if ok:
            success_count += 1
        else:
            fail_count += 1
            if fail_count <= 5:
                logger.warning("Failed to index doc: %s", item)

    logger.info("Laws ES import complete: %d succeeded, %d failed", success_count, fail_count)
    return success_count


def finalize_es_index(es: Elasticsearch, index_name: str):
    """Restore production settings, refresh, and force-merge after bulk import."""
    logger.info("Finalizing ES index %s: restoring refresh_interval and replicas", index_name)
    es.indices.put_settings(
        index=index_name,
        body={
            "index": {
                "refresh_interval": "1s",
                "number_of_replicas": 1,
            }
        },
    )

    logger.info("Refreshing ES index %s", index_name)
    es.indices.refresh(index=index_name)

    logger.info("Force-merging ES index %s to 1 segment (this may take a while)", index_name)
    es.options(request_timeout=3600).indices.forcemerge(
        index=index_name, max_num_segments=1
    )
    logger.info("Finalized ES index %s", index_name)


# ────────── Atomic Swap ───────────────────────────────────────────────────── #


def swap_mongo_collection(db, live_name: str, new_name: str):
    """Atomically swap a new collection into the live name."""
    old_name = f"{live_name}-old"

    # Rename current to -old (if it exists)
    if live_name in db.list_collection_names():
        db[live_name].rename(old_name)
        logger.info("Renamed %s -> %s", live_name, old_name)

    # Rename new to live
    db[new_name].rename(live_name)
    logger.info("Renamed %s -> %s", new_name, live_name)

    # Drop old
    if old_name in db.list_collection_names():
        db.drop_collection(old_name)
        logger.info("Dropped %s", old_name)


def swap_es_index(es: Elasticsearch, alias_name: str, new_index: str):
    """Atomically swap an ES alias to point to a new index."""
    # Find which index(es) currently have this alias
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

    # Delete old indices
    for action in actions:
        if "remove" in action:
            old_idx = action["remove"]["index"]
            try:
                es.indices.delete(index=old_idx)
                logger.info("Deleted old ES index %s", old_idx)
            except Exception:
                logger.warning("Failed to delete old ES index %s", old_idx)


# ────────── Cleanup on Failure ────────────────────────────────────────────── #


def cleanup_temps(db, es: Elasticsearch, temp_names: Dict[str, list]):
    """Drop temporary collections and indices on failure."""
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


# ────────── Main ──────────────────────────────────────────────────────────── #


def main():
    start_time = time.time()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    logger.info("=" * 60)
    logger.info("Weekly update started at %s", datetime.now(timezone.utc).isoformat())

    # Step 1: Refresh GitHub caches
    logger.info("Step 1: Refreshing GitHub caches")
    refresh_github_caches()

    # Connect to databases
    mongo_client = MongoClient(MONGO_URL)
    db = mongo_client[MONGO_DB]
    es = Elasticsearch(ES_URL)

    # Temp names
    cases_mongo_temp = "canadian-case-law-new"
    laws_mongo_temp = "canadian-laws-new"
    cases_es_temp = f"canadian-case-law-{timestamp}"
    laws_es_temp = f"canadian-laws-{timestamp}"

    temp_names = {
        "mongo": [cases_mongo_temp, laws_mongo_temp],
        "es": [cases_es_temp, laws_es_temp],
    }

    try:
        # Clean up any leftover temp collections from a previous failed run
        cleanup_temps(db, es, temp_names)

        # ── Cases: import, index, swap, then free old ──
        logger.info("Step 2: Importing cases to MongoDB")
        cases_mongo_count = import_cases_to_mongo(db, cases_mongo_temp)

        logger.info("Step 2b: Creating MongoDB indexes for cases")
        create_mongo_indexes(db, cases_mongo_temp)

        logger.info("Step 3: Importing cases to Elasticsearch")
        create_es_index(es, cases_es_temp)
        cases_es_count = import_cases_to_es(es, cases_es_temp)
        finalize_es_index(es, cases_es_temp)

        logger.info("Step 4: Swapping cases (mongo + ES)")
        swap_mongo_collection(db, "canadian-case-law", cases_mongo_temp)
        swap_es_index(es, "canadian-case-law", cases_es_temp)

        # ── Laws: import, index, swap, then free old ──
        logger.info("Step 5: Importing laws to MongoDB")
        laws_mongo_count = import_laws_to_mongo(db, laws_mongo_temp)

        logger.info("Step 5b: Creating MongoDB indexes for laws")
        create_mongo_indexes(db, laws_mongo_temp)

        logger.info("Step 6: Importing laws to Elasticsearch")
        create_es_index(es, laws_es_temp)
        laws_es_count = import_laws_to_es(es, laws_es_temp)
        finalize_es_index(es, laws_es_temp)

        logger.info("Step 7: Swapping laws (mongo + ES)")
        swap_mongo_collection(db, "canadian-laws", laws_mongo_temp)
        swap_es_index(es, "canadian-laws", laws_es_temp)

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
