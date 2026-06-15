import asyncio
from pydantic import BaseModel
from typing import List
from sqlalchemy.orm import Session

# The native OpenAI client
from openai import AsyncOpenAI
import models
from schemas import *
# Import your session maker AND your Settings class
from database import SessionLocal, Settings

# Initialize OpenAI natively (Notice we removed the base_url proxy!)
client = AsyncOpenAI(
    api_key=Settings().OPENAI_API_KEY,
)

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
    if not text: return []
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

                # Native OpenAI parsing
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


from pydantic import BaseModel, Field
from typing import List, Literal, Annotated, Union



# =====================================================================
# 3. THE GENERATION ENGINE
# =====================================================================

async def draft_course_curriculum(topic: str, context: str) -> dict:
    """
    Generates a highly structured, multi-format course draft for frontend review.
    Uses structured outputs (response_format=AICourseDraft) to guarantee schema compliance.
    """

    system_prompt = """
You are a world-class curriculum architect who has designed syllabi for top university
departments and professional certification programs. Your job is to turn any topic into
a complete, real course — not a slide deck, not a quick overview. The kind of course
a student would pay for and feel they got their money's worth.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP ONE: THINK LIKE A SYLLABUS DESIGNER, NOT A SCHEMA FILLER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Before generating anything, think through how this topic is actually taught.
Draw on real university courses, professional certifications, textbooks, and
well-regarded open-source learning paths (e.g. a CFA curriculum, a CS50-style
course outline, a Coursera specialization, a standard accounting textbook's
chapter structure) as your mental reference points.

Ask yourself:
- If this were a real semester-long or multi-week course, how many distinct
  topics/subtopics would it actually cover?
- What would a textbook's table of contents for this subject look like?
- What foundational concepts MUST come first, and what builds on what?

A real course on a substantive topic typically has 5–8+ modules, each covering
a genuinely distinct subtopic, with 4–8 lessons per module depending on how much
ground that subtopic covers. Some modules will need more lessons than others —
that's normal and expected. Let the TOPIC's natural structure determine the
module count and lesson count, not a fixed template.

If you find yourself converging on exactly 5 modules with exactly 3 lessons each,
STOP. That is almost certainly under-covering the topic. Go back and ask what
real coursework on this subject would include that you're missing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LESSON FORMAT REFERENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You have four lesson types. Use them deliberately and asymmetrically —
not as a rotating set you cycle through.

  "text"
      The lecture. Go deep — rigorous, precise, and dense with value.
      Open with a concrete real-world hook that makes the concept feel urgent
      and relevant.

      FORMATTING IS PART OF THE CONTENT, NOT DECORATION. Use real Markdown:
        - ## or ### subheadings to break up distinct ideas within the lesson
        - **bold** for key terms the first time they're introduced
        - Numbered or bulleted lists for sequences, criteria, or comparisons
        - > blockquotes for definitions or critical warnings
        - Tables (Markdown table syntax) when comparing options, formulas,
          or scenarios side-by-side
        - Code blocks if the topic involves any formula, syntax, or notation
      A "text" lesson that is one undifferentiated wall of prose is a failure,
      regardless of how good the writing is. Structure the page the way a
      well-formatted textbook chapter or technical blog post would be structured.

      Write like the smartest professor the student ever had: no filler, no
      padding, no "Great question!" energy. Just clarity — with structure.

  "scenario"
      The arena. This is NOT a re-explanation of the concept with a story
      wrapper — that's what "text" is for. A scenario exists to TEST whether
      the student can apply a concept they've ALREADY learned, under realistic
      pressure where the "obvious" answer is often wrong.

      A scenario should be unsolvable by someone who only read the text lesson
      passively. It requires them to actually reason about the specific numbers,
      constraints, or trade-offs in THIS situation.

      Give 3–4 choices. At least one should be a trap that looks sensible —
      the kind of mistake a smart but inexperienced person would actually make
      (e.g. the textbook-correct answer that ignores a real-world constraint
      mentioned in the scenario itself). Consequences must explain *why*,
      connecting back to the underlying principle, not just confirm right/wrong.

  "cards"
      Precision ammunition for the brain. Each card is one atomic fact,
      definition, formula, or distinction. No sentences that start with
      "Remember that..." — just the raw, memorable truth.
      A flashcard set should feel like a condensed reference sheet for everything
      covered in the lessons around it — comprehensive enough to be useful for
      review, not just 3-4 token examples.

  "assessment"
      The reckoning. Every question must earn its place.
      Wrong options must be plausible enough that a student who half-understood
      the material would genuinely pause. No decoys like "All of the above...
      of nothing."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRICULUM RULES  (non-negotiable)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1.  LET THE TOPIC SET THE STRUCTURE. Module count, lesson count per module,
    and format choices should all flow from what the subject actually requires —
    not from a fixed pattern. Some modules might be 4 lessons, others 7.
    Some might lean heavily on "text" because the subtopic is dense and
    conceptual; others might lean on "scenario" + "assessment" because the
    subtopic is primarily about application and judgment.

2.  REPETITION OF TYPES IS ALLOWED AND OFTEN CORRECT. Two "text" lessons in a
    row is fine if the subtopic genuinely has two distinct concepts that both
    need deep explanation before any application makes sense. Two "scenario"
    lessons in a row is fine if a concept has multiple distinct failure modes
    worth testing separately. Do not force variety where the content doesn't
    call for it — but do not default to the same sequence in every module either.
    Each module's lesson sequence should look DIFFERENT from the others, because
    each subtopic has different needs.

3.  EVERY MODULE SHOULD HAVE AT LEAST ONE "scenario" OR "assessment" LESSON
    that tests application of that module's concepts — not just lessons that
    explain or list facts. A module that is 100% "text" and "cards" has taught
    nothing about whether the student can USE what they learned.

4.  EARN EVERY LESSON — BUT DON'T STARVE THE TOPIC. "Cut what's inessential"
    does NOT mean "produce the minimum that technically satisfies the schema."
    If a real course/textbook on this subject would cover something, include it.
    The bar is: would a student who paid for this course feel it was thorough,
    or would they feel like they got the free trial version?

5.  MANDATORY FINAL EXAM. The very last lesson of the very last module must be
    an "assessment" that covers the ENTIRE course — drawing questions from
    concepts across multiple modules, not just the last one. It should be
    comprehensive, fair, and genuinely diagnostic of whether the student
    integrated the whole course.

6.  DISTRACTOR QUALITY. Every wrong answer — in assessments and scenarios alike —
    must be a believable mistake. If a student gets it wrong, they should learn
    something from understanding why. No throwaway options.

7.  PRECISION IN SUMMARIES. Each "text" lesson ends with exactly 3 bullet points
    in its summary. These are not vague takeaways. They are the three things the
    student must be able to recall cold, a week from now.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TONE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Smart, clear, occasionally witty — but never at the expense of precision.
Write for someone who is capable and motivated, not someone who needs to be
coddled or entertained into learning.
""".strip()

    user_prompt = f"""
TOPIC: {topic}

TARGET AUDIENCE / CONTEXT:
{context}

Before writing any lessons, plan out the module structure. Think about what a
genuinely thorough course on this topic would need to cover — reference how this
subject is actually taught in real courses, textbooks, or learning paths if you
know of relevant ones. Then build out each module with however many lessons
that subtopic actually needs, in whatever format sequence makes sense for it.

Design the complete, multi-format course curriculum. Be thorough. Be deliberate.
Every module, every lesson, every wrong answer choice should have a reason to exist.
This should feel like a real course, not a summary of one.
""".strip()

    print(f"[INFO] Generating curriculum for: '{topic}'")

    response = await client.beta.chat.completions.parse(
        # gpt-4o handles deeply nested discriminated unions significantly better than gpt-4o-mini.
        # Do not downgrade this without testing schema compliance on complex topics first.
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=AICourseDraft,
        # Slightly raised from 0.3 — at 0.3 the model converged hard on a single
        # "safe" structure (5 modules x 3 lessons, same type order). 0.4-0.5 gives
        # it room to actually vary structure across modules while staying schema-valid.
        temperature=0.45,
    )

    parsed: AICourseDraft = response.choices[0].message.parsed
    return parsed.model_dump()