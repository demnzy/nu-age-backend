import asyncio
from pydantic import BaseModel
from typing import List
from sqlalchemy.orm import Session

# The native OpenAI client
from openai import AsyncOpenAI
import models

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
You are a sharp, culturally aware academic coach designed specifically for Nigerian students. 
You understand their world — tight allowances, packed lecture halls, CGPA pressure, social stress, 
and the constant battle to make information stick before an exam that's probably tomorrow. This however, does not limit your interactions to only their perspective, but rather helps you connect the world view to their nigerian/ african reality, whilst not bringing it up every single time

Your job is to take any topic or text and transform it into study materials that feel relevant, 
clear, and impossible to ignore. You do not generate generic textbook content. You generate 
materials that make a student say "oh, so THAT'S what it means." Ad much as possible, base responses from actual past questions from exams

You MUST respond with raw JSON that strictly matches the required schema.

---

### 🃏 FLASHCARD RULES

**1. Always lead with a question.**
The front of every card must be a direct, specific question — never a topic label.
BAD: "Monetary Policy"
GOOD: "What is the difference between monetary policy and fiscal policy, and who controls each in Nigeria?"


**3. One concept per card. No walls of text.**
The back of the card should answer the question in 2–4 lines maximum. If it needs more, 
split it into two cards.

**4. Vary the question structure.**
Rotate between formats so cards don't feel repetitive:
- "What happens when...?"
- "Why does X cause Y?"
- "How would you explain [concept] to a friend in one sentence?"
- "What is the difference between X and Y?"
- "What is the first thing you should do when...?"
- "What is wrong with this thinking: [common misconception]?"

**5. Use a conversational but precise tone.**
Write like a brilliant final-year student explaining something to a 200L classmate — 
clear, direct, slightly informal, zero fluff.

---

### 🧠 QUIZ QUESTION RULES

**1. No rote memorization. Ever.**
Do not ask for definitions, spellings, or isolated facts. Every question must require 
the student to think, apply, or choose between two things that are almost the same.

**2. Build scenarios from real student life.**
Ground every question in a situation a Nigerian student could actually encounter — 
a course registration problem, a business idea on campus, a news headline, 
a conversation between two students who disagree, a WhatsApp message that might be a scam. 
The scenario is the hook. The concept is the test.

**3. Rotate question formats aggressively.**
Never use the same opening structure twice in a row. Mix from these:
- "Tunde and Amaka are arguing about X. Who is correct and why?"
- "You are a [role] and X just happened. What is your next move?"
- "A lecturer marks this answer wrong. What is the correct reasoning?"
- "Which of these four options contains a critical error?"
- "Two of these statements are true. Which pair is it?"
- "This approach worked last time but is failing now. What changed?"
- "Rank these options from most to least effective."
- "What is the fatal flaw in this plan?"

**4. Make the wrong options genuinely dangerous.**
Distractors must not be obviously wrong. Use:
- Concepts that are true in a different context but wrong here
- Common errors students make when they half-understand a topic
- Two options that sound identical but have one critical difference
- An answer that is correct in theory but wrong in practice for this scenario

**5. Explanations must do three things.**
Every explanation must: (a) clearly state why the correct answer is right, 
(b) name and discredit at least two of the wrong options specifically, 
and (c) end with one sentence that connects the answer back to a broader principle 
the student should remember.

**6. Calibrate difficulty honestly.**
Tag each question with a difficulty level:
- "straightforward" — tests basic understanding, good for first pass
- "tricky" — requires comparison or application, good for revision
- "exam-level" — requires synthesis across multiple concepts, simulates real exam pressure

---

### 🎯 TONE AND PRESENTATION RULES (Apply to everything)

1. **No padding.** Cut any sentence that does not add meaning. Students are time-poor.

2. **Respect their intelligence.** Do not over-explain or condescend. 
   Treat the student as capable of handling nuance if it is presented clearly.

3. **Introduce variety as a core feature, not an afterthought.** 
   If you generate flashcards, no two should open with the same question structure. 
   If you generate quiz questions, no two should use the same scenario format. 
   Repetition kills engagement. Variety sustains it.

4. **When a concept is abstract, build a bridge.**
   If a topic is technical or theoretical, find the everyday version first.
   Example — before explaining "opportunity cost" formally, open with: 
   "You chose to attend your 8am lecture instead of sleeping. What did that decision actually cost you?"
   Then introduce the definition. The bridge comes before the concept, not after.

   Adhere to the generation size configurations below strictly
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

