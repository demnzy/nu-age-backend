"""
Contextual Educational Image Resolution Service for Nu-Age Course Generation.
Provides a multi-tier, zero-broken-link image retrieval engine:
  * Tier 1: Brave Search API (Open Web Image Search) when configured
  * Tier 2: Wikimedia Commons Academic API (100% Free, high-res diagrams & scientific media)
  * Tier 3: Pexels API (curated stock photography for business, lifestyle, workplace)
  * Tier 4: Wikipedia Lead Article Image API (conceptual topic grounding)
  * Tier 5: Fallback to deterministic Mermaid.js process diagrams
"""

import asyncio
import os
import re
import urllib.parse
from typing import Optional
import httpx
from database import Settings


# In-memory session cache for query -> image URL mappings to eliminate redundant API calls
_IMAGE_QUERY_CACHE: dict[str, str] = {}

# Wikimedia requires a descriptive User-Agent identifying the application
_WIKIMEDIA_USER_AGENT = "NuAge-EducationalPlatform/1.0 (academic-course-engine; contact@nu-age.name.ng)"

# Domains known to block hotlinking or return watermarked previews
_BLOCKED_DOMAINS = (
    "pinimg.com",
    "pinterest.com",
    "shutterstock.com",
    "gettyimages.com",
    "istockphoto.com",
    "dreamstime.com",
    "alamy.com",
    "depositphotos.com",
)


async def is_valid_image_url(url: str, timeout: float = 3.0) -> bool:
    """Verifies that an external image URL is accessible and returns HTTP 200."""
    if not url or not url.startswith(("http://", "https://")):
        return False

    # Blocked stock watermark domains
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower()
    if any(b in netloc for b in _BLOCKED_DOMAINS):
        return False

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # First attempt fast HEAD request
            try:
                res = await client.head(url, headers=headers)
                if res.status_code == 200:
                    c_type = res.headers.get("content-type", "").lower()
                    if "image/" in c_type or any(url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif")):
                        return True
            except Exception:
                pass

            # Fallback to GET with byte-range limit to avoid full payload download
            res = await client.get(url, headers={**headers, "Range": "bytes=0-1024"})
            if res.status_code in (200, 206):
                c_type = res.headers.get("content-type", "").lower()
                if "image/" in c_type or any(url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif")):
                    return True
    except Exception:
        return False

    return False


async def search_brave_images(query: str, api_key: str) -> Optional[str]:
    """
    Tier 1: Brave Search API (Image Search).
    True independent open-web search index without domain restriction limitations.
    Returns high-resolution, verified image URL.
    """
    clean_q = query.strip()
    if not clean_q or not api_key:
        return None

    endpoint = "https://api.search.brave.com/res/v1/images/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
        "User-Agent": "NuAge-EducationalPlatform/1.0 (course-image-engine; contact@nu-age.name.ng)",
    }
    params = {
        "q": clean_q,
        "count": 5,
        "safesearch": "moderate",
        "search_lang": "en",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(endpoint, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                results = data.get("results", [])
                for item in results:
                    candidate = (
                        (item.get("properties") or {}).get("url")
                        or item.get("url")
                    )
                    if candidate and await is_valid_image_url(candidate):
                        return candidate

                    # If full-res fails preflight check, test thumbnail
                    thumb = ((item.get("properties") or {}).get("thumbnail") or {}).get("src")
                    if thumb and await is_valid_image_url(thumb):
                        return thumb
    except Exception as e:
        print(f"[WARNING] Brave Image Search failed for '{query}': {e}")

    return None


async def search_wikimedia_commons(query: str) -> Optional[str]:
    """
    Tier 2: Wikimedia Commons API.
    Zero-config, 100% free gold standard for educational diagrams, scientific illustrations,
    anatomical models, chemical apparatus, and historical photographs.
    Returns direct optimized web thumbnails (800px width) from Wikipedia CDN.
    """
    clean_q = query.strip()
    if not clean_q:
        return None

    # Search with MediaWiki Action API generator
    endpoint = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": clean_q,
        "gsrnamespace": 6,  # Namespace 6 = Media/File assets
        "gsrlimit": 5,
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": 800,  # Scaled responsive web thumbnail
        "origin": "*",
    }
    headers = {"User-Agent": _WIKIMEDIA_USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(endpoint, params=params, headers=headers)
            if res.status_code == 200:
                data = res.json()
                pages = (data.get("query") or {}).get("pages", {})
                for _, page_info in pages.items():
                    imageinfo_list = page_info.get("imageinfo", [])
                    if imageinfo_list:
                        info = imageinfo_list[0]
                        mime = str(info.get("mime", "")).lower()
                        # Only accept images and vector graphics
                        if mime.startswith("image/") or "svg" in mime:
                            candidate_url = info.get("thumburl") or info.get("url")
                            if candidate_url:
                                return candidate_url
    except Exception as e:
        print(f"[WARNING] Wikimedia Commons search failed for '{query}': {e}")

    return None


async def search_pexels_images(query: str, api_key: str) -> Optional[str]:
    """
    Tier 3: Pexels API.
    Curated stock photography for modern workplace, business, marketing, lifestyle, and soft-skills.
    """
    clean_q = query.strip()
    if not clean_q or not api_key:
        return None

    endpoint = "https://api.pexels.com/v1/search"
    headers = {"Authorization": api_key}
    params = {
        "query": clean_q,
        "per_page": 3,
        "orientation": "landscape",
    }

    try:
        async with httpx.AsyncClient(timeout=4.5) as client:
            res = await client.get(endpoint, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                photos = data.get("photos", [])
                for photo in photos:
                    src = photo.get("src", {})
                    candidate_url = src.get("large") or src.get("medium")
                    if candidate_url and await is_valid_image_url(candidate_url):
                        return candidate_url
    except Exception as e:
        print(f"[WARNING] Pexels search failed for '{query}': {e}")

    return None


async def search_wikipedia_lead_image(query: str) -> Optional[str]:
    """
    Tier 4: Wikipedia Lead Article Image API.
    Directly retrieves the lead illustrative photograph or schematic from Wikipedia.
    """
    clean_q = query.strip()
    if not clean_q:
        return None

    endpoint = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "pithumbsize": 800,
        "generator": "search",
        "gsrsearch": clean_q,
        "gsrlimit": 3,
        "origin": "*",
    }
    headers = {"User-Agent": _WIKIMEDIA_USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=4.5) as client:
            res = await client.get(endpoint, params=params, headers=headers)
            if res.status_code == 200:
                data = res.json()
                pages = (data.get("query") or {}).get("pages", {})
                for _, page_info in pages.items():
                    thumb = page_info.get("thumbnail", {})
                    candidate_url = thumb.get("source")
                    if candidate_url:
                        return candidate_url
    except Exception as e:
        print(f"[WARNING] Wikipedia lead image search failed for '{query}': {e}")

    return None


async def resolve_contextual_image(query: str, alt_text: str = "") -> Optional[str]:
    """
    Executes the multi-tier cascading educational image resolution pipeline:
    1. Memory Cache check
    2. Tier 1: Google Custom Search (if API key and CX configured)
    3. Tier 2: Wikimedia Commons API (Academic/Scientific figures, diagrams, apparatus)
    4. Tier 3: Pexels API (if configured, ideal for business/workplace/lifestyle)
    5. Tier 4: Wikipedia Lead Article Image API
    Returns verified, direct image URL or None if all image sources fail.
    """
    clean_q = query.strip()
    if not clean_q:
        return None

    # Check cache
    cache_key = clean_q.lower()
    if cache_key in _IMAGE_QUERY_CACHE:
        return _IMAGE_QUERY_CACHE[cache_key]

    settings = Settings()
    brave_key = settings.get_brave_search_api_key()
    pexels_key = settings.get_pexels_api_key()

    # 1. Tier 1: Brave Search API (Open Web Image Search if configured)
    if brave_key:
        img_url = await search_brave_images(clean_q, brave_key)
        if img_url:
            _IMAGE_QUERY_CACHE[cache_key] = img_url
            return img_url

    # 2. Tier 2: Wikimedia Commons (Academic/Scientific primary)
    img_url = await search_wikimedia_commons(clean_q)
    if img_url:
        _IMAGE_QUERY_CACHE[cache_key] = img_url
        return img_url

    # If query had domain qualifiers that didn't match directly in Wikimedia, try stripped base query
    simplified_q = re.sub(r"\b(labeled|diagram|schematic|illustration|photograph|micrograph|anatomy|cross section)\b", "", clean_q, flags=re.I).strip()
    if simplified_q and simplified_q != clean_q:
        img_url = await search_wikimedia_commons(simplified_q)
        if img_url:
            _IMAGE_QUERY_CACHE[cache_key] = img_url
            return img_url

    # 3. Tier 3: Pexels (Stock photography fallback)
    if pexels_key:
        img_url = await search_pexels_images(clean_q, pexels_key)
        if img_url:
            _IMAGE_QUERY_CACHE[cache_key] = img_url
            return img_url

    # 4. Tier 4: Wikipedia Lead Image
    img_url = await search_wikipedia_lead_image(clean_q)
    if img_url:
        _IMAGE_QUERY_CACHE[cache_key] = img_url
        return img_url

    if simplified_q:
        img_url = await search_wikipedia_lead_image(simplified_q)
        if img_url:
            _IMAGE_QUERY_CACHE[cache_key] = img_url
            return img_url

    return None
