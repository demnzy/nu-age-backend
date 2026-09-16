import asyncio
import re
import time
import traceback
import base64
import urllib.parse
import httpx
from pydantic import BaseModel, Field
from typing import List, Literal, Annotated, Union
from sqlalchemy.orm import Session

# The native OpenAI client
from openai import AsyncOpenAI, APITimeoutError, APIStatusError
import models
from schemas import *
# Import your session maker AND your Settings class
from database import SessionLocal, Settings

# --- Explicit timeout + capped retries for OpenAI client -------------------
client = AsyncOpenAI(
    api_key=Settings().OPENAI_API_KEY,
    timeout=170.0,      # per-call ceiling, comfortably bounded
    max_retries=1,      # 1 retry on transient network/5xx errors
)
# -----------------------------------------------------------------------------


# --- Educational Visual & Diagram Support -----------------------------------
# 1. Mermaid.js Diagrams:
#    ![alt text](DIAGRAM:graph TD\n...) -> Encoded to high-res SVG/PNG via mermaid.ink
# 2. Educational Illustrations / Photos:
#    ![alt text](IMG:concrete search query) -> Resolved via Unsplash or high-detail educational diagram generation

IMAGE_PLACEHOLDER_RE = re.compile(r"!\[([^\]]*)\]\(IMG:\s*([^)]+?)\s*\)")
DIAGRAM_PLACEHOLDER_RE = re.compile(r"!\[([^\]]*)\]\(DIAGRAM:\s*([\s\S]+?)\s*\)")
UNSPLASH_API_URL = "https://api.unsplash.com/search/photos"


def _render_mermaid_url(mermaid_code: str) -> str:
    """Encodes a Mermaid.js diagram string into a working hotlinkable SVG/PNG image URL."""
    clean = mermaid_code.strip()
    b64 = base64.urlsafe_b64encode(clean.encode("utf-8")).decode("utf-8")
    return f"https://mermaid.ink/img/{b64}?bgColor=FFFFFF"


def _render_educational_illustration_url(query: str) -> str:
    """Builds a deterministic, high-quality textbook diagram/illustration URL for educational topics."""
    prompt = f"{query.strip()} educational textbook diagram clear infographic vector illustration clean white background"
    encoded_prompt = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=800&height=500&nologo=true"


async def _search_unsplash(client: httpx.AsyncClient, query: str) -> str | None:
    """Return a working Unsplash photo URL for a query, or fallback to educational illustration."""
    access_key = Settings().UNSPLASH_ACCESS_KEY
    if access_key:
        try:
            resp = await client.get(
                UNSPLASH_API_URL,
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {access_key}"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                results = resp.json().get("results") or []
                if results:
                    return results[0]["urls"]["regular"]
        except Exception as e:
            print(f"[WARNING] Unsplash lookup failed for query '{query}': {e}")

    # Fallback to high-definition educational illustration
    return _render_educational_illustration_url(query)


async def resolve_image_placeholders(data: dict) -> dict:
    """
    Walk the parsed course draft:
    1. Converts `![alt](DIAGRAM:mermaid_code)` into working Mermaid rendered diagrams.
    2. Resolves `![alt](IMG:query)` to Unsplash photo or educational illustration.
    """
    queries: set[str] = set()

    def collect(node):
        if isinstance(node, str):
            queries.update(m.group(2) for m in IMAGE_PLACEHOLDER_RE.finditer(node))
        elif isinstance(node, dict):
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(data)

    resolved_images: dict[str, str | None] = {}
    if queries:
        semaphore = asyncio.Semaphore(5)
        async with httpx.AsyncClient() as client:
            async def resolve_one(q: str):
                async with semaphore:
                    resolved_images[q] = await _search_unsplash(client, q)
            await asyncio.gather(*(resolve_one(q) for q in queries))

    def replace_in_text(text: str) -> str:
        # First resolve Mermaid diagram placeholders
        def _sub_diag(m: re.Match) -> str:
            alt, code = m.group(1), m.group(2)
            url = _render_mermaid_url(code)
            return f"![{alt}]({url})"

        text = DIAGRAM_PLACEHOLDER_RE.sub(_sub_diag, text)

        # Next resolve image / illustration placeholders
        def _sub_img(m: re.Match) -> str:
            alt, query = m.group(1), m.group(2)
            url = resolved_images.get(query)
            if not url:
                url = _render_educational_illustration_url(query)
            return f"![{alt}]({url})"

        return IMAGE_PLACEHOLDER_RE.sub(_sub_img, text)

    def walk(node):
        if isinstance(node, str):
            if IMAGE_PLACEHOLDER_RE.search(node) or DIAGRAM_PLACEHOLDER_RE.search(node):
                return replace_in_text(node)
            return node
        elif isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        elif isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(data)
# -------------------------------------------------------------------------------


class GeneratedFlashcard(BaseModel):
    front: str
    back: str

class GeneratedQuestion(BaseModel):
    question_text: str
    options: List[str]
    answer_index: int
    explanation: str

# This is the exact structure OpenAI will mathematically enforce
class AIResponse(BaseModel):
    flashcards: List[GeneratedFlashcard]
    questions: List[GeneratedQuestion]


def chunk_text(text: str, chunk_size: int = 15000, overlap: int = 1000) -> List[str]:
    if not text:
        return []
    chunks, start, text_length = [], 0, len(text)
    while start < text_length:
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


async def process_and_generate_content(user_id: str, material_ids: List[str], content_text: str, types_requested: List[str]):
    """Thread-safe background worker using OpenAI's guaranteed Structured Outputs."""
    db: Session = SessionLocal()

    system_prompt = """
You are a sharp academic coach built for students who are under real pressure —
packed schedules, high-stakes exams, and the constant need to make information
stick fast. You understand their world well enough to make any concept land in it,
but you don't force the reference. When a local analogy works, use it. When it doesn't, don't.

Your job is to take any topic or text and turn it into study materials that feel
relevant, precise, and impossible to ignore. Not generic textbook rehashes.
Materials that make a student say: "oh — so THAT'S what it means."

Where possible, ground questions and examples in real past exam patterns for the subject.

You MUST respond with raw JSON that strictly matches the required schema.
Do not include markdown, code fences, or preamble. JSON only.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🃏  FLASHCARD RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. ALWAYS LEAD WITH A QUESTION.
   The front of every card is a direct, specific question — never a topic label.
   BAD:  "Monetary Policy"
   GOOD: "What is the difference between monetary policy and fiscal policy,
          and who controls each one?"

2. ONE CONCEPT PER CARD. NO WALLS OF TEXT.
   The back answers the question in 2–4 lines. If it needs more, split into two cards.

3. VARY THE QUESTION STRUCTURE. Rotate between these formats so cards don't blur together:
   - "What is the difference between X and Y?"
   - "Why does X cause Y?"
   - "What happens when [condition] changes?"
   - "What is wrong with this thinking: [common misconception]?"
   - "How would you explain [concept] to someone in one sentence?"
   - "What is the first thing you should do when [situation]?"
   - "Under what conditions does X not apply?"

4. TONE: Precise, clear, slightly informal — zero fluff.
   Write like a brilliant final-year explaining something to a junior who's smart
   but pressed for time. No hedging. No padding. Just the point.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠  QUIZ QUESTION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. NO ROTE MEMORISATION. EVER.
   No definitions. No spellings. No isolated facts lifted straight from a textbook.
   Every question must require the student to think, apply, compare, or decide.

2. BUILD SCENARIOS. EVERY TIME.
   Ground every question in a concrete situation — a business decision, a technical
   failure, a disagreement between two people, a suspicious message, a real news event.
   The scenario is the hook. The concept is what's being tested.

3. ROTATE QUESTION FORMATS AGGRESSIVELY.
   Never use the same opening structure twice in a row. Pull from:
   - "Tunde and Amaka are arguing about X. Who is correct and why?"
   - "You are a [role] and X just happened. What is your next move?"
   - "A lecturer marks this answer wrong. What is the correct reasoning?"
   - "Which of these options contains a critical error?"
   - "Two of these statements are true. Which pair?"
   - "This approach worked last time but is failing now. What changed?"
   - "What is the fatal flaw in this plan?"
   - "Rank these from most to least effective."

4. MAKE WRONG OPTIONS GENUINELY DANGEROUS.
   Distractors must not be obviously wrong. Use:
   - Concepts that are true in a different context, but wrong here
   - The mistake a student makes when they half-understood the material
   - Two options that sound nearly identical but differ on one critical word
   - An answer that is correct in theory, but wrong in practice for this scenario

5. EXPLANATIONS MUST DO THREE THINGS.
   Every explanation must:
   (a) State clearly why the correct answer is right
   (b) Name and discredit at least two wrong options specifically — by their logic, not just their label
   (c) End with one sentence connecting the answer to a broader principle worth remembering

6. TAG EVERY QUESTION WITH A DIFFICULTY LEVEL.
   - "straightforward" — tests basic understanding; good for first pass
   - "tricky"          — requires comparison or application; good for revision
   - "exam-level"      — requires synthesis across concepts; simulates real exam pressure

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯  TONE + PRESENTATION  (applies to everything)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NO PADDING.
Cut any sentence that does not add meaning. Students are time-poor and will notice
filler before they notice accuracy.

RESPECT THEIR INTELLIGENCE.
Write for someone capable of handling nuance when it's presented clearly.
Don't over-explain. Don't condescend. Don't celebrate basic effort.

VARIETY IS NON-NEGOTIABLE.
No two flashcards should open with the same question structure.
No two quiz questions should use the same scenario format.
Repetition kills engagement. Variation sustains it.

BUILD A BRIDGE BEFORE THE CONCEPT.
If a topic is abstract or technical, lead with the everyday version first.
Example — before "opportunity cost": 
  "You skipped sleep to finish an assignment. What did that decision actually cost you?"
Then introduce the definition. The bridge comes before the concept, not after.

Adhere strictly to the generation size configurations provided.
"""

    try:
        chunks = chunk_text(content_text)
        total_chunks = len(chunks)

        if total_chunks == 0:
            print("[WARNING] No text to process.")
            return

        print(f"[DEBUG] Document split into {total_chunks} chunks. Firing at OpenAI...")

        # --- THE SMART LIMIT MATH ---
        # Divide the requested defaults by the number of chunks so the total matches the goal
        cards_per_chunk = max(1, 15 // total_chunks)
        quiz_per_chunk = max(1, 20 // total_chunks)
        exam_per_chunk = max(1, 40 // total_chunks)

        for index, chunk in enumerate(chunks):
            try:
                print(f"[DEBUG] Processing chunk {index + 1} of {total_chunks}...")

                # --- DYNAMIC TARGET INSTRUCTIONS ---
                generation_goals = []

                if "flashcards" in types_requested:
                    generation_goals.append(f"- Generate exactly {cards_per_chunk} Flashcards.")

                # Differentiate between Exam mode and Quiz mode
                if "exam" in types_requested:
                    generation_goals.append(f"- Generate exactly {exam_per_chunk} EXAM multiple-choice questions (Highly complex, scenario-heavy, tricky distractors).")
                elif "quiz" in types_requested:
                    generation_goals.append(f"- Generate exactly {quiz_per_chunk} QUIZ multiple-choice questions (Focus on core concepts and immediate factual application).")

                goals_text = "\n".join(generation_goals)

                # Combine the goals and the text for the user message
                user_prompt = f"TARGET OUTPUTS FOR THIS CHUNK:\n{goals_text}\n\nTEXT TO PROCESS:\n{chunk}"

                # Native OpenAI parsing with structured outputs
                response = await client.beta.chat.completions.parse(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    response_format=AIResponse,
                    temperature=0.2
                )

                # The response is already a perfectly formatted Python object
                result = response.choices[0].message.parsed

                if "flashcards" in types_requested and result.flashcards:
                    for card in result.flashcards:
                        db.add(models.Flashcard(
                            user_id=user_id, material_id=material_ids[0], front=card.front, back=card.back
                        ))

                if "quiz" in types_requested or "exam" in types_requested:
                    if result.questions:
                        for q in result.questions:
                            db.add(models.Question(
                                user_id=user_id, material_id=material_ids[0], question_text=q.question_text,
                                options=q.options, answer_index=q.answer_index, explanation=q.explanation
                            ))

                db.commit()
                print(f"[SUCCESS] Chunk {index + 1} saved.")

                # A 1-second pause keeps you safely under OpenAI's Tier 1 limits
                await asyncio.sleep(1)

            except Exception as e:
                print(f"[ERROR] OpenAI failed on chunk {index + 1}: {e}")
                db.rollback()

        print(f"[COMPLETE] OpenAI Generation finished flawlessly.")

    except Exception as e:
        print(f"[CRITICAL ERROR] Background task failed: {e}")

    finally:
        # Unlock the materials so the frontend knows to stop spinning
        materials = db.query(models.StudyMaterial).filter(
            models.StudyMaterial.id.in_(material_ids)
        ).all()

        for mat in materials:
            mat.is_generating = False

        db.commit()
        db.close()
        print("[DEBUG] Database session closed and materials unlocked.")


# =====================================================================
# 3. THE GENERATION ENGINE (Two-Stage Hierarchical Generator - OpenAI Locked)
# =====================================================================

OPENAI_MODEL = getattr(Settings(), "OPENAI_MODEL", None) or "gpt-4o-mini"

LESSON_CONTENT_SCHEMA = {
    "video": ["file_name", "video_url", "accompanying_text"],
    "audio": ["file_name", "audio_path", "accompanying_text"],
    "document": ["file_name", "document_url", "accompanying_text"],
    "text": ["text"],
    "cards": ["cards"],
    "assessment": ["questions"],
    "scenario": ["scenario", "choices"],
    "stepper": ["title", "intro", "steps"],
    "sequencer": ["prompt", "items"],
    "cloze": ["text", "distractors", "explanation"],
    "code_lab": ["language", "instructions", "starter_code", "solution_code", "setup_sql", "test_cases"],
}


STAGE_1_ARCHITECT_PROMPT = """
You are a world-class curriculum architect and master educator designing comprehensive, university-grade courses.
Your syllabus design is grounded in:
1. Cognitive Load Theory (Sweller): Progressive scaffolding from fundamental concepts to procedural mastery, complex application, and synthesis.
2. Bloom's Revised Taxonomy: Map formats to cognitive levels:
   - Remember & Understand: "text", "cards", "cloze"
   - Apply & Analyze: "stepper", "sequencer", "code_lab"
   - Evaluate & Create: "scenario", "assessment"
3. Multi-Modal Retention: Vary pedagogical formats across lessons so learning never becomes passive or monotonous.

Supported lesson types:
- "text": In-depth conceptual deep dive with structured markdown, real-world analogies, tables, and inline image placeholders.
- "cards": Atomic flashcards for key definitions, formulas, and critical distinctions.
- "scenario": High-stakes decision challenge testing critical judgment and trade-offs under real-world constraints.
- "assessment": Rigorous diagnostic multiple-choice questions with scenario-grounded options.
- "stepper": Phased, sequential walkthrough guiding students through a multi-stage methodology or process.
- "sequencer": Chronological or algorithmic reordering challenge where students arrange scrambled steps into the correct sequence.
- "cloze": Active recall passage with fill-in-the-blank tokens [[blank]] or [[blank|hint]] to test precise conceptual vocabulary.
- "code_lab": Hands-on coding or SQL playground with starter boilerplate, reference solution, and unit test cases.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRICULUM STRUCTURING RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. COURSE SCOPE: A comprehensive course must contain 5 to 7 modules, each with 4 to 6 lessons.
2. DOMAIN-SPECIFIC FORMAT SELECTION:
   - For coding, data science, databases, or algorithms: You MUST include "code_lab" lessons (for programming exercises) and "sequencer" lessons (for algorithm execution, lifecycle phases, or data pipelines).
   - For technical, engineering, operations, or laboratory workflows: Heavily utilize "stepper" and "sequencer" lessons.
   - For business, management, legal, ethics, or leadership topics: Heavily utilize "scenario" lessons with nuanced consequences.
   - For all subjects: Integrate "cloze" and "cards" for active recall vocabulary retention, and "text" for foundational explanations.
3. FINAL CAPSTONE EXAM: The very last lesson of the very last module MUST be an "assessment" serving as a comprehensive final examination covering all modules.
4. PEDAGOGICAL GOAL: Assign a clear, specific learning outcome or mental model to every lesson blueprint.
""".strip()


STAGE_2_SYNTHESIZER_PROMPT = """
You are an elite instructional content synthesizer. Your task is to generate rich, production-ready educational content for every lesson in a specified course module, adhering strictly to the provided lesson blueprint.

You must populate the `content` field for each lesson according to its `type`.
All unused fields in the `content` object must be left empty (empty string "" or empty list []).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LESSON TYPE CONTENT SPECIFICATIONS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. "text" (Rich Reading Lecture):
   - Rich Markdown content in the `text` field.
   - Start with a compelling real-world hook that explains why this concept matters.
   - Use structured Markdown: ## and ### subheadings, **bold** key terms on first introduction, numbered/bulleted lists, > blockquotes for definitions or core rules, and Markdown tables when comparing concepts or formulas.
   - VISUAL AIDS & DIAGRAMS (Include 1-2 relevant visual aids where helpful):
     a) For architectures, memory layouts, flowcharts, data structures, or execution lifecycles:
        Use Mermaid.js diagram syntax inside a DIAGRAM placeholder:
        ![Memory Pointer Layout](DIAGRAM:graph LR\n  A[Pointer ptr] -->|0x7ffd| B[Value: 42])
     b) For physical, scientific, or real-world concepts:
        Use descriptive educational textbook illustration query inside an IMG placeholder:
        ![Photosynthesis Process](IMG:plant chloroplast light reaction and calvin cycle textbook diagram)
     NEVER invent random external URLs. Only use DIAGRAM: or IMG: placeholders.
   - End the `text` field with exactly 3 bullet points summarizing the core takeaways.

2. "cards" (Atomic Flashcard Deck):
   - Populate `cards`: a list of 4 to 8 standalone, memorable facts, definitions, distinctions, or formulas.

3. "scenario" (Decision Challenge):
   - Populate `scenario`: A detailed, high-stakes situation setup with concrete characters, numbers, and constraints.
   - Populate `choices`: 3 to 4 objects with `text` and `consequence`.
     At least one choice must be a subtle trap (looks textbook-correct but fails due to a real-world constraint in the scenario).
     Each `consequence` must deeply explain the physiological, business, or technical logic of why it succeeds or fails.

4. "assessment" (Diagnostic Quiz):
   - Populate `questions`: 3 to 6 high-quality multiple choice questions (or 10-15 for a final exam module).
   - Each question has `text` and 2 to 4 `options` ({text, is_correct}).
   - At least one option must have `is_correct: True`. Distractors must be plausible misconceptions, not silly throwaways.

5. "stepper" (Phased Walkthrough):
   - Populate `title`: Concise title for the walkthrough.
   - Populate `intro`: 1-2 sentence orientation explaining the goal of the walkthrough.
   - Populate `steps`: 3 to 6 ordered step objects.
     Each step has:
     - `tag`: Short phase badge (e.g. "Phase 1", "Step 1", "Preparation", "Execution")
     - `headline`: Clear action or phase title
     - `content`: Deep, instructional explanation of the steps and actions to take
     - `takeaway`: Critical tip, common failure point, or rule of thumb

6. "sequencer" (Process & Timeline Challenge):
   - Populate `prompt`: Clear prompt instructing the student on what workflow, timeline, or algorithm they must reconstruct.
   - Populate `items`: 3 to 6 sequence item objects.
     Each item has:
     - `id`: Short unique identifier (e.g. "step_1", "step_2")
     - `label`: Description of the action, event, or statement
     - `correct_order`: 1-based integer position in the correct sequence (1, 2, 3...)
     - `explanation`: Clear explanation of why this step belongs at this position in the sequence

7. "cloze" (Active Recall Fill-in-the-Blanks):
   - Populate `text`: A 2 to 4 sentence educational passage where 2 to 4 critical technical keywords are replaced with brackets:
     Format: `[[correct_word]]` or `[[correct_word|optional hint]]`
     Example: "In relational databases, [[ACID|acronym for reliability properties]] guarantees that transactions are processed reliably, while [[atomicity]] ensures all-or-nothing execution."
     MUST contain at least one `[[...]]` blank token!
   - Populate `distractors`: 3 to 5 plausible incorrect terms that test precision.
   - Populate `explanation`: Thorough educational explanation of why the blanked terms are correct.

8. "code_lab" (Interactive Coding Playground):
   - Populate `language`: Select the matching language for the course: "cpp", "javascript", "typescript", "java", "python", "c", or "sql".
   - Populate `instructions`: Comprehensive problem statement, input/output specifications, and constraints.
   - Populate `starter_code`: Clean, idiomatic boilerplate with proper headers/signatures, docstrings, and `# TODO` / `// TODO` markers. CRITICAL: starter_code is strictly required and must NEVER be empty.
   - Populate `solution_code`: Complete, optimal, passing solution code.
   - Populate `setup_sql`: For "sql", DDL `CREATE TABLE` and sample `INSERT INTO` statements for SQLite. For other languages, leave empty string "".
   - Populate `test_cases`: 2 to 4 test case objects ({description, input, expected_output}).
""".strip()


def normalize_lesson(lesson: AILesson) -> dict:
    """
    Normalizes a generated lesson into the exact dictionary shape expected
    by the frontend course builder, pruning extraneous empty keys and applying
    pedagogical sanity defaults.
    """
    t = lesson.type
    raw_content = lesson.content.model_dump()
    allowed_keys = LESSON_CONTENT_SCHEMA.get(t, [])
    content = {k: v for k, v in raw_content.items() if k in allowed_keys}

    if t == "text":
        if not content.get("text", "").strip():
            content["text"] = f"# {lesson.title}\n\nCore instructional content and principles."

    elif t == "cards":
        cards = content.get("cards", [])
        if not cards or not any(str(x).strip() for x in cards):
            content["cards"] = [f"Core principle: {lesson.title}"]

    elif t == "scenario":
        if not content.get("scenario", "").strip():
            content["scenario"] = f"Scenario challenge regarding {lesson.title}."
        choices = content.get("choices", [])
        if len(choices) < 2:
            content["choices"] = [
                {"text": "Analyze the primary constraints first", "consequence": "Correct approach ensuring all dependencies are validated."},
                {"text": "Execute immediately without validation", "consequence": "Flawed approach leading to unexpected edge case failures."}
            ]

    elif t == "assessment":
        questions = content.get("questions", [])
        if not questions:
            content["questions"] = [{
                "text": f"What is the key principle behind {lesson.title}?",
                "options": [
                    {"text": "The primary operational standard", "is_correct": True},
                    {"text": "A deprecated legacy method", "is_correct": False},
                ]
            }]
        else:
            for q in questions:
                opts = q.get("options", [])
                if opts and not any(o.get("is_correct") for o in opts):
                    opts[0]["is_correct"] = True

    elif t == "stepper":
        steps = content.get("steps", [])
        if not steps:
            content["steps"] = [{
                "tag": "Step 1",
                "headline": "Getting Started",
                "content": f"Fundamental setup and concepts for {lesson.title}.",
                "takeaway": "Understand the initial requirements."
            }]
        else:
            for i, st in enumerate(steps, start=1):
                if not st.get("tag"):
                    st["tag"] = f"Step {i}"
                if not st.get("headline"):
                    st["headline"] = f"Phase {i}"
                if not st.get("content"):
                    st["content"] = "Follow the standard operational guideline."
                if not st.get("takeaway"):
                    st["takeaway"] = "Execute step accurately."

    elif t == "sequencer":
        if not content.get("prompt", "").strip():
            content["prompt"] = f"Arrange the following steps for {lesson.title} in the correct execution order:"
        items = content.get("items", [])
        if len(items) < 2:
            content["items"] = [
                {"id": "step_1", "label": "Initial assessment and setup", "correct_order": 1, "explanation": "Always start with preparation."},
                {"id": "step_2", "label": "Execution of primary process", "correct_order": 2, "explanation": "Execute after prerequisites are met."},
                {"id": "step_3", "label": "Verification and review", "correct_order": 3, "explanation": "Validate final results."}
            ]
        else:
            for i, it in enumerate(items, start=1):
                if not it.get("id"):
                    it["id"] = f"step_{i}"
                if not it.get("correct_order"):
                    it["correct_order"] = i

    elif t == "cloze":
        raw_text = content.get("text", "")
        if "[[" not in raw_text or "]]" not in raw_text:
            words = [w for w in re.findall(r'\b[A-Za-z]{4,}\b', raw_text)]
            if words:
                chosen = words[len(words) // 2]
                content["text"] = raw_text.replace(chosen, f"[[{chosen}]]", 1)
            else:
                content["text"] = f"In {lesson.title}, the primary mechanism is [[active recall]]."
        if not content.get("distractors"):
            content["distractors"] = ["Irrelevant", "Opposite", "Secondary"]
        if not content.get("explanation"):
            content["explanation"] = "Review the context of the sentence to determine the correct technical term."

    elif t == "code_lab":
        if not content.get("instructions", "").strip():
            content["instructions"] = f"Implement the required functionality for {lesson.title}."
        if not content.get("language"):
            content["language"] = "python"
        if not content.get("starter_code", "").strip():
            content["starter_code"] = "# Write your solution below\ndef solution():\n    pass\n"
        if not content.get("solution_code", "").strip():
            content["solution_code"] = "# Reference solution\ndef solution():\n    return True\n"
        if not content.get("test_cases"):
            content["test_cases"] = [
                {"description": "Default test case", "input": "", "expected_output": "True"}
            ]

    return {
        "id": "new",
        "title": lesson.title,
        "type": t,
        "content": content,
    }


async def draft_curriculum_blueprint(topic: str, context: str) -> AICourseBlueprint:
    """Stage 1: Generates the pedagogical course blueprint using OpenAI structured outputs."""
    user_prompt = f"""
TOPIC: {topic}

TARGET AUDIENCE / CONTEXT:
{context}

Design a comprehensive course curriculum blueprint. Select the optimal lesson types
from the 8 supported formats to match each subtopic's pedagogical needs.
Ensure 5 to 7 modules, with 4 to 6 lessons per module.
Conclude the final module with a comprehensive final assessment.
""".strip()

    response = await asyncio.wait_for(
        client.beta.chat.completions.parse(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": STAGE_1_ARCHITECT_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=AICourseBlueprint,
            temperature=0.4,
        ),
        timeout=120.0,
    )
    return response.choices[0].message.parsed


async def synthesize_module_content(
    course_name: str,
    course_desc: str,
    context: str,
    module_bp: AIModuleBlueprint,
    module_index: int,
    total_modules: int,
) -> dict:
    """Stage 2: Synthesizes rich educational content for a single module with retries."""
    lessons_manifest = "\n".join([
        f"- Lesson {i+1}: '{l.title}' | Type: '{l.type}' | Goal: {l.pedagogical_goal}"
        for i, l in enumerate(module_bp.lessons)
    ])

    user_prompt = f"""
COURSE: {course_name}
COURSE DESCRIPTION: {course_desc}
TARGET AUDIENCE: {context}

MODULE ({module_index + 1} of {total_modules}): {module_bp.title}
PEDAGOGICAL FOCUS: {module_bp.pedagogical_focus}

LESSONS TO GENERATE FOR THIS MODULE:
{lessons_manifest}

Synthesize complete, production-ready educational content for every single lesson listed above.
Follow the exact field rules for each lesson type.
""".strip()

    # Retry up to 2 times on transient API issues
    last_error = None
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(
                client.beta.chat.completions.parse(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": STAGE_2_SYNTHESIZER_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format=AIModuleContent,
                    temperature=0.35,
                ),
                timeout=160.0,
            )
            parsed: AIModuleContent = response.choices[0].message.parsed
            normalized_lessons = [normalize_lesson(l) for l in parsed.lessons]
            return {
                "id": "new_module",
                "title": module_bp.title,
                "lessons": normalized_lessons,
            }
        except Exception as e:
            last_error = e
            print(f"[WARNING] Module '{module_bp.title}' generation attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(1.5)

    raise RuntimeError(f"Failed to generate content for module '{module_bp.title}': {last_error}")


async def draft_course_curriculum(topic: str, context: str) -> dict:
    """
    Two-stage hierarchical course curriculum generation permanently locked to OpenAI.
    Stage 1: Creates an educationally grounded course blueprint.
    Stage 2: Concurrently synthesizes rich lesson content across all 8 supported formats.
    """
    print(f"[INFO] Initiating two-stage OpenAI curriculum generation for: '{topic}'")
    start = time.monotonic()

    try:
        # 1. Stage 1: Generate Blueprint
        blueprint = await draft_curriculum_blueprint(topic=topic, context=context)
        print(
            f"[INFO] Stage 1 blueprint generated in {time.monotonic() - start:.1f}s: "
            f"'{blueprint.course_name}' with {len(blueprint.modules)} modules."
        )

        # 2. Stage 2: Concurrently synthesize all modules
        semaphore = asyncio.Semaphore(3)

        async def synthesize_one(idx: int, mbp: AIModuleBlueprint) -> dict:
            async with semaphore:
                return await synthesize_module_content(
                    course_name=blueprint.course_name,
                    course_desc=blueprint.description,
                    context=context,
                    module_bp=mbp,
                    module_index=idx,
                    total_modules=len(blueprint.modules),
                )

        tasks = [synthesize_one(i, mbp) for i, mbp in enumerate(blueprint.modules)]
        modules_data = await asyncio.gather(*tasks)

        draft_data = {
            "course_name": blueprint.course_name,
            "description": blueprint.description,
            "objectives": blueprint.objectives,
            "modules": modules_data,
        }

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        print(f"[ERROR] Curriculum generation exceeded {elapsed:.1f}s hard timeout for topic '{topic}'")
        raise RuntimeError("Generation took too long. Try a narrower topic or shorter context.")
    except APITimeoutError as e:
        print(f"[ERROR] OpenAI API timeout for topic '{topic}': {e}")
        raise RuntimeError("The AI provider timed out. Please try again.")
    except APIStatusError as e:
        print(f"[ERROR] OpenAI API error ({e.status_code}) for topic '{topic}': {e.response.text}")
        raise RuntimeError(f"The AI provider returned an error (status {e.status_code}).")

    elapsed = time.monotonic() - start
    total_lessons = sum(len(m.get("lessons", [])) for m in draft_data.get("modules", []))
    print(
        f"[TIMING] Full curriculum generated for '{topic}' in {elapsed:.1f}s "
        f"({len(draft_data.get('modules', []))} modules, {total_lessons} lessons)."
    )

    # 3. Resolve image placeholders via Unsplash
    draft_data = await resolve_image_placeholders(draft_data)

    return draft_data


async def run_course_draft_job(job_id: str, topic: str, context: str):
    """
    Background wrapper: owns the job row lifecycle (PENDING -> RUNNING ->
    SUCCESS/FAILED) so the HTTP request never has to wait for generation.
    Same shape as process_and_generate_content's is_generating flag pattern,
    but with explicit status + result/error columns for polling.
    """
    db: Session = SessionLocal()

    try:
        job = db.query(models.CourseDraftJob).filter(models.CourseDraftJob.id == job_id).first()
        if not job:
            print(f"[ERROR] Job {job_id} vanished before it could start.")
            return

        job.status = models.JobStatus.RUNNING
        db.commit()

        draft_data = await draft_course_curriculum(topic=topic, context=context)

        job.status = models.JobStatus.SUCCESS
        job.result = draft_data
        db.commit()
        print(f"[SUCCESS] Job {job_id} completed for topic '{topic}'.")

    except Exception as e:
        # Full trace server-side for debugging; short, safe message stored for the frontend.
        print(f"[CRITICAL ERROR] Job {job_id} failed: {traceback.format_exc()}")
        db.rollback()

        job = db.query(models.CourseDraftJob).filter(models.CourseDraftJob.id == job_id).first()
        if job:
            job.status = models.JobStatus.FAILED
            job.error = str(e) if isinstance(e, RuntimeError) else "Failed to generate the course draft. The AI structure may have been invalid."
            db.commit()

    finally:
        db.close()