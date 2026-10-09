import re
import json
import asyncio
import os
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

def _get_api_key() -> str:
    try:
        from database import Settings
        return Settings().OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "") or getattr(Settings(), "AGENT_ROUTER_API_KEY", "")
    except Exception:
        return os.getenv("OPENAI_API_KEY", "") or os.getenv("AGENT_ROUTER_API_KEY", "")

def _get_gemini_key() -> str:
    try:
        from database import Settings
        return getattr(Settings(), "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    except Exception:
        return os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")

class ParsedCBTQuestion(BaseModel):
    question_text: str = Field(..., description="Clean question body without question numbers")
    options: List[str] = Field(..., description="Exact 4 or 5 options (A-D or A-E) without letter prefixes")
    answer_index: int = Field(0, description="0 for A, 1 for B, 2 for C, 3 for D, 4 for E")
    explanation: Optional[str] = Field("", description="Step-by-step solution or conceptual reasoning")
    topic: Optional[str] = Field("General", description="Syllabus topic e.g. Mechanics, Genetics")
    has_diagram: bool = Field(False, description="True if question refers to a figure or diagram")
    diagram_placeholder: Optional[str] = Field(None, description="Description of the figure required if has_diagram is True")
    difficulty: str = Field("standard", description="easy, standard, or hard")

class ParsedCBTPack(BaseModel):
    suggested_title: str
    subject: str
    exam_type: str
    exam_year: Optional[int] = None
    syllabus_topics: List[str] = Field(default_factory=list)
    questions: List[ParsedCBTQuestion] = Field(default_factory=list)


def parse_cbt_text_regex(raw_text: str, default_subject: str = "General", default_exam: str = "JAMB UTME", default_year: Optional[int] = None) -> ParsedCBTPack:
    """
    Deterministic regex fallback parser for structured past question text blocks.
    Supports 4 options (JAMB/UTME) or 5 options (WAEC/NECO/WASSCE):
      1. What is the speed of light?
      A. 3x10^8 m/s
      B. 2x10^8 m/s
      C. 1x10^8 m/s
      D. 4x10^8 m/s
      E. 5x10^8 m/s
      Answer: A
      Explanation: The speed of light in vacuum is approx 3 x 10^8 m/s.
    """
    lines = raw_text.splitlines()
    questions: List[ParsedCBTQuestion] = []
    current_q_text = []
    current_options = []
    current_answer_idx = 0
    current_expl = ""
    current_topic = default_subject
    current_has_diagram = False

    def _flush_question():
        nonlocal current_q_text, current_options, current_answer_idx, current_expl, current_topic, current_has_diagram
        if current_q_text and current_options:
            q_clean = " ".join(current_q_text).strip()
            # Remove leading number like "1.", "1)", "Q1:"
            q_clean = re.sub(r"^(?:Q\s*)?\d+[\.\)\:\-]\s*", "", q_clean).strip()
            
            # Check for diagram references
            has_diag = bool(re.search(r"(?i)\b(diagram|figure|illustration|shown below|in the circuit|graph|chart|circuit|table)\b", q_clean)) or current_has_diagram

            # Clean option prefixes (A., B), etc.) and support 4 or 5 options
            opts = [re.sub(r"^[A-Ea-e][\.\)\:\-]\s*", "", o).strip() for o in current_options]
            while len(opts) < 4:
                opts.append(f"Option {chr(65 + len(opts))}")
            opts = opts[:5]  # Support up to 5 options (A-E)

            questions.append(ParsedCBTQuestion(
                question_text=q_clean,
                options=opts,
                answer_index=min(current_answer_idx, len(opts) - 1),
                explanation=current_expl.strip(),
                topic=current_topic,
                has_diagram=has_diag,
                diagram_placeholder="Diagram referenced in question" if has_diag else None,
                difficulty="standard"
            ))
        current_q_text = []
        current_options = []
        current_answer_idx = 0
        current_expl = ""
        current_has_diagram = False

    for line in lines:
        sline = line.strip()
        if not sline:
            continue

        # Check for new question pattern: e.g. "1.", "1)", "Q1.", "Question 1:"
        q_start = re.match(r"^(?:Question\s+|\bQ\s*)?\d+[\.\)\:\-]\s+(.+)", sline, re.IGNORECASE)
        # Check for option pattern: e.g. "A.", "A)", "(A)", "[A]"
        opt_match = re.match(r"^(?:\(?([A-Ea-e])[\.\)\]\:\-]\s*)(.+)", sline)
        # Check for answer pattern: e.g. "Answer: A", "Ans: C", "Correct: B", "Correct Answer: D", "Key: A"
        ans_match = re.match(r"^(?:(?:Correct\s+)?(?:Answer|Ans|Option)|Key|Correct)\s*(?:is)?\s*[\:\=\-]?\s*\(?([A-Ea-e])\)?(?:\s|$)", sline, re.IGNORECASE)
        # Check for explanation pattern: e.g. "Explanation: ...", "Reason: ..."
        expl_match = re.match(r"^(?:Explanation|Expl|Reason|Working)\s*[\:\=\-]\s*(.+)", sline, re.IGNORECASE)
        # Check for topic tag: e.g. "Topic: Mechanics"
        topic_match = re.match(r"^(?:Topic|Section)\s*[\:\=\-]\s*(.+)", sline, re.IGNORECASE)

        if ans_match:
            letter = ans_match.group(1).upper()
            current_answer_idx = max(0, ord(letter) - ord("A"))
            continue

        if expl_match:
            current_expl += " " + expl_match.group(1).strip()
            continue

        if topic_match:
            current_topic = topic_match.group(1).strip()
            continue

        if opt_match:
            current_options.append(opt_match.group(2).strip())
            continue

        if q_start:
            if current_q_text and current_options:
                _flush_question()
            current_q_text.append(q_start.group(1).strip())
            continue

        # Continuation line
        if current_options:
            # option continuation
            current_options[-1] += " " + sline
        elif current_q_text:
            current_q_text.append(sline)
        else:
            current_q_text.append(sline)

    _flush_question()

    title_parts = [default_exam]
    if default_year:
        title_parts.append(str(default_year))
    title_parts.append(default_subject)
    title_parts.append("Past Questions CBT")

    topics = list({q.topic for q in questions if q.topic})
    return ParsedCBTPack(
        suggested_title=" ".join(title_parts),
        subject=default_subject,
        exam_type=default_exam,
        exam_year=default_year,
        syllabus_topics=topics or [default_subject],
        questions=questions
    )


async def parse_past_questions_with_ai(
    raw_text: str,
    subject: str = "General",
    exam_type: str = "JAMB UTME",
    exam_year: Optional[int] = None
) -> ParsedCBTPack:
    """
    Parses past questions using OpenAI / Gemini / AgentRouter structured outputs.
    Falls back to deterministic regex parser if API keys are missing or calls fail.
    """
    api_key = _get_api_key()
    gemini_key = _get_gemini_key()

    if not api_key and not gemini_key:
        return parse_cbt_text_regex(raw_text, default_subject=subject, default_exam=exam_type, default_year=exam_year)

    system_prompt = (
        "You are an expert West African Examinations (JAMB UTME, WAEC WASSCE, NECO) CBT ingestion specialist. "
        "Your task is to parse raw past exam papers and syllabus documents into a clean, structured CBT test pack.\n"
        "Guidelines:\n"
        "1. Remove leading question numbers (e.g. '1.', 'Q2:').\n"
        "2. Extract exactly 4 options for JAMB/standard exams or 5 options for WAEC/NECO exams: [Option A, B, C, D, (E)]. Remove letter prefixes (like 'A.').\n"
        "3. Determine the correct answer_index (0 for A, 1 for B, 2 for C, 3 for D, 4 for E).\n"
        "4. Provide step-by-step mathematical working or conceptual explanation for each answer.\n"
        "5. Assign a syllabus topic (e.g. 'Waves & Optics', 'Organic Chemistry', 'Calculus').\n"
        "6. Check if question refers to a diagram, graph, circuit, map, or figure. If so, set has_diagram=True and describe the diagram.\n"
        "7. Keep mathematical symbols, exponents, chemical formulas, and scientific units intact."
    )

    user_prompt = (
        f"Parse the following {exam_type} ({exam_year or 'Past Years'}) {subject} exam material into CBT questions:\n\n"
        f"{raw_text[:75000]}"
    )

    # 1. Try OpenAI if API key exists
    if api_key:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=api_key,
                timeout=120.0,
                max_retries=1
            )
            response = await client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format=ParsedCBTPack,
                temperature=0.2,
            )
            parsed = response.choices[0].message.parsed
            if parsed and parsed.questions:
                if not parsed.suggested_title:
                    parsed.suggested_title = f"{exam_type} {exam_year or ''} {subject} CBT Practice Pack".strip()
                return parsed
        except Exception as e:
            print(f"[CBT AI PARSER (OpenAI) ERROR]: {e}")

    # 2. Try Gemini via google.genai if key exists
    if gemini_key:
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[f"{system_prompt}\n\n{user_prompt}"],
                config={"response_mime_type": "application/json", "response_schema": ParsedCBTPack}
            )
            parsed = ParsedCBTPack.model_validate_json(resp.text)
            if parsed and parsed.questions:
                if not parsed.suggested_title:
                    parsed.suggested_title = f"{exam_type} {exam_year or ''} {subject} CBT Practice Pack".strip()
                return parsed
        except Exception as e:
            print(f"[CBT AI PARSER (Gemini) ERROR]: {e}")

    # 3. Deterministic regex fallback
    return parse_cbt_text_regex(raw_text, default_subject=subject, default_exam=exam_type, default_year=exam_year)
