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
    assert "pollinations.ai" in lesson_text, "Educational diagram illustration failed to resolve"
    assert "unsplash" not in lesson_text.lower(), "Unsplash should NOT appear anywhere!"
    print("\n[SUCCESS] Zero Unsplash! All visual placeholders resolve to crisp Mermaid diagrams and educational textbook illustrations.")

if __name__ == "__main__":
    asyncio.run(test())
