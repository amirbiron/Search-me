#!/usr/bin/env python3
"""
Minimal sanity check for Tavily integration.
"""
import os
import logging
from tavily import TavilyClient

# Set up logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

def tavily_search(query: str, **kwargs):
    """
    Enhanced Tavily search with logging and error handling.
    """
    API_KEY = os.getenv("TAVILY_API_KEY")
    assert API_KEY and API_KEY.strip(), "TAVILY_API_KEY is missing or empty"
    logger.info(f"Tavily key prefix: {API_KEY[:4]}***")  # sanity only
    
    client = TavilyClient(api_key=API_KEY)
    
    logger.info(f"Tavily query: {query} | kwargs={kwargs}")
    try:
        resp = client.search(
            query=query,
            include_answer=True,
            max_results=10,
            search_depth="advanced",
            **kwargs
        )
        logger.debug(f"Tavily raw response: {resp}")
        if not resp or not resp.get("results"):
            logger.warning("Tavily returned empty results")
        return resp
    except Exception as e:
        logger.exception("Tavily search failed")
        raise

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

if __name__ == "__main__":
    test_tavily_connection()