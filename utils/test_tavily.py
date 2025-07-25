#!/usr/bin/env python3
"""
Minimal sanity check for Tavily integration.
"""
import os
from tavily import TavilyClient

def test_tavily_connection():
    """Test basic Tavily connection and functionality."""
    api_key = os.getenv("TAVILY_API_KEY")
    
    if not api_key:
        print("❌ TAVILY_API_KEY environment variable is not set")
        return False
    
    try:
        client = TavilyClient(api_key=api_key)
        # Simple test query
        response = client.search(query="test", max_results=1)
        
        if response and 'results' in response:
            print("✅ Tavily connection successful")
            return True
        else:
            print("❌ Tavily returned unexpected response format")
            return False
            
    except Exception as e:
        print(f"❌ Tavily connection failed: {e}")
        return False

if __name__ == "__main__":
    test_tavily_connection()