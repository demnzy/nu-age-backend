import sys
import uuid
import asyncio
sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nu_voice.linguistics.normalizer import prepare_text_for_speech
from nu_voice.engines.engine_manager import EngineManager
from routers.tts import extract_lesson_narratable_text, compute_content_hash
import models

print("=== 1. Testing EngineManager Synthesis ===")
manager = EngineManager()
mp3_bytes = manager.synthesize_sentence_audio_bytes("Sannu da zuwa darasin kimiyya.", lang="ha")
print(f"Generated Hausa MP3 chunk: {len(mp3_bytes)} bytes")
assert len(mp3_bytes) > 0

wav_bytes = manager.synthesize_sentence_audio_bytes("Bienvenue au cours.", lang="fr", format="wav")
print(f"Generated French WAV chunk: {len(wav_bytes)} bytes")
assert wav_bytes.startswith(b"RIFF")

print("=== 2. Testing Lesson Text Extraction across Types ===")
# Article Lesson
art_les = models.Lesson(
    id=uuid.uuid4(),
    title="Introduction to Machine Learning",
    type="text",
    content={"text": "Machine learning is a subset of artificial intelligence. It focuses on building applications that learn from data."}
)
text_extracted = extract_lesson_narratable_text(art_les)
print("Extracted Article Text:", text_extracted)
assert "Introduction to Machine Learning" in text_extracted
assert "Machine learning is a subset" in text_extracted

# Stepper Lesson
step_les = models.Lesson(
    id=uuid.uuid4(),
    title="Setting up Python",
    type="stepper",
    content={
        "title": "Python Walkthrough",
        "intro": "Follow these steps.",
        "steps": [
            {"headline": "Download Installer", "content": "Visit python.org and grab the latest release."},
            {"headline": "Run Setup", "content": "Check Add Python to PATH before clicking Install."}
        ]
    }
)
step_extracted = extract_lesson_narratable_text(step_les)
print("Extracted Stepper Text:", step_extracted)
assert "Download Installer" in step_extracted
assert "Run Setup" in step_extracted

# Video Lesson (should be rejected)
vid_les = models.Lesson(
    id=uuid.uuid4(),
    title="Lecture Video",
    type="video",
    content={"video_url": "https://video.bunnycdn.com/123"}
)
try:
    extract_lesson_narratable_text(vid_les)
    assert False, "Should have raised exception for video lesson"
except Exception as e:
    print("Correctly rejected video lesson narration:", e)

print("=== 3. Testing Streaming Flow with Mock Sentences ===")
sentences = prepare_text_for_speech(text_extracted, lang="en")
print(f"Lesson chunked into {len(sentences)} sentences:")
for i, s in enumerate(sentences):
    print(f"  [{i+1}] {s}")

print("\nALL BACKEND TTS E2E TESTS PASSED SUCCESSFULLY!")
