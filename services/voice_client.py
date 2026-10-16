"""
Voice Client Service for Nu-Age LMS.
Communicates asynchronously with the standalone Nu-Voice microservice.
Supports chunked streaming, health checks, and graceful fallbacks.
"""

import logging
import httpx
from typing import AsyncIterator, Optional, Dict, Any
from database import Settings

logger = logging.getLogger("nu_age.voice_client")
settings = Settings()

# Default to localhost:8000 for local development if NU_VOICE_URL is not explicitly configured
DEFAULT_VOICE_URL = "http://localhost:8000"


def get_voice_service_url() -> str:
    url = getattr(settings, "NU_VOICE_URL", "") or DEFAULT_VOICE_URL
    return url.rstrip("/")


async def check_voice_service_health() -> Dict[str, Any]:
    """Checks whether the Nu-Voice microservice is awake and responsive."""
    base_url = get_voice_service_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{base_url}/v1/tts/health")
            if res.status_code == 200:
                return res.json()
    except Exception as e:
        logger.warning(f"Nu-Voice health check failed ({base_url}): {e}")
    return {"status": "unavailable", "url": base_url}


async def get_voice_models_status() -> Dict[str, Any]:
    """Retrieves neural model readiness and memory residency from Nu-Voice."""
    base_url = get_voice_service_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{base_url}/v1/models/status")
            if res.status_code == 200:
                return res.json()
    except Exception as e:
        logger.debug(f"Nu-Voice models status check notice: {e}")
    return {"status": "unavailable", "url": base_url}


async def preload_remote_voice_models(warmup: bool = True) -> Dict[str, Any]:
    """Triggers on-demand neural model preloading and RAM warmup on Nu-Voice."""
    base_url = get_voice_service_url()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(f"{base_url}/v1/models/preload", params={"warmup": warmup})
            if res.status_code == 200:
                return res.json()
    except Exception as e:
        logger.warning(f"Nu-Voice models preload request notice: {e}")
    return {"status": "failed", "url": base_url}


async def stream_lesson_speech(
    lesson_id: str,
    title: str,
    content_text: str,
    target_lang: str = "en",
    source_lang: str = "en",
    audio_format: str = "mp3",
) -> AsyncIterator[bytes]:
    """
    Streams lesson audio chunks from the standalone Nu-Voice microservice.
    Yields audio byte chunks in real-time as they are synthesized.
    """
    base_url = get_voice_service_url()
    endpoint = f"{base_url}/v1/tts/lesson/stream"
    payload = {
        "lesson_id": lesson_id,
        "title": title or "",
        "content_text": content_text,
        "target_lang": target_lang,
        "source_lang": source_lang,
        "format": audio_format,
    }

    client = httpx.AsyncClient(timeout=180.0)
    try:
        req = client.build_request("POST", endpoint, json=payload)
        res = await client.send(req, stream=True)

        if res.status_code != 200:
            error_body = await res.aread()
            logger.error(f"Nu-Voice returned HTTP {res.status_code}: {error_body.decode(errors='ignore')}")
            await res.aclose()
            await client.aclose()
            raise RuntimeError(f"Voice service error: HTTP {res.status_code}")

        async for chunk in res.aiter_bytes():
            yield chunk

    finally:
        await client.aclose()
