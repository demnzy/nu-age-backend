import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import asyncio
from unittest.mock import patch, MagicMock
from services.image_search_service import search_brave_images, resolve_contextual_image
from database import Settings

async def test_brave_parsing():
    print("=== TEST 1: Brave Image Search Parsing ===")
    mock_brave_response = {
        "type": "images",
        "query": {"original": "raspberry pi 4 pinout"},
        "results": [
            {
                "title": "Raspberry Pi 4 Pinout Diagram",
                "url": "https://example.com/images/rpi4_pinout.png",
                "properties": {
                    "url": "https://example.com/images/rpi4_pinout.png",
                    "thumbnail": {
                        "src": "https://example.com/thumbs/rpi4_pinout_thumb.jpg"
                    }
                }
            }
        ]
    }

    class MockResponse:
        status_code = 200
        def json(self):
            return mock_brave_response

    class MockAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, headers=None, params=None):
            return MockResponse()

    with patch("httpx.AsyncClient", return_value=MockAsyncClient()), \
         patch("services.image_search_service.is_valid_image_url", return_value=True):
        url = await search_brave_images("raspberry pi 4 pinout", "test_brave_key_12345")
        print("Brave Mock Resolved URL:", url)
        assert url == "https://example.com/images/rpi4_pinout.png", f"Expected rpi4_pinout.png, got {url}"
    print("PASS: Brave Search API payload parsing verified!")


async def test_cascading_without_brave_key():
    print("\n=== TEST 2: Cascading to Wikimedia when Brave Key is Empty ===")
    url = await resolve_contextual_image("neuron anatomy labeled diagram")
    print("Resolved Academic URL:", url)
    assert url and "wikimedia.org" in url, "Expected Wikimedia Commons image for neuron anatomy!"
    print("PASS: Cascading to Wikimedia verified!")


async def main():
    # Verify settings getters
    settings = Settings()
    brave_key = settings.get_brave_search_api_key()
    print(f"Brave Search API Key currently configured: {'<Set>' if brave_key else '<None (using Wikimedia & Mermaid)>'}")
    assert not hasattr(settings, "get_google_search_api_key"), "Error: get_google_search_api_key still exists on Settings!"
    assert not hasattr(settings, "get_google_search_cx"), "Error: get_google_search_cx still exists on Settings!"
    print("PASS: Google search settings expectancies completely removed.")

    await test_brave_parsing()
    await test_cascading_without_brave_key()
    print("\nALL BRAVE SEARCH INTEGRATION TESTS PASSED CLEANLY!")

if __name__ == "__main__":
    asyncio.run(main())
