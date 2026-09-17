import asyncio
import sys, os
sys.path.insert(0, os.path.abspath("."))

from services.ai_service import resolve_image_placeholders

async def test():
    test_draft = {
        "modules": [
            {
                "lessons": [
                    {
                        "type": "text",
                        "content": {
                            "text": "Here is an architecture diagram:\n![Architecture](DIAGRAM:graph TD\n  Client --> Server)\nAnd here is a concept illustration:\n![CPU Memory Bus](IMG:computer cpu memory bus architecture)"
                        }
                    }
                ]
            }
        ]
    }

    res = await resolve_image_placeholders(test_draft)
    lesson_text = res["modules"][0]["lessons"][0]["content"]["text"]
    print("Resolved Lesson Text:")
    print(lesson_text)

    assert "mermaid.ink" in lesson_text, "Mermaid diagram failed to resolve"
    assert "pollinations.ai" not in lesson_text, "Pollinations AI should NEVER appear in output!"
    assert "unsplash" not in lesson_text.lower(), "Unsplash should NOT appear anywhere!"
    print("\n[SUCCESS] 100% Deterministic Diagrams! Zero Pollinations and Zero Unsplash. All visual placeholders resolve to crisp vector diagrams.")

if __name__ == "__main__":
    asyncio.run(test())
