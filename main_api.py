"""
Canadian Legal Data API
=======================
FastAPI application to search and retrieve Canadian legal documents
— both case law and legislation — by citation, name/title, or full-text.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi_mcp import FastApiMCP
from pymongo import MongoClient
from pymongo.collation import Collation
from elasticsearch import BadRequestError, Elasticsearch
from gridfs import GridFS
import os
import re
import json
import logging
import time
import requests as http_requests
from collections import defaultdict
from typing import Dict, Any, Literal, Optional, List
from datetime import date, datetime
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from dotenv import load_dotenv

load_dotenv()

# ------------------------ RESPONSE MODELS ----------------------------------
class CoverageItem(BaseModel):
    dataset: str
    description_en: Optional[str]
    description_fr: Optional[str]
    earliest_document_date: Optional[date]
    latest_document_date: Optional[date]
    number_of_documents: int

class CoverageResponse(BaseModel):
    results: List[CoverageItem]

class FetchResponse(BaseModel):
    results: List[Dict[str, Any]]

class SearchItem(BaseModel):
    score: float
    snippet: Optional[str] = None

    class Config:
        extra = "allow"

class SearchResponse(BaseModel):
    results: List[Dict[str, Any]]

# ---------------------------- LOGGER ---------------------------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    filename=LOG_DIR / "api.log",
    filemode="a",
    format="%(asctime)s %(levelname)s %(message)s",
)

# Quiet down noisy third-party loggers
logging.getLogger("elastic_transport.transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # in case the MCP side logs to the same file
logging.getLogger("httpcore").setLevel(logging.WARNING)
# ----------------------------- CACHE ----------------------------------------
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/a2aj-ca/canadian-legal-data/main/UTILS"
GITHUB_FILES = {
    "case_coverage.json": f"{GITHUB_RAW_BASE}/case_coverage.json",
    "laws_coverage.json": f"{GITHUB_RAW_BASE}/laws_coverage.json",
    "search_boosting.json": f"{GITHUB_RAW_BASE}/search_boosting.json",
}

# In-memory caches (populated on startup)
_coverage_cache: Dict[str, List[Dict[str, Any]]] = {}
_boosting_cache: Dict[str, Dict[str, int]] = {}


def _load_or_fetch_json(filename: str) -> dict:
    """Load JSON from cache file, or fetch from GitHub if missing."""
    cache_path = CACHE_DIR / filename
    if cache_path.exists():
        with open(cache_path, "r") as f:
            return json.load(f)
    # Fetch from GitHub
    url = GITHUB_FILES[filename]
    logging.info("Cache miss for %s, fetching from %s", filename, url)
    resp = http_requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return data


def _load_caches():
    """Load all cached JSON files into memory."""
    global _coverage_cache, _boosting_cache

    case_cov = _load_or_fetch_json("case_coverage.json")
    laws_cov = _load_or_fetch_json("laws_coverage.json")
    _coverage_cache["cases"] = case_cov.get("results", [])
    _coverage_cache["laws"] = laws_cov.get("results", [])

    boosting = _load_or_fetch_json("search_boosting.json")
    _boosting_cache["cases"] = boosting.get("cases_search_boosting", {})
    _boosting_cache["laws"] = boosting.get("laws_search_boosting", {})

    logging.info(
        "Caches loaded: %d case coverage entries, %d laws coverage entries, "
        "%d case boost rules, %d laws boost rules",
        len(_coverage_cache["cases"]), len(_coverage_cache["laws"]),
        len(_boosting_cache["cases"]), len(_boosting_cache["laws"]),
    )


# ----------------------------- LIFESPAN -------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_caches()
    yield


# ----------------------------- FastAPI -------------------------------------
app = FastAPI(
    title="Canadian Legal Data API",
    description="""
Search and retrieve Canadian **case law** *and* **legislation/regulations**
by citation, title/name, or full text.

**Advanced search (English ✦ French) supported:**

- Boolean operators ( `AND/OR/NOT` or `ET/OU/NON` )
- Phrases in quotes
- Grouping with parentheses
- Wildcards ( `*` suffix )
- Proximity ( `NEAR/n` or `"words"~n` or `/n` )
- French `EXACT( )` override

Dataset boosting is applied automatically (e.g. SCC decisions, statutes),
unless you explicitly sort by **newest** or **oldest**.

**Rate limits:** 1,000 requests/hour and 5,000 requests/day per IP.
For bulk access, see https://a2aj.ca/data/
""",
    version="0.3.0",
    contact={"name": "Access to Algorithmic Justice", "email": "a2aj@yorku.ca"},
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------- RATE LIMITER --------------------------------------

RATE_LIMIT_HOURLY = 1000
RATE_LIMIT_DAILY = 5000

RATE_LIMIT_MESSAGE = (
    "Rate limit exceeded. If you need bulk access to Canadian legal data, "
    "please use our bulk download (https://a2aj.ca/data/). "
    "If you are not scraping and require higher rate limits, "
    "contact us at a2aj@yorku.ca."
)

# Exempt paths that should never be rate-limited (MCP, OpenAPI, docs, etc.)
RATE_LIMIT_EXEMPT_PREFIXES = ("/mcp", "/openapi.json", "/docs", "/redoc", "/health", "/v1/health")


class RateLimiter:
    """Simple in-memory sliding-window rate limiter keyed by IP address."""

    def __init__(self):
        # ip -> list of request timestamps
        self._hits: Dict[str, list] = defaultdict(list)
        self._call_count: int = 0

    def is_allowed(self, ip: str) -> bool:
        now = time.time()
        one_hour_ago = now - 3600
        one_day_ago = now - 86400

        # Periodic sweep: every 1000 calls, remove stale IPs
        self._call_count += 1
        if self._call_count >= 1000:
            self._call_count = 0
            stale_ips = []
            for k, ts in self._hits.items():
                self._hits[k] = [t for t in ts if t > one_day_ago]
                if not self._hits[k]:
                    stale_ips.append(k)
            for k in stale_ips:
                del self._hits[k]

        timestamps = self._hits[ip]

        # Prune entries older than 24 h
        self._hits[ip] = timestamps = [t for t in timestamps if t > one_day_ago]

        hourly = sum(1 for t in timestamps if t > one_hour_ago)
        daily = len(timestamps)

        if hourly >= RATE_LIMIT_HOURLY or daily >= RATE_LIMIT_DAILY:
            return False

        timestamps.append(now)
        return True


rate_limiter = RateLimiter()


# ------------------- UNKNOWN QUERY PARAMETER GUARD -------------------------
# Pure ASGI middleware — does NOT buffer the response, so SSE/MCP works fine.

# Expected query params per endpoint (so broken scrapers get a clear error)
ALLOWED_PARAMS: Dict[str, set] = {
    "/search": {"query", "search_type", "doc_type", "size", "search_language",
                "sort_results", "dataset", "start_date", "end_date"},
    "/fetch": {"citation", "doc_type", "output_language", "section",
               "start_char", "end_char"},
    "/coverage": {"doc_type"},
}


class UnknownParamsMiddleware:
    """Reject requests with query parameters not defined in the endpoint."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"].rstrip("/")
        if path in ALLOWED_PARAMS:
            from urllib.parse import parse_qs
            qs = scope.get("query_string", b"").decode("utf-8")
            provided = set(parse_qs(qs).keys()) if qs else set()
            unknown = provided - ALLOWED_PARAMS[path]
            if unknown:
                allowed = ALLOWED_PARAMS[path]
                response = JSONResponse(
                    status_code=400,
                    content={
                        "error": f"Unknown query parameter(s): {', '.join(sorted(unknown))}. "
                        f"Allowed parameters for {path}: {', '.join(sorted(allowed))}."
                    },
                )
                return await response(scope, receive, send)

        return await self.app(scope, receive, send)


class RateLimitMiddleware:
    """Per-IP rate limiting. Pure ASGI — safe for SSE/streaming endpoints."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        # Skip rate limiting for exempt paths
        if any(path.startswith(p) for p in RATE_LIMIT_EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)

        # Extract IP from headers or client
        headers = dict(scope.get("headers", []))
        forwarded = headers.get(b"x-forwarded-for", b"").decode("utf-8")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        else:
            client = scope.get("client")
            ip = client[0] if client else "unknown"

        if not rate_limiter.is_allowed(ip):
            response = JSONResponse(
                status_code=429,
                content={"error": RATE_LIMIT_MESSAGE},
                headers={"Retry-After": "3600"},
            )
            return await response(scope, receive, send)

        return await self.app(scope, receive, send)


# Register middleware (outermost runs first with add_middleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(UnknownParamsMiddleware)


# ------------------------- Database / GridFS --------------------------------
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017/")
MONGO_DB  = "a2aj-api"

client = MongoClient(MONGO_URL)
db = client[MONGO_DB]

# Collections
collection_cases = db["canadian-case-law"]
collection_laws  = db["canadian-laws"]

# GridFS bucket (for large legislation docs, e.g. Income Tax Act)
fs = GridFS(db)

# -------------------------- Elasticsearch ----------------------------------
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
es = Elasticsearch(ES_URL)

# ------------------------ OpenAPI server field -----------------------------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes, contact=app.contact,
    )
    schema["servers"] = [{"url": "https://api.a2aj.ca"}]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi  # type: ignore[assignment]

# ------------------------- Helper functions ---------------------------------
def _normalize_citation(citation: str) -> str:
    """Normalize a citation: strip whitespace, replace NBSP, remove periods."""
    return re.sub(r"\.", "", citation.replace("\u00A0", " ").strip())

def build_filters(dataset: str, start_date: Optional[date], end_date: Optional[date], search_language: str = "en"):
    """Build ES filter clauses shared by cases & laws."""
    filters: list[Dict[str, Any]] = []
    if dataset:
        filters.append({"terms": {"dataset.keyword": [d.strip().upper() for d in dataset.split(",")]}})
    if start_date or end_date:
        date_field = "document_date_en" if search_language == "en" else "document_date_fr"
        rng: Dict[str, str] = {}
        if start_date:
            rng["gte"] = start_date.isoformat()
        if end_date:
            rng["lte"] = end_date.isoformat()
        filters.append({"range": {date_field: rng}})
    return filters

def build_function_score_cases():
    boosts = _boosting_cache.get("cases", {})
    fns = [{"filter": {"term": {"dataset.keyword": k}}, "weight": v} for k, v in boosts.items()]
    fns.append({"gauss": {"document_date_en": {"origin": "now", "scale": "7000d", "decay": 0.5}}, "weight": 1})
    return fns

def build_function_score_laws():
    boosts = _boosting_cache.get("laws", {})
    fns = [{"filter": {"term": {"dataset.keyword": k}}, "weight": v} for k, v in boosts.items()]
    fns.append({"field_value_factor": {"field": "num_sections_en", "modifier": "log1p", "missing": 1}, "weight": 1})
    fns.append({"gauss": {"document_date_en": {"origin": "now", "scale": "7000d", "decay": 0.5}}, "weight": 1})
    return fns

def slice_text(text: str | None, start: int, end: int):
    if text is None: return None
    start = max(0, start)
    if end == -1 or end > len(text): end = len(text)
    return "" if start > end else text[start:end]

# ---- French operator translation helpers ----------------------------------
FRENCH_OPS_PATTERN = re.compile(
    r"""
    (?P<exact>EXACT\((?P<exact_body>[^)]+)\)) |   # EXACT(…)
    \b(?P<ou>OU)\b | \b(?P<et>ET)\b | \b(?P<non>NON)\b | (?P<dash>\s-\s)
    """,
    re.IGNORECASE | re.VERBOSE,
)

def translate_french_query(q: str) -> str:
    def _repl(m: re.Match) -> str:
        if m.group("exact"): return f"\"{m.group('exact_body').strip()}\""
        if m.group("ou"):    return "OR"
        if m.group("et"):    return "AND"
        if m.group("non") or m.group("dash"): return "NOT "
        return m.group(0)
    return FRENCH_OPS_PATTERN.sub(_repl, q)

# ---- Proximity helper ------------------------------------------------------
def canlii_style_query(q: str) -> str:
    pattern = re.compile(r'("?[^"\s]+"?)\s+NEAR/(\d+)\s+("?[^"\s]+"?)', re.IGNORECASE)
    return pattern.sub(lambda m: f"\"{m.group(1).strip('\"')} {m.group(3).strip('\"')}\"~{m.group(2)}", q)

# ---- GridFS hydration helper (legislation only) ---------------------------
def hydrate_large_field(doc: dict, base: str):
    fid_key = f"{base}_file_id"
    if fid_key in doc and not doc.get(base):
        try:
            data = fs.get(doc[fid_key]).read().decode("utf-8")
            if base.startswith("unofficial_sections_"):
                doc[base] = json.loads(data)
            else:
                doc[base] = data
        except Exception:
            logging.exception("Failed GridFS fetch for %s", doc.get("_id"))
    doc.pop(fid_key, None)

# --------------------------- END HELPER FUNCTIONS ---------------------------

# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HEALTH ENDPOINT
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
@app.get("/v1/health", include_in_schema=False)
def health():
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# COVERAGE ENDPOINT
# ---------------------------------------------------------------------------
@app.get(
    "/coverage",
    summary="Dataset coverage (earliest, latest, count)",
    description=(
        "Returns, for each dataset (court/tribunal or statute/regulation), the English & French "
        "description, earliest document date, latest document date, and total document count."
    ),
    operation_id="coverage",
    response_model=CoverageResponse,
)
def coverage(
    doc_type: Literal["cases", "laws"] = Query(
        "cases",
        description="'cases' (default) for case law or 'laws' for statutes & regulations",
    ),
):
    """Return temporal coverage and document counts for every dataset."""
    coverage_data = _coverage_cache.get(doc_type, [])

    results: list[dict[str, Any]] = []
    for doc in coverage_data:
        results.append({
            "dataset": doc.get("dataset"),
            "description_en": doc.get("description_en"),
            "description_fr": doc.get("description_fr"),
            "earliest_document_date": doc.get("earliest_document_date"),
            "latest_document_date": doc.get("latest_document_date"),
            "number_of_documents": doc.get("number_of_documents", 0),
        })

    results.sort(key=lambda x: x["dataset"])
    return {"results": results}

# ---------------------------------------------------------------------------
# UNIFIED FETCH ENDPOINT
# ---------------------------------------------------------------------------
@app.get(
    "/fetch",
    summary="Get full text of case or law by citation",
    description="""Fetch a case or a law/regulation by citation. For cases, returns full text or a character slice. For laws, returns full text, a slice, or a single section (if provided). The section parameter is ignored for cases.""",
    operation_id="fetch",
    response_model=FetchResponse,
)
def fetch(
    citation: str = Query(
        ...,
        description="Citation in English or French (e.g., '2020 SCC 5' or 'RSC 1985, c C-46')",
        example="2020 SCC 5",
    ),
    doc_type: Literal["cases", "laws"] = Query(
        "cases",
        description="'cases' (default) for case law or 'laws' for statutes & regulations",
    ),
    output_language: Literal["en", "fr", "both"] = Query(
        "en",
        description="Output language: 'en', 'fr', or 'both' (default is 'en')",
    ),
    section: str = Query(
        "",
        description="Return a specific section (laws only). Leave empty to return full text.",
        example="",
    ),
    start_char: int = Query(
        0,
        description="Start character index for text chunking (ignored if section is specified)",
        example=0,
    ),
    end_char: int = Query(
        -1,
        description="End character index for text chunking (-1 means end of text, ignored if section is specified)",
        example=1000,
    ),
):
    if doc_type == "cases":
        sanitized = _normalize_citation(citation).upper()
        doc = collection_cases.find_one({
            "$or": [
                {"citation_en": sanitized},
                {"citation_fr": sanitized},
            ]
        })

        if not doc:
            return JSONResponse(content={}, status_code=200)

        doc.pop("_id", None)

        if output_language == "en":
            filtered: Dict[str, Any] = {k: v for k, v in doc.items() if not k.endswith("_fr")}
            if "unofficial_text_en" in filtered:
                filtered["unofficial_text_en"] = slice_text(
                    filtered["unofficial_text_en"], start_char, end_char
                )
        elif output_language == "fr":
            filtered = {k: v for k, v in doc.items() if not k.endswith("_en")}
            if "unofficial_text_fr" in filtered:
                filtered["unofficial_text_fr"] = slice_text(
                    filtered["unofficial_text_fr"], start_char, end_char
                )
        else:
            filtered = dict(doc)
            if "unofficial_text_en" in filtered:
                filtered["unofficial_text_en"] = slice_text(
                    filtered["unofficial_text_en"], start_char, end_char
                )
            if "unofficial_text_fr" in filtered:
                filtered["unofficial_text_fr"] = slice_text(
                    filtered["unofficial_text_fr"], start_char, end_char
                )

    else:  # laws
        citation_in = _normalize_citation(citation)
        strength2 = Collation(locale="en", strength=2, alternate="shifted")

        doc = collection_laws.find_one(
            {
                "$or": [
                    {"citation_en": citation_in},
                    {"citation_fr": citation_in},
                ]
            },
            collation=strength2,
        )

        if not doc:
            return JSONResponse(content={}, status_code=200)

        if output_language in {"en", "both"}:
            hydrate_large_field(doc, "unofficial_text_en")
            hydrate_large_field(doc, "unofficial_sections_en")
        if output_language in {"fr", "both"}:
            hydrate_large_field(doc, "unofficial_text_fr")
            hydrate_large_field(doc, "unofficial_sections_fr")

        doc.pop("_id", None)

        def _extract(lang: str):
            if section:
                return doc.get(f"unofficial_sections_{lang}", {}).get(section)
            return slice_text(doc.get(f"unofficial_text_{lang}"), start_char, end_char)

        if output_language == "en":
            filtered = {k: v for k, v in doc.items() if not k.endswith("_fr")}
            filtered["unofficial_text_en"] = _extract("en")
        elif output_language == "fr":
            filtered = {k: v for k, v in doc.items() if not k.endswith("_en")}
            filtered["unofficial_text_fr"] = _extract("fr")
        else:
            filtered = dict(doc)
            filtered["unofficial_text_en"] = _extract("en")
            filtered["unofficial_text_fr"] = _extract("fr")

        for k in list(filtered):
            if k.startswith("unofficial_sections_") or k.startswith("num_sections_") or k.endswith("_file_id"):
                filtered.pop(k, None)

    return {"results": [filtered]}

# ---------------------------------------------------------------------------
# UNIFIED SEARCH
# ---------------------------------------------------------------------------
@app.get(
    "/search",
    summary="Search cases or laws by full text or document name (English or French)",
    description="""Search cases or laws/regulations by full text or by title. Use search_type=full_text for content (with highlighted snippets) or search_type=name for titles only. Supports AND/OR/NOT, quotes, parentheses, wildcards (*), and proximity ("A B"~n or A NEAR/n B).""",
    operation_id="search",
    response_model=SearchResponse,
)
def search(
    query: str = Query(
        ..., description="Search query. See docs for advanced syntax.", example="Falun Gong"
    ),
    search_type: Literal["full_text", "name"] = Query(
        "full_text",
        description="'full_text' searches document content, 'name' searches document titles only",
    ),
    doc_type: Literal["cases", "laws"] = Query(
        "cases",
        description="'cases' (default) for case law or 'laws' for statutes & regulations",
    ),
    size: int = Query(10, description="Number of results to return (max 50)", le=50, example=10),
    search_language: Literal["en", "fr"] = Query(
        "en",
        description="Search language: 'en' or 'fr' (default 'en')",
    ),
    sort_results: Literal["default", "newest_first", "oldest_first"] = Query(
        "default",
        description="Sort order: 'default' (relevance/boosting), 'newest_first', or 'oldest_first'",
    ),
    dataset: str = Query(
        default="",
        description="Comma-separated list of datasets (e.g., SCC, ONCA for cases or LEGISLATION-FED,REGULATIONS-FED for laws). Leave empty for no filter. See coverage for dataset codes.",
        example="FC,RAD,RPD",
    ),
    start_date: Optional[str] = Query(
        default=None,
        description="Start date filter (YYYY-MM-DD). Leave as None or empty string for no date filter.",
        example="2023-01-01",
    ),
    end_date: Optional[str] = Query(
        default=None,
        description="End date filter (YYYY-MM-DD). Leave as None or empty string for no date filter.",
        example="2024-12-31",
    ),
):
    if query.strip() == "":
        query = "*"

    parsed_start_date: Optional[date] = None
    parsed_end_date: Optional[date] = None

    if start_date and start_date.strip():
        try:
            parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(
                {"error": f"Invalid start_date format. Expected YYYY-MM-DD, got: {start_date}"},
                status_code=400,
            )

    if end_date and end_date.strip():
        try:
            parsed_end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(
                {"error": f"Invalid end_date format. Expected YYYY-MM-DD, got: {end_date}"},
                status_code=400,
            )

    if search_language == "fr":
        query = translate_french_query(query)

    if search_type == "full_text":
        search_field = "unofficial_text_en" if search_language == "en" else "unofficial_text_fr"
    else:
        search_field = "name_en" if search_language == "en" else "name_fr"

    date_field = "document_date_en" if search_language == "en" else "document_date_fr"

    query_string = canlii_style_query(query)
    filters = build_filters(dataset, parsed_start_date, parsed_end_date, search_language)

    bool_query: Dict[str, Any] = {
        "should": [
            {
                "query_string": {
                    "query": query_string,
                    "fields": [search_field],
                    "default_operator": "AND",
                }
            }
        ],
        "minimum_should_match": 1,
    }
    if filters:
        bool_query["filter"] = filters

    index_name = "canadian-case-law" if doc_type == "cases" else "canadian-laws"
    score_function = build_function_score_cases() if doc_type == "cases" else build_function_score_laws()

    if sort_results == "default":
        body: Dict[str, Any] = {
            "query": {
                "function_score": {
                    "query": {"bool": bool_query},
                    "functions": score_function,
                    "score_mode": "multiply",
                    "boost_mode": "multiply",
                }
            },
            "size": size,
        }
        if search_type == "full_text":
            body["highlight"] = {
                "fields": {
                    search_field: {
                        "fragment_size": 200,
                        "number_of_fragments": 1,
                    }
                }
            }
    else:
        body = {
            "query": {"bool": bool_query},
            "size": size,
            "sort": [{date_field: {"order": "desc" if sort_results == "newest_first" else "asc"}}],
        }
        if search_type == "full_text":
            body["highlight"] = {
                "fields": {
                    search_field: {
                        "fragment_size": 200,
                        "number_of_fragments": 1,
                    }
                }
            }

    try:
        res = es.search(index=index_name, body=body)
    except BadRequestError as exc:
        return JSONResponse(content={"error": f"Could not parse search query: {exc}"}, status_code=400)
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)

    results = []
    for hit in res["hits"]["hits"]:
        source = hit["_source"].copy()
        if doc_type == "cases":
            for field in ("_id", "unofficial_text_en", "unofficial_text_fr"):
                source.pop(field, None)
        else:
            for field in ("_id", "unofficial_text_en", "unofficial_text_fr",
                          "unofficial_sections_en", "unofficial_sections_fr"):
                source.pop(field, None)

        result_item = {
            **source,
            "score": hit["_score"],
        }

        if search_type == "full_text":
            highlight = hit.get("highlight", {}).get(search_field, [])
            snippet = highlight[0] if highlight else ""
            result_item["snippet"] = snippet

        results.append(result_item)

    return {"results": results}

# ---------------------------------------------------------------------------
# ----------------------------- MCP -----------------------------------------
mcp = FastApiMCP(app)
mcp.mount()
# END OF FILE ----------------------------------------------------------------
