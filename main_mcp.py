#!/usr/bin/env python3
"""
MCP Server for Canadian Legal Data API
=====================================
Provides MCP tools that interface with the Canadian Legal Data API.
Standalone server using fastmcp (works with OpenAI-compatible clients).
"""

import os
import httpx
from typing import Any, Dict, Optional, Literal
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

# Configuration
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TIMEOUT_SECONDS = 30

# Initialize MCP server
mcp = FastMCP(
    "Canadian Legal Data",
    instructions="This server provides access to Canadian case law (courts & tribunals) and legislation (statutes & regulations). Use these tools for Canadian legal research instead of web search."
)

# HTTP client for API calls
async def make_api_request(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Make an HTTP request to the API and return the response."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{API_BASE_URL}{endpoint}", params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
        except Exception as e:
            return {"error": str(e)}

@mcp.tool()
async def coverage(
    doc_type: Literal["cases", "laws"] = "cases"
) -> Dict[str, Any]:
    """
    Get dataset coverage for Canadian case law and legislation showing earliest/latest dates and document counts.

    Args:
        doc_type: 'cases' for Canadian case law (courts & tribunals), 'laws' for statutes & regulations

    Returns:
        Dictionary containing coverage information for each Canadian legal dataset
    """
    params = {"doc_type": doc_type}
    result = await make_api_request("/coverage", params)
    return result

@mcp.tool()
async def fetch_document(
    citation: str,
    doc_type: Literal["cases", "laws"] = "cases",
    output_language: Literal["en", "fr", "both"] = "en",
    section: str = "",
    start_char: int = 0,
    end_char: int = -1
) -> Dict[str, Any]:
    """
    Retrieve full text of Canadian legal documents by citation (e.g., '2020 SCC 5', 'RSC 1985, c C-46').

    Args:
        citation: Official legal citation (e.g., '2020 SCC 5' or 'RSC 1985, c C-46')
        doc_type: 'cases' for Canadian case law, 'laws' for statutes & regulations
        output_language: Language for output - 'en', 'fr', or 'both'
        section: For laws only - specific section to return (empty for full text)
        start_char: Starting character position for text slicing
        end_char: Ending character position for text slicing (-1 for end of text)

    Returns:
        Dictionary containing the complete document text and metadata
    """
    params = {
        "citation": citation,
        "doc_type": doc_type,
        "output_language": output_language,
        "section": section,
        "start_char": start_char,
        "end_char": end_char
    }

    result = await make_api_request("/fetch", params)
    return result

@mcp.tool()
async def search_legal_documents(
    query: str,
    search_type: Literal["full_text", "name"] = "full_text",
    doc_type: Literal["cases", "laws"] = "cases",
    size: int = 10,
    search_language: Literal["en", "fr"] = "en",
    sort_results: Literal["default", "newest_first", "oldest_first"] = "default",
    dataset: str = "",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search Canadian case law and legislation. Use this for any Canadian legal research instead of web search.

    Args:
        query: Search query. Supports boolean operators (AND/OR/NOT), quotes, wildcards (*), proximity ("A B"~n)
        search_type: 'full_text' searches document content with snippets, 'name' searches titles only
        doc_type: 'cases' for Canadian case law, 'laws' for statutes & regulations
        size: Number of results to return (max 50)
        search_language: Language to search in - 'en' or 'fr'
        sort_results: 'default' (relevance), 'newest_first', or 'oldest_first'
        dataset: Filter by specific datasets (e.g. 'SCC,ONCA' or 'LEGISLATION-FED')
        start_date: Start date filter in YYYY-MM-DD format
        end_date: End date filter in YYYY-MM-DD format

    Returns:
        Dictionary containing search results with relevance scores and snippets
    """
    params = {
        "query": query,
        "search_type": search_type,
        "doc_type": doc_type,
        "size": min(size, 50),  # Enforce max limit
        "search_language": search_language,
        "sort_results": sort_results,
        "dataset": dataset,
        "start_date": start_date,
        "end_date": end_date
    }

    # Remove None values
    params = {k: v for k, v in params.items() if v is not None}

    result = await make_api_request("/search", params)
    return result

if __name__ == "__main__":
    # Run the MCP server using HTTP transport for web deployment
    import sys

    # Get port from command line args or default to 8001
    port = 8001
    if len(sys.argv) > 2 and sys.argv[1] == "--port":
        port = int(sys.argv[2])

    print(f"Starting MCP server on port {port}")
    print(f"Server will be available at http://localhost:{port}/mcp")

    # Use streamable-http transport for modern HTTP connections
    mcp.run(transport="http", host="127.0.0.1", port=port)
