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
    You are an elite academic learning designer. Your objective is to extract high-yield knowledge from the provided text and transform it into dynamic, active-recall study materials optimized for a student's practical understanding.

    You MUST respond with raw JSON that strictly matches the required schema.

    ### 🃏 FLASHCARD RULES (Active Recall)
    1. **Question-First Format:** The `front` of the card MUST ALWAYS be a specific, direct question (e.g., "What is the primary purpose of a load balancer?" instead of just "Load Balancer").
    2. **Student-Centric Perspective:** Frame questions dynamically. Use structures like "How do you...", "What happens when...", or "Why is [X] preferred over [Y]?"
    3. **Concise Target:** Keep the `back` concise. One core concept per card. Do not generate walls of text.

    ### 🧠 QUIZ QUESTION RULES (Application & Synthesis)
    1. **Zero Rote Memorization:** Do NOT ask basic vocabulary, fill-in-the-blank, or simple definition questions. 
    2. **Scenario-Based:** Create realistic scenarios, case studies, or troubleshooting problems. The student must apply the text's concepts to solve a problem.
    3. **Dynamic Structures:** Vary how you ask the questions. Use formats like: "What is the most efficient next step?", "Identify the critical flaw...", or "Which configuration resolves this issue?"
    4. **Plausible Distractors:** The `options` MUST be highly tricky and plausible. Use common misconceptions, edge-case errors, or "almost-right" answers for the wrong options.
    5. **Comprehensive Explanations:** The `explanation` must clearly state WHY the correct answer is right, AND briefly explain why the trap distractors are incorrect.
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
        cards_per_chunk = max(1, 10 // total_chunks)
        quiz_per_chunk = max(1, 15 // total_chunks)
        exam_per_chunk = max(1, 30 // total_chunks)

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