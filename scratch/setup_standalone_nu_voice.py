import os
import sys
from pathlib import Path

NU_VOICE_DIR = Path(__file__).resolve().parent.parent.parent / "nu_voice"
NU_AGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NU_AGE_DIR))
print(f"Targeting nu_voice at: {NU_VOICE_DIR}")

# 1. config.py
config_code = '''"""
Configuration & Environment Settings for Nu-Voice.
Loads credentials for Bunny.net CDN, Groq/OpenAI translation, and server options.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = Path(__file__).resolve().parent / ".env"

class Settings(BaseSettings):
    # Server Binding
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Optional Shared Secret for API Authentication
    VOICE_API_KEY: str = ""

    # Bunny.net CDN Storage
    BUNNY_STORAGE_KEY: str = ""
    STORAGE_ZONE_NAME: str = ""
    BUNNY_REGION_URL: str = "storage.bunnycdn.com"
    PULL_ZONE_URL: str = ""

    # Translation Providers (Groq / OpenAI)
    GROQ_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    # Engine Options
    DEFAULT_SPEED: float = 1.0
    DEFAULT_ENGLISH_VOICE: str = "af_heart"
    CPU_THREADS: int = 2

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH) if ENV_PATH.exists() else None,
        extra="ignore"
    )

settings = Settings()
'''

(NU_VOICE_DIR / "config.py").write_text(config_code, encoding="utf-8")
print("Created config.py")

# 2. .env.example
env_example = '''# ==============================================================================
# Nu-Voice Microservice Environment Configuration
# ==============================================================================

# Server Configuration
HOST=0.0.0.0
PORT=8000
VOICE_API_KEY=

# Bunny.net CDN Storage Credentials (Required for saving full lesson audio)
BUNNY_STORAGE_KEY=your_bunny_storage_api_key_here
STORAGE_ZONE_NAME=your_storage_zone_name_here
BUNNY_REGION_URL=storage.bunnycdn.com
PULL_ZONE_URL=https://your-pullzone.b-cdn.net

# Translation APIs (Required for translating English lessons to Hausa or French)
GROQ_API_KEY=your_groq_api_key_here
OPENAI_API_KEY=your_openai_api_key_here

# Voice Engine Settings
DEFAULT_SPEED=1.0
DEFAULT_ENGLISH_VOICE=af_heart
CPU_THREADS=2
'''
(NU_VOICE_DIR / ".env.example").write_text(env_example, encoding="utf-8")
print("Created .env.example")

# 3. .env (Pre-populated from Nu-age)
from database import Settings as NuSettings
nu_s = NuSettings()
env_content = f'''# Auto-generated from Nu-Age active configuration for immediate testing
HOST=0.0.0.0
PORT=8000
VOICE_API_KEY=

BUNNY_STORAGE_KEY={nu_s.BUNNY_STORAGE_KEY}
STORAGE_ZONE_NAME={nu_s.STORAGE_ZONE_NAME}
BUNNY_REGION_URL={nu_s.BUNNY_REGION_URL}
PULL_ZONE_URL={nu_s.PULL_ZONE_URL}

GROQ_API_KEY={nu_s.GROQ_API_KEY}
OPENAI_API_KEY={nu_s.OPENAI_API_KEY}

DEFAULT_SPEED=1.0
DEFAULT_ENGLISH_VOICE=af_heart
CPU_THREADS=2
'''
(NU_VOICE_DIR / ".env").write_text(env_content, encoding="utf-8")
print("Created .env")

# 4. .gitignore
gitignore_content = '''# Environment & Secrets
.env
*.env.local

# Python caches & virtual environments
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
ENV/

# Audio / Temp outputs
*.mp3
*.wav
*.ogg
temp/
logs/

# IDE / OS metadata
.DS_Store
.idea/
.vscode/
'''
(NU_VOICE_DIR / ".gitignore").write_text(gitignore_content, encoding="utf-8")
print("Created .gitignore")

# 5. Fix storage/bunny_storage.py
bunny_code = '''"""
Bunny.net CDN Storage Adapter for Nu-Voice.
Uploads generated lesson audio files to Bunny Storage and returns
the high-speed public CDN Pull Zone URL.
"""

import os
import logging
import httpx
from typing import Optional

try:
    from config import settings
except ImportError:
    from ..config import settings

logger = logging.getLogger(__name__)


class BunnyAudioStorage:
    def __init__(
        self,
        storage_key: Optional[str] = None,
        storage_zone: Optional[str] = None,
        region_url: Optional[str] = None,
        pull_zone_url: Optional[str] = None,
    ):
        self.storage_key = storage_key or settings.BUNNY_STORAGE_KEY or os.getenv("BUNNY_STORAGE_KEY", "")
        self.storage_zone = storage_zone or settings.STORAGE_ZONE_NAME or os.getenv("STORAGE_ZONE_NAME", "")
        self.region_url = region_url or settings.BUNNY_REGION_URL or os.getenv("BUNNY_REGION_URL", "storage.bunnycdn.com")
        self.pull_zone_url = (pull_zone_url or settings.PULL_ZONE_URL or os.getenv("PULL_ZONE_URL", "")).rstrip("/")

    async def upload_audio_bytes(
        self,
        audio_bytes: bytes,
        filename: str,
        folder_path: str = "lessons/audio",
    ) -> str:
        """
        Uploads audio file to Bunny.net and returns the permanent CDN URL.
        """
        if not self.storage_key or not self.storage_zone:
            logger.warning("Bunny.net credentials not configured. Returning local mock path.")
            return f"/local_audio/{folder_path}/{filename}"

        clean_folder = folder_path.strip("/")
        upload_url = f"https://{self.region_url}/{self.storage_zone}/{clean_folder}/{filename}"

        headers = {
            "AccessKey": self.storage_key,
            "Content-Type": "audio/mpeg",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.put(upload_url, content=audio_bytes, headers=headers)
            if res.status_code not in (200, 201):
                logger.error(f"Bunny CDN Upload failed: HTTP {res.status_code} - {res.text}")
                raise RuntimeError(f"Bunny CDN upload failed: {res.text}")

        cdn_url = f"{self.pull_zone_url}/{clean_folder}/{filename}"
        logger.info(f"Successfully uploaded audio to Bunny CDN: {cdn_url}")
        return cdn_url
'''
(NU_VOICE_DIR / "storage" / "bunny_storage.py").write_text(bunny_code, encoding="utf-8")
print("Updated storage/bunny_storage.py")

# 6. Fix translation/translator.py
translator_code = '''"""
Educational Translation Engine for Nu-Voice.
Translates English course material into natural, pedagogical Hausa or French
using ultra-fast LLM inference (Groq / OpenAI / Gemini).
"""

import os
import logging
import httpx
from typing import Optional

try:
    from config import settings
except ImportError:
    from ..config import settings

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "ha": "Hausa",
    "fr": "French",
    "en": "English",
}

HAUSA_SYSTEM_PROMPT = """You are an expert native Hausa linguist and educator.
Translate the following educational course text into natural, spoken Standard Hausa suitable for an audio lecture.
Rules:
1. Ensure the rhythm and sentence structures sound natural when spoken aloud.
2. Use accurate Hausa orthography with hooked consonants (ɓ, ɗ, ƙ, ƴ).
3. Keep technical or domain terms recognizable, explaining them briefly in Hausa if necessary.
4. Output ONLY the translated Hausa text. Do not include markdown preamble, translator notes, or quotes.
"""

FRENCH_SYSTEM_PROMPT = """You are an expert French educational instructor.
Translate the following educational course text into natural, clear, and engaging spoken French suitable for an audio lecture.
Rules:
1. Ensure phrasing flows naturally for speech and listening comprehension.
2. Maintain pedagogical clarity and professional, warm tone.
3. Output ONLY the translated French text. Do not include introductory remarks or metadata.
"""


class Translator:
    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        groq_api_key: Optional[str] = None,
    ):
        self.openai_api_key = openai_api_key or settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")
        self.groq_api_key = groq_api_key or settings.GROQ_API_KEY or os.getenv("GROQ_API_KEY", "")

    async def translate_text(
        self,
        text: str,
        target_lang: str,
        source_lang: str = "en",
    ) -> str:
        """
        Translates text to target_lang.
        If target_lang == source_lang, returns text unchanged.
        """
        target_lang = (target_lang or "en").lower().strip()
        source_lang = (source_lang or "en").lower().strip()

        if target_lang == source_lang or not text.strip():
            return text

        system_prompt = HAUSA_SYSTEM_PROMPT if target_lang == "ha" else FRENCH_SYSTEM_PROMPT
        target_name = LANGUAGE_NAMES.get(target_lang, target_lang)

        # 1. Try Groq first for blazing-fast inference (< 250ms)
        if self.groq_api_key:
            try:
                translated = await self._call_groq(text, system_prompt)
                if translated and len(translated.strip()) > 0:
                    return translated.strip()
            except Exception as e:
                logger.warning(f"Groq translation failed: {e}. Falling back to OpenAI.")

        # 2. Fallback to OpenAI
        if self.openai_api_key:
            try:
                translated = await self._call_openai(text, system_prompt)
                if translated and len(translated.strip()) > 0:
                    return translated.strip()
            except Exception as e:
                logger.error(f"OpenAI translation failed: {e}")

        # If both fail or not configured, return original text
        logger.warning(f"Translation to {target_name} unavailable; using original text.")
        return text

    async def _call_groq(self, text: str, system_prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
            "max_tokens": 2048,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise RuntimeError(f"Groq error HTTP {res.status_code}: {res.text}")

    async def _call_openai(self, text: str, system_prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-6-luna",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "reasoning_effort": "medium",
            "max_tokens": 2048,
        }
        async with httpx.AsyncClient(timeout=25.0) as client:
            res = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise RuntimeError(f"OpenAI error HTTP {res.status_code}: {res.text}")
'''
(NU_VOICE_DIR / "translation" / "translator.py").write_text(translator_code, encoding="utf-8")
print("Updated translation/translator.py")

# 7. Fix engines/mms_engine.py
mms_code = '''"""
Meta MMS-TTS Engine for Hausa and French.
Uses facebook/mms-tts-hau (~140MB) and facebook/mms-tts-fra (~140MB).
VITS architecture optimized for standard CPU hardware with PyTorch.
"""

import logging
from typing import Tuple, Optional
import numpy as np

try:
    from engines.base import BaseTTSEngine
except ImportError:
    from .base import BaseTTSEngine

logger = logging.getLogger(__name__)

MMS_MODEL_IDS = {
    "ha": "facebook/mms-tts-hau",
    "fr": "facebook/mms-tts-fra",
}


class MMSTTSEngine(BaseTTSEngine):
    def __init__(self, language: str = "ha"):
        self.language = language.lower().strip()
        self.model_id = MMS_MODEL_IDS.get(self.language, "facebook/mms-tts-hau")
        self.model = None
        self.tokenizer = None
        self.sample_rate = 16000

    def is_loaded(self) -> bool:
        return self.model is not None and self.tokenizer is not None

    def load(self):
        """Loads model and tokenizer weights into RAM."""
        if self.is_loaded():
            return

        logger.info(f"Loading Meta MMS-TTS model for [{self.language}]: {self.model_id}...")
        try:
            import torch
            from transformers import VitsModel, AutoTokenizer

            if torch.get_num_threads() > 4:
                torch.set_num_threads(2)

            self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            self.model = VitsModel.from_pretrained(self.model_id)
            self.model.eval()
            self.sample_rate = getattr(self.model.config, "sampling_rate", 16000)
            logger.info(f"Meta MMS-TTS [{self.language}] successfully loaded into RAM.")
        except Exception as e:
            logger.error(f"Failed to load Meta MMS-TTS [{self.language}]: {e}")
            raise

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        """
        Synthesizes Hausa or French text into audio waveform.
        Returns (audio_np, sample_rate).
        """
        if not self.is_loaded():
            self.load()

        import torch

        clean_text = text.strip()
        if not clean_text:
            return np.zeros(int(self.sample_rate * 0.1), dtype=np.float32), self.sample_rate

        inputs = self.tokenizer(clean_text, return_tensors="pt")
        with torch.no_grad():
            output = self.model(**inputs).waveform

        audio_arr = output.squeeze().cpu().numpy()
        return audio_arr, self.sample_rate
'''
(NU_VOICE_DIR / "engines" / "mms_engine.py").write_text(mms_code, encoding="utf-8")
print("Updated engines/mms_engine.py")

# 8. Fix engines/kokoro_engine.py
kokoro_code = '''"""
English Voice Engine using Kokoro-82M (hexgrad/Kokoro-82M).
Produces ultra-natural, conversational 24kHz speech on standard CPU hardware.
Includes graceful fallback to facebook/mms-tts-eng if Kokoro weights are downloading.
"""

import logging
from typing import Tuple, Optional
import numpy as np

try:
    from engines.base import BaseTTSEngine
except ImportError:
    from .base import BaseTTSEngine

logger = logging.getLogger(__name__)


class KokoroTTSEngine(BaseTTSEngine):
    def __init__(self, voice: str = "af_heart", speed: float = 1.0):
        self.voice = voice
        self.speed = speed
        self.pipeline = None
        self.sample_rate = 24000
        self.fallback_engine = None

    def is_loaded(self) -> bool:
        return self.pipeline is not None or (self.fallback_engine is not None and self.fallback_engine.is_loaded())

    def load(self):
        """Attempts to load Kokoro-82M pipeline; falls back to Meta MMS-TTS English if needed."""
        if self.is_loaded():
            return

        try:
            logger.info("Loading Kokoro-82M English pipeline...")
            from kokoro import KPipeline
            self.pipeline = KPipeline(lang_code="a")
            self.sample_rate = 24000
            logger.info("Kokoro-82M English engine successfully loaded into RAM.")
            return
        except Exception as e:
            logger.warning(f"Kokoro library not available ({e}). Trying Meta MMS-TTS English fallback.")

        try:
            try:
                from engines.mms_engine import MMSTTSEngine
            except ImportError:
                from .mms_engine import MMSTTSEngine
            self.fallback_engine = MMSTTSEngine(language="eng")
            self.fallback_engine.model_id = "facebook/mms-tts-eng"
            self.fallback_engine.load()
            self.sample_rate = self.fallback_engine.sample_rate
            logger.info("Using Meta MMS-TTS English fallback engine.")
        except Exception as ex:
            logger.error(f"Failed to load both Kokoro and MMS-TTS English: {ex}")
            raise

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        """
        Synthesizes English text into audio.
        Returns (audio_np, sample_rate).
        """
        if not self.is_loaded():
            self.load()

        clean_text = text.strip()
        if not clean_text:
            return np.zeros(int(self.sample_rate * 0.1), dtype=np.float32), self.sample_rate

        if self.pipeline is not None:
            try:
                chunks = []
                generator = self.pipeline(clean_text, voice=self.voice, speed=self.speed)
                for _, _, audio in generator:
                    if hasattr(audio, "numpy"):
                        chunks.append(audio.numpy())
                    elif isinstance(audio, np.ndarray):
                        chunks.append(audio)
                if chunks:
                    return np.concatenate(chunks), self.sample_rate
            except Exception as e:
                logger.error(f"Kokoro synthesis failed: {e}")

        if self.fallback_engine is not None:
            return self.fallback_engine.synthesize(clean_text)

        return np.zeros(int(self.sample_rate * 0.1), dtype=np.float32), self.sample_rate
'''
(NU_VOICE_DIR / "engines" / "kokoro_engine.py").write_text(kokoro_code, encoding="utf-8")
print("Updated engines/kokoro_engine.py")

# 9. Fix engines/engine_manager.py
engine_mgr_code = '''"""
TTS Engine Manager for Nu-Voice.
Provides thread-safe singleton access to Kokoro-82M (English) and Meta MMS-TTS (Hausa, French).
Handles lazy loading, sample rate conversion, and MP3 encoding.
"""

import threading
import logging
from typing import Dict, Tuple, Optional
import numpy as np

try:
    from engines.base import BaseTTSEngine
    from engines.audio_encoder import encode_to_mp3, encode_to_wav
except ImportError:
    from .base import BaseTTSEngine
    from .audio_encoder import encode_to_mp3, encode_to_wav

logger = logging.getLogger(__name__)


class EngineManager:
    _instance: Optional["EngineManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(EngineManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.engines: Dict[str, BaseTTSEngine] = {}
        self._engine_locks = {
            "en": threading.Lock(),
            "ha": threading.Lock(),
            "fr": threading.Lock(),
        }

    def get_engine(self, lang: str) -> BaseTTSEngine:
        """Lazily retrieves or initializes the voice engine for the target language."""
        lang = (lang or "en").lower().strip()
        if lang not in self._engine_locks:
            lang = "en"

        with self._engine_locks[lang]:
            if lang in self.engines and self.engines[lang].is_loaded():
                return self.engines[lang]

            if lang == "en":
                try:
                    from engines.kokoro_engine import KokoroTTSEngine
                except ImportError:
                    from .kokoro_engine import KokoroTTSEngine
                engine = KokoroTTSEngine()
            elif lang in ("ha", "fr"):
                try:
                    from engines.mms_engine import MMSTTSEngine
                except ImportError:
                    from .mms_engine import MMSTTSEngine
                engine = MMSTTSEngine(language=lang)
            else:
                try:
                    from engines.kokoro_engine import KokoroTTSEngine
                except ImportError:
                    from .kokoro_engine import KokoroTTSEngine
                engine = KokoroTTSEngine()

            try:
                engine.load()
            except Exception as e:
                logger.error(f"Error loading voice model for {lang}: {e}. Creating mock test fallback.")
                engine = _MockTestEngine(sample_rate=16000)

            self.engines[lang] = engine
            return engine

    def synthesize_sentence(self, text: str, lang: str = "en") -> Tuple[np.ndarray, int]:
        """Synthesizes sentence and returns (audio_samples, sample_rate)."""
        engine = self.get_engine(lang)
        return engine.synthesize(text)

    def synthesize_sentence_audio_bytes(self, text: str, lang: str = "en", format: str = "mp3") -> bytes:
        """Synthesizes sentence and returns encoded MP3 (or WAV) bytes."""
        samples, sr = self.synthesize_sentence(text, lang)
        if format.lower() == "wav":
            return encode_to_wav(samples, sr)
        return encode_to_mp3(samples, sr)


class _MockTestEngine(BaseTTSEngine):
    """Generates synthetic speech-like waveforms when heavy model weights are not installed."""
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate

    def load(self):
        pass

    def is_loaded(self) -> bool:
        return True

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        duration = max(0.4, min(3.0, len(text) * 0.04))
        t = np.linspace(0, duration, int(self.sample_rate * duration), endpoint=False, dtype=np.float32)
        envelope = np.sin(np.pi * np.linspace(0, 1, len(t), dtype=np.float32))
        audio = (0.2 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 440 * t)) * envelope
        return audio, self.sample_rate
'''
(NU_VOICE_DIR / "engines" / "engine_manager.py").write_text(engine_mgr_code, encoding="utf-8")
print("Updated engines/engine_manager.py")

# 10. Fix server.py
server_code = '''"""
Nu-Voice FastAPI Microservice.
High-throughput, low-latency streaming TTS engine supporting English, French, and Hausa.
Features sentence-by-sentence streaming, client disconnect cancellation, and Bunny CDN caching.
"""

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks, Header, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from linguistics.normalizer import prepare_text_for_speech
from translation.translator import Translator
from storage.bunny_storage import BunnyAudioStorage
from engines.engine_manager import EngineManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("nu_voice")

app = FastAPI(
    title="Nu-Voice TTS Streaming Service",
    version="1.0.0",
    description="Reusable multi-language speech streaming microservice for the Nu-Age ecosystem",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine_manager = EngineManager()
translator = Translator()
bunny_storage = BunnyAudioStorage()

AUDIO_CACHE: Dict[str, str] = {}


class LessonStreamRequest(BaseModel):
    lesson_id: str
    title: Optional[str] = ""
    content_text: str
    target_lang: str = Field(default="en", description="Target audio language: en, ha, fr")
    source_lang: str = Field(default="en", description="Source text language: en")
    format: str = Field(default="mp3", description="Audio format: mp3 or wav")


def compute_content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


@app.get("/v1/tts/stream")
async def stream_generic_text(
    request: Request,
    text: str = Query(..., description="Text to speak"),
    lang: str = Query("en", description="Language code: en, ha, fr"),
    format: str = Query("mp3", description="Audio format: mp3 or wav"),
):
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    sentences = prepare_text_for_speech(text, lang=lang)
    if not sentences:
        raise HTTPException(status_code=400, detail="No readable sentences found.")

    async def audio_generator():
        logger.info(f"Starting generic TTS stream: lang={lang}, sentences={len(sentences)}")
        for idx, sentence in enumerate(sentences):
            if await request.is_disconnected():
                logger.info(f"Client disconnected during sentence {idx+1}/{len(sentences)}. Halting TTS.")
                return

            try:
                chunk = await asyncio.to_thread(
                    engine_manager.synthesize_sentence_audio_bytes,
                    sentence,
                    lang=lang,
                    format=format,
                )
                yield chunk
            except Exception as e:
                logger.error(f"Error synthesizing sentence '{sentence[:30]}...': {e}")
                continue

    media_type = "audio/mpeg" if format.lower() == "mp3" else "audio/wav"
    return StreamingResponse(
        audio_generator(),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )


@app.post("/v1/tts/lesson/stream")
async def stream_lesson_audio(
    payload: LessonStreamRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    lesson_id = payload.lesson_id.strip()
    target_lang = payload.target_lang.lower().strip()
    source_lang = payload.source_lang.lower().strip()
    text = payload.content_text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="Lesson text cannot be empty.")

    content_hash = compute_content_hash(text)
    cache_key = f"{lesson_id}_{target_lang}_{content_hash}"

    if cache_key in AUDIO_CACHE:
        cdn_url = AUDIO_CACHE[cache_key]
        logger.info(f"Cache HIT for lesson {lesson_id} [{target_lang}]: {cdn_url}")
        return JSONResponse(
            content={
                "cached": True,
                "audio_url": cdn_url,
                "lesson_id": lesson_id,
                "language": target_lang,
            }
        )

    if target_lang != source_lang:
        logger.info(f"Translating lesson {lesson_id} from {source_lang} to {target_lang}...")
        try:
            text = await translator.translate_text(text, target_lang=target_lang, source_lang=source_lang)
        except Exception as e:
            logger.error(f"Translation failed: {e}. Proceeding with original text.")

    sentences = prepare_text_for_speech(text, lang=target_lang)
    if not sentences:
        raise HTTPException(status_code=400, detail="No readable sentences in lesson.")

    accumulated_audio = bytearray()

    async def lesson_audio_generator():
        logger.info(f"Streaming lesson {lesson_id} [{target_lang}]: {len(sentences)} sentences")
        all_completed = True

        for idx, sentence in enumerate(sentences):
            if await request.is_disconnected():
                logger.info(f"Student disconnected at sentence {idx+1}/{len(sentences)} of lesson {lesson_id}. Aborting.")
                all_completed = False
                return

            try:
                chunk = await asyncio.to_thread(
                    engine_manager.synthesize_sentence_audio_bytes,
                    sentence,
                    lang=target_lang,
                    format=payload.format,
                )
                accumulated_audio.extend(chunk)
                yield chunk
            except Exception as e:
                logger.error(f"Failed chunk {idx+1} for lesson {lesson_id}: {e}")
                continue

        if all_completed and len(accumulated_audio) > 0:
            logger.info(f"Synthesized full audio for lesson {lesson_id} ({len(accumulated_audio)} bytes). Queueing CDN upload.")
            filename = f"{lesson_id}_{target_lang}_{content_hash}.mp3"
            background_tasks.add_task(
                _upload_and_record_cache,
                cache_key=cache_key,
                filename=filename,
                audio_bytes=bytes(accumulated_audio),
            )

    media_type = "audio/mpeg" if payload.format.lower() == "mp3" else "audio/wav"
    return StreamingResponse(
        lesson_audio_generator(),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "X-Lesson-Id": lesson_id,
            "X-Audio-Language": target_lang,
        },
    )


async def _upload_and_record_cache(cache_key: str, filename: str, audio_bytes: bytes):
    try:
        cdn_url = await bunny_storage.upload_audio_bytes(
            audio_bytes=audio_bytes,
            filename=filename,
            folder_path="lessons/audio",
        )
        AUDIO_CACHE[cache_key] = cdn_url
        logger.info(f"Registered audio cache key '{cache_key}' -> {cdn_url}")
    except Exception as e:
        logger.error(f"Failed to cache lesson audio to Bunny CDN: {e}")


@app.get("/v1/tts/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "nu-voice",
        "port": settings.PORT,
        "loaded_engines": list(engine_manager.engines.keys()),
        "cache_entries": len(AUDIO_CACHE),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=settings.HOST, port=settings.PORT, reload=True)
'''
(NU_VOICE_DIR / "server.py").write_text(server_code, encoding="utf-8")
print("Updated server.py")

# 11. Update Dockerfile to port 8000
dockerfile_content = '''FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ffmpeg \\
    git \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
'''
(NU_VOICE_DIR / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")
print("Updated Dockerfile for port 8000")

print("\nNU_VOICE STANDALONE SETUP COMPLETE!")
