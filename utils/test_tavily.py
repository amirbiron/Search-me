#!/usr/bin/env python3
"""
Minimal sanity check for Tavily integration.
"""
import os, logging, requests, json
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

API_KEY = os.getenv("TAVILY_API_KEY")
assert API_KEY and API_KEY.strip(), "TAVILY_API_KEY missing/empty"

from tavily import TavilyClient
client = TavilyClient(api_key=API_KEY)
logger.info(f"Tavily key prefix: {API_KEY[:4]}***")

def _tavily_raw(query: str):
    url = "https://api.tavily.com/search"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    payload = {"query": query, "include_answer": True, "max_results": 5}
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
    logger.info(f"[RAW] Tavily status: {r.status_code}")
    logger.debug(f"[RAW] Tavily body: {r.text}")
    r.raise_for_status()
    return r.json()

def tavily_search(query: str, **kwargs):
    logger.info(f"Tavily query: {query} | kwargs={kwargs}")
    try:
        resp = client.search(
            query=query,
            include_answer=True,
            max_results=10,
            search_depth="advanced",
            **kwargs
        )
        logger.debug(f"[SDK] Tavily raw response: {resp}")
        if not resp or not resp.get("results"):
            logger.warning("SDK returned empty. Trying raw HTTP…")
            raw = _tavily_raw(query)
            if not raw or not raw.get("results"):
                raise RuntimeError("Tavily empty both SDK and raw")
            return raw
        return resp
    except Exception:
        logger.exception("Tavily search failed – falling back to raw")
        raw = _tavily_raw(query)
        return raw

def test_tavily_connection():
    """Test basic Tavily connection and functionality."""
    try:
        # Smoke test
        test = tavily_search("What is OpenAI?", include_images=False)
        assert test and test.get("results"), "Smoke test failed: Tavily returned no results"
        
        print("✅ Tavily connection and smoke test successful")
        print(f"✅ Found {len(test.get('results', []))} results")
        return True
            
    except Exception as e:
        print(f"❌ Tavily connection failed: {e}")
        return False

# Smoke test (runs once at startup)
if os.getenv("RUN_SMOKE_TEST", "true").lower() == "true":
    test = tavily_search("What is OpenAI?")
    assert test and test.get("results"), "Smoke test failed – empty results"
    logger.info("Smoke test passed ✅")

if __name__ == "__main__":
    test_tavily_connection()