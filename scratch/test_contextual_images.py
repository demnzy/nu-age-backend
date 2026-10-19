import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import asyncio
from services.image_search_service import resolve_contextual_image
from services.ai_service import resolve_image_placeholders

async def main():
    print("=== TEST 1: Direct Contextual Image Resolution ===")
    queries = [
        ("neuron anatomy labeled diagram", "Neuron Anatomy"),
        ("rosetta stone photograph", "Rosetta Stone"),
        ("xyz12349876foobarbaznonsense", "Gibberish Query"),
    ]
    for q, alt in queries:
        url = await resolve_contextual_image(q, alt)
        print(f"Query: '{q}' -> Resolved URL: {url}")

    print("\n=== TEST 2: Course Draft Image Placeholder Resolution ===")
    mock_draft = {
        "title": "Neuroscience & Cognitive Science Fundamentals",
        "modules": [
            {
                "title": "Module 1: Cellular Neurobiology",
                "lessons": [
                    {
                        "type": "text",
                        "title": "The Structure of a Neuron",
                        "content": {
                            "text": (
                                "## Introduction to Neurons\n\n"
                                "Neurons are the fundamental units of the brain and nervous system.\n\n"
                                "![Neuron Anatomy](IMG:neuron anatomy labeled diagram)\n\n"
                                "Signal propagation happens through action potentials.\n\n"
                                "![Action Potential Propagation](DIAGRAM:flowchart LR\n  Dendrite --> Soma --> AxonHillock --> Axon --> SynapticTerminal)\n\n"
                                "### Edge Case Test (Unmatchable Query Fallback)\n\n"
                                "![Fallback Diagram](IMG:xyz12349876foobarbaznonsense)\n\n"
                                "- Key takeaway 1\n- Key takeaway 2\n- Key takeaway 3"
                            )
                        }
                    }
                ]
            }
        ]
    }

    resolved_draft = await resolve_image_placeholders(mock_draft)
    lesson_text = resolved_draft["modules"][0]["lessons"][0]["content"]["text"]
    print("\nProcessed Lesson Text:\n----------------------")
    print(lesson_text)
    print("----------------------\n")

    # Assertions
    assert "IMG:" not in lesson_text, "Failed: Residual IMG: placeholder remained in lesson text!"
    assert "DIAGRAM:" not in lesson_text, "Failed: Residual DIAGRAM: placeholder remained in lesson text!"
    assert "upload.wikimedia.org" in lesson_text or "thumb.wikimedia.org" in lesson_text, "Failed: Did not resolve Wikimedia image for neuron!"
    assert "mermaid.ink" in lesson_text, "Failed: Mermaid diagram or fallback was not generated!"
    print("ALL TESTS PASSED! Contextual images and zero-broken-link fallbacks working seamlessly.")

if __name__ == "__main__":
    asyncio.run(main())
