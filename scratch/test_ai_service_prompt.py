import sys
sys.path.insert(0, ".")
from schemas import AILessonContent, CodeLabTestCase

c = AILessonContent(
    text="", cards=[], scenario="", choices=[], questions=[], title="", intro="", steps=[], prompt="", items=[], distractors=[], explanation="", setup_sql="",
    language="html",
    instructions="Create a PWA offline page with service worker registration",
    starter_code="<!DOCTYPE html><html><body><h1>Offline</h1></body></html>",
    solution_code="<!DOCTYPE html><html><body><h1>Offline Ready</h1><script>navigator.serviceWorker.register('/sw.js');</script></body></html>",
    test_cases=[CodeLabTestCase(description="Registers Service Worker", input="", expected_output="serviceWorker")]
)
print("HTML Code Lab Schema validation passed! Language:", c.language)
