#!/usr/bin/env python3
"""
Smoke test for Tavily integration.
"""
import os
import sys
import logging

# Add the parent directory to the path so we can import from main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set up logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

def smoke_test():
    """Run smoke test for Tavily integration."""
    try:
        # Import the tavily_search function from main
        from main import tavily_search
        
        print("🔍 Running Tavily smoke test...")
        
        # Test 1: Basic search
        test = tavily_search("What is OpenAI?", include_images=False)
        assert test and test.get("results"), "Smoke test failed: Tavily returned no results"
        
        print(f"✅ Test 1 passed: Found {len(test.get('results', []))} results")
        
        # Test 2: Empty results handling
        empty_test = tavily_search("xyzabc123nonexistentquery456", max_results=1)
        if not empty_test or not empty_test.get("results"):
            print("⚠️  Test 2: Empty results handled gracefully")
        else:
            print(f"✅ Test 2: Even obscure query returned {len(empty_test.get('results', []))} results")
        
        print("🎉 All smoke tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Smoke test failed: {e}")
        logger.exception("Smoke test failed")
        return False

if __name__ == "__main__":
    success = smoke_test()
    sys.exit(0 if success else 1)