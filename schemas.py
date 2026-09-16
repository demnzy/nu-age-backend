from __future__ import annotations
from pydantic import BaseModel, field_serializer, EmailStr, Field, field_validator
from typing import Optional, List, Union, Literal, Annotated
from enum import Enum
from uuid import UUID
from datetime import datetime


class Roles(str,Enum):
    STUDENT = "Student"
    TEACHER = "Teacher"
    ADMIN = 'Admin'

class Gender(str, Enum):
    MALE = "Male" 
    FEMALE = "Female" 
    CUSTOM = "Rather not say"
    
class Organisation(BaseModel):
    id: UUID
    name : str
    email: EmailStr
    number: int
    address: str
    
class UserBase(BaseModel):
    id: UUID
    email: EmailStr
    username : str
    password : str
    first_name : str
    last_name: str
    gender: str
    role: str
    streak: Optional[int] = 0
    university: Optional[str] = ""
    model_config = {'from_attributes' : True}

class UserProfile(BaseModel):
    id: Optional[UUID] = None
    email: EmailStr
    username : str
    first_name : str
    last_name: str
    gender: str
    role: str
    streak: Optional[int] = 0
    university: Optional[str] = ""
    created_at: Optional[datetime] = None
    is_verified: Optional[bool] = False
    active_count: Optional[int] = 0
    finished_count: Optional[int] = 0
    model_config = {'from_attributes' : True}
    
class UserReg(BaseModel):
    email: EmailStr
    username : str
    password : str
    first_name : str
    last_name: str
    gender: str
    role: str
    university: Optional[str] = None
    model_config = {'from_attributes' : True}

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'Bearer'
    model_config = {'from_attributes' : True}

class LoginUser(BaseModel):
    email: Optional[EmailStr] 
    username: str

    password: str
    
class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    gender: Optional[str] = None

class CourseBase(BaseModel):
    name: str
    description: str
    category_id: str
    objectives: Optional[List[str]] = None
    public: str = "false"
    org_id: Optional[str] = None
    teacher_id: Optional[str] = None
    supervised: bool = False
    is_freelance: bool = False   
    auto_certificate: bool = True
    
    @field_validator('public', mode='before')
    @classmethod
    def coerce_public_to_string(cls, v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    
    # --- ADD THESE FOR BUSNNY.NET ---
    image_bytes: Optional[str] = None
    image_filename: Optional[str] = None
    
class CategoryBase(BaseModel):
    name: str
    description: str

class CategoryOut(BaseModel):
    id: UUID
    name: str
    description: str
    
class CategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
class Description(BaseModel):
    description: str

class Name(BaseModel):
    name: str
    
class EnrollmentBase(BaseModel):
    student_id: UUID | None = None
    course_id: UUID | None = None

class CourseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    public: str | None= None
    

# schemas.py

class UserMin(BaseModel):
    id: UUID
    first_name: str
    last_name: str

    class Config:
        from_attributes = True

class CatMin(BaseModel):
    id: UUID
    name: str

    class Config:
        from_attributes = True
        
class CourseOut(BaseModel):
    id: UUID
    name: str
    category: CatMin
    created_at: datetime # This stays a datetime object internally

    @field_serializer('created_at')
    def serialize_dt(self, dt: datetime, _info):
        # Format: Day/Month/Year (e.g., 24/03/2026)
        return dt.strftime('%d/%m/%Y')
    progress: Optional[float] = 0.0
    image_url: Optional[str] = None
    admin: UserMin 
    objectives: List[str] |  None = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    public: Optional[str] = None
    organisation: Optional['orgbase'] = None
    auto_certificate: bool
    total_modules: Optional[int] = 0
    total_students: Optional[int] = 0

    class Config:
        from_attributes = True
        
class orgbase(BaseModel):
    name: str
    email :str
    number : str
    website: Optional[str] = None
    address : str 
    logo_bytes: Optional[bytes] = None
    logo_filename: Optional[str] = None
    theme_color: Optional[str] = None
    model_config = {'from_attributes': True}

class CourseSettings(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    supervised: Optional[bool] = None
    teacher_id: Optional[UUID] | str = None
    auto_certificate: Optional[bool] = None
    public: Optional[str] = None
    category_id: Optional[UUID] = None

# --- Flashcard Schemas ---
class SRSState(BaseModel):
    interval: int
    ease_factor: float
    repetitions: int

class FlashcardResponse(BaseModel):
    id: UUID
    front: str
    back: str
    material_id: Optional[UUID] = None
    srs_state: SRSState
    
    class Config:
        from_attributes = True

class ReviewPayload(BaseModel):
    card_id: UUID
    quality: int = Field(..., ge=0, le=5, description="Quality rating: 0 (Blackout) to 5 (Perfect)")

class ReviewResponse(BaseModel):
    next_review_date: datetime
    interval_days: int

class SaveFlashcardPayload(BaseModel):
    front: str
    back: str
    source_material_id: Optional[UUID] = None

# --- Material Schemas ---
class MaterialResponse(BaseModel):
    id: UUID
    title: str
    source_type: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class UploadResponse(BaseModel):
    material_id: UUID
    message: str

# --- AI & Assessment Schemas ---
class GeneratePayload(BaseModel):
    material_ids: List[UUID]
    types: List[str] # Expects ["flashcards", "quiz", "exam"]

class GenerateResponse(BaseModel):
    cards: List[FlashcardResponse]
    quiz_count: int
    exam_count: int

class QuestionResponse(BaseModel):
    id: UUID
    question: str
    options: List[str]
    answer: int
    explanation: Optional[str] = None
    
    class Config:
        from_attributes = True
class ExamResponse(BaseModel):
    questions: List[QuestionResponse]
    duration_seconds: int
    
class PlanConfigItem(BaseModel):
    label: str
    materials_limit: Optional[int]
    generations_limit: Optional[int]

class SubscriptionStatusResponse(BaseModel):
    plan_id: str
    label: str
    
    materials_used: int
    materials_limit: Optional[int]
    materials_remaining: Optional[int]
    
    generations_used: int
    generations_limit: Optional[int]
    generations_remaining: Optional[int]

class NetworkUserResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    university: Optional[str] = None
    org: Optional[str] = None
    online: bool = False
    streak: int = 0
    
    class Config:
        from_attributes = True # Allows Pydantic to read SQLAlchemy objects directly

class ConnectionRequestResponse(BaseModel):
    id: UUID
    user: NetworkUserResponse
    created_at: datetime
    
    class Config:
        from_attributes = True
class EnrollmentStatsResponse(BaseModel):
    course_id: UUID
    course_title: str
    enrolled_at: datetime
    completed_at: datetime
    time_spent_seconds: int
    time_spent_formatted: str  # e.g., "12h 30m"
    certificate_download_url: Optional[str] = None
    auto_certificate: bool
    
    # Leaderboard / Gamification stats
    leaderboard_rank: int
    total_completers: int
    faster_than_percentile: float # e.g., 85.5 (meaning faster than 85.5% of people)

    class Config:
        from_attributes = True


class VerifyEmailSchema(BaseModel):
    email: str
    code: str

class InviteCreateRequest(BaseModel):
    target_email: EmailStr
    organisation_id: UUID
    role: str = "Student" # Default to student

class JoinProcessRequest(BaseModel):
    token: UUID

from typing import Annotated, List, Literal, Union

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Base Sub-components
# ---------------------------------------------------------------------------

class ScenarioChoice(BaseModel):
    text: str = Field(description="The option the user can select (e.g. 'Restart the server').")
    consequence: str = Field(description="The outcome of this choice, explaining why it was right or wrong.")

class AssessmentOption(BaseModel):
    text: str
    is_correct: bool

class AssessmentQuestion(BaseModel):
    text: str
    options: List[AssessmentOption] = Field(
        description="2–4 options; at least one must be marked correct."
    )

    @model_validator(mode="after")
    def must_have_correct_answer(self) -> "AssessmentQuestion":
        if self.options and not any(opt.is_correct for opt in self.options):
            self.options[0].is_correct = True
        return self

class StepperStep(BaseModel):
    tag: str = Field(description="Short phase/milestone badge, e.g., 'Step 1', 'Setup', 'Phase A'")
    headline: str = Field(description="Concise action or phase title")
    content: str = Field(description="Clear, substantive explanation of this step")
    takeaway: str = Field(description="Key insight or common pitfall to remember for this step")

class SequencerItem(BaseModel):
    id: str = Field(description="Unique short identifier, e.g. 'step_1', 'item_a'")
    label: str = Field(description="Action, milestone, or event text to be arranged")
    correct_order: int = Field(description="1-based integer index of correct sequence order (1, 2, 3...)")
    explanation: str = Field(description="Why this step belongs at this position in the sequence")

class CodeLabTestCase(BaseModel):
    description: str = Field(description="What this test case checks, e.g. 'Handles empty list'")
    input: str = Field(description="Arguments or input data for the test case")
    expected_output: str = Field(description="Expected standard output or return value")

# ---------------------------------------------------------------------------
# Unified Content Schema for OpenAI Structured Outputs
# ---------------------------------------------------------------------------

class AILessonContent(BaseModel):
    # The AI must populate ALL these fields, but we instruct it to leave unused ones empty based on the lesson type.
    text: str = Field(
        description="Rich Markdown text for 'text' lesson OR active recall passage with [[blank]] / [[blank|hint]] tokens for 'cloze'. Empty string if other type."
    )
    cards: List[str] = Field(
        description="List of standalone atomic facts/definitions. ONLY populate if type is 'cards', otherwise empty list."
    )
    scenario: str = Field(
        description="Dilemma setup with context, numbers, or constraints. ONLY populate if type is 'scenario', otherwise empty string."
    )
    choices: List[ScenarioChoice] = Field(
        description="3-4 decision choices with consequences. ONLY populate if type is 'scenario', otherwise empty list."
    )
    questions: List[AssessmentQuestion] = Field(
        description="Multiple-choice questions with answer options. ONLY populate if type is 'assessment', otherwise empty list."
    )
    title: str = Field(
        description="Walkthrough title. ONLY populate if type is 'stepper', otherwise empty string."
    )
    intro: str = Field(
        description="Introductory context. ONLY populate if type is 'stepper', otherwise empty string."
    )
    steps: List[StepperStep] = Field(
        description="Sequential walkthrough steps. ONLY populate if type is 'stepper', otherwise empty list."
    )
    prompt: str = Field(
        description="Sequencing challenge prompt. ONLY populate if type is 'sequencer', otherwise empty string."
    )
    items: List[SequencerItem] = Field(
        description="Items to arrange in order. ONLY populate if type is 'sequencer', otherwise empty list."
    )
    distractors: List[str] = Field(
        description="Plausible distractor words for cloze blanks. ONLY populate if type is 'cloze', otherwise empty list."
    )
    explanation: str = Field(
        description="Pedagogical explanation for cloze blanks. ONLY populate if type is 'cloze', otherwise empty string."
    )
    language: str = Field(
        description="Programming language ('python' or 'sql'). ONLY populate if type is 'code_lab', otherwise empty string."
    )
    instructions: str = Field(
        description="Task instructions and problem statement. ONLY populate if type is 'code_lab', otherwise empty string."
    )
    starter_code: str = Field(
        description="Initial code template or boilerplate. ONLY populate if type is 'code_lab', otherwise empty string."
    )
    solution_code: str = Field(
        description="Full reference solution. ONLY populate if type is 'code_lab', otherwise empty string."
    )
    setup_sql: str = Field(
        description="SQLite table schema & seed data (for SQL labs). ONLY populate if type is 'code_lab' and language is 'sql', otherwise empty string."
    )
    test_cases: List[CodeLabTestCase] = Field(
        description="Test cases to validate student solution. ONLY populate if type is 'code_lab', otherwise empty list."
    )
    video_url: str = Field(
        default="",
        description="YouTube video search query or URL (e.g. 'YOUTUBE: brief video search query' or 'https://www.youtube.com/watch?v=...'). ONLY populate if type is 'video', otherwise empty string."
    )
    accompanying_text: str = Field(
        default="",
        description="Educational notes or summary accompanying the video. ONLY populate if type is 'video', otherwise empty string."
    )

class AILesson(BaseModel):
    title: str
    type: Literal[
        "text",
        "cards",
        "scenario",
        "assessment",
        "stepper",
        "sequencer",
        "cloze",
        "code_lab",
        "video",
    ] = Field(description="The format of the lesson.")
    content: AILessonContent

    @model_validator(mode="after")
    def validate_content_matches_type(self) -> "AILesson":
        t = self.type
        c = self.content
        
        if t == "text":
            if not c.text.strip():
                c.text = f"# {self.title}\n\nCore instructional content and principles."
            
        elif t == "cards":
            if len(c.cards) < 1 or not any(str(x).strip() for x in c.cards):
                c.cards = [f"Core principle: {self.title}"]
            
        elif t == "scenario":
            if not c.scenario.strip():
                c.scenario = f"Scenario challenge regarding {self.title}."
            if len(c.choices) < 2:
                c.choices = [
                    ScenarioChoice(text="Analyze constraints first", consequence="Correct approach ensuring all prerequisites are met."),
                    ScenarioChoice(text="Execute immediately without validation", consequence="Flawed approach leading to unexpected failures.")
                ]
                
        elif t == "assessment":
            if len(c.questions) < 1:
                c.questions = [
                    AssessmentQuestion(
                        text=f"What is the key principle behind {self.title}?",
                        options=[
                            AssessmentOption(text="The primary operational standard", is_correct=True),
                            AssessmentOption(text="A deprecated legacy method", is_correct=False),
                        ]
                    )
                ]

        elif t == "stepper":
            if len(c.steps) < 1:
                c.steps = [
                    StepperStep(
                        tag="Step 1",
                        headline="Overview",
                        content=f"Fundamental walkthrough for {self.title}.",
                        takeaway="Understand the initial requirements."
                    )
                ]

        elif t == "sequencer":
            if not c.prompt.strip():
                c.prompt = f"Arrange the following steps for {self.title} in the correct order:"
            if len(c.items) < 2:
                c.items = [
                    SequencerItem(id="step_1", label="Phase 1: Setup and preparation", correct_order=1, explanation="Preparation comes first."),
                    SequencerItem(id="step_2", label="Phase 2: Core processing", correct_order=2, explanation="Processing follows setup."),
                    SequencerItem(id="step_3", label="Phase 3: Validation", correct_order=3, explanation="Validation confirms accuracy.")
                ]

        elif t == "cloze":
            if not c.text.strip():
                c.text = f"In {self.title}, the foundational concept is [[active recall]]."
            elif "[[" not in c.text or "]]" not in c.text:
                c.text = f"{c.text}\n\nKey takeaway: [[{self.title}]]."

        elif t == "code_lab":
            if not c.instructions.strip():
                c.instructions = f"Implement the required functionality for {self.title}."
            if not c.language.strip():
                c.language = "python"
            if not c.starter_code.strip():
                if "sql" in c.language.lower():
                    c.starter_code = "-- Write your SQL query below\nSELECT * FROM table_name;\n"
                else:
                    c.starter_code = "# Write your solution below\ndef solution():\n    pass\n"
            if not c.solution_code.strip():
                if "sql" in c.language.lower():
                    c.solution_code = "-- Reference query\nSELECT * FROM table_name WHERE id IS NOT NULL;\n"
                else:
                    c.solution_code = "# Reference solution\ndef solution():\n    return True\n"

        elif t == "video":
            if not c.video_url.strip():
                c.video_url = f"YOUTUBE: {self.title} short tutorial"
            if not c.accompanying_text.strip():
                c.accompanying_text = f"Key video walkthrough and explanations for {self.title}."
            
        return self

# ---------------------------------------------------------------------------
# Two-Stage Generation Schemas (Curriculum Blueprint & Module Content)
# ---------------------------------------------------------------------------

class AILessonBlueprint(BaseModel):
    title: str = Field(description="Title of the lesson")
    type: Literal[
        "text",
        "cards",
        "scenario",
        "assessment",
        "stepper",
        "sequencer",
        "cloze",
        "code_lab",
        "video",
    ] = Field(description="Pedagogical format for this lesson")
    pedagogical_goal: str = Field(
        description="Specific learning outcome or concept to teach/test in this lesson"
    )

class AIModuleBlueprint(BaseModel):
    title: str = Field(description="Module title")
    pedagogical_focus: str = Field(description="Core theme and educational purpose of this module")
    lessons: List[AILessonBlueprint] = Field(
        min_length=1,
        description="Ordered list of lesson blueprints in this module"
    )

class AICourseBlueprint(BaseModel):
    course_name: str
    description: str
    objectives: List[str] = Field(
        min_length=1,
        description="At least one high-level course learning objective."
    )
    modules: List[AIModuleBlueprint] = Field(min_length=1)

class AIModuleContent(BaseModel):
    title: str
    lessons: List[AILesson] = Field(min_length=1)

# ---------------------------------------------------------------------------
# Top-level draft models
# ---------------------------------------------------------------------------

class AIModule(BaseModel):
    title: str
    lessons: List[AILesson] = Field(min_length=1)

class AICourseDraft(BaseModel):
    course_name: str
    description: str
    objectives: List[str] = Field(
        min_length=1,
        description="At least one learning objective is required.",
    )
    modules: List[AIModule] = Field(min_length=1)

class MemberResponse(BaseModel):
    id: UUID
    email: EmailStr
    username : str
    password : str
    first_name : str
    last_name: str
    gender: str
    role: str
    streak: Optional[int] = 0
    university: Optional[str] = ""
    model_config = {'from_attributes' : True}


class OrgDraftRequest(BaseModel):
    org_id: UUID
    topic: str
    context: str | None = None

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    type: str

class PlaylistBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = True
    image_bytes: Optional[str] = None
    image_filename: Optional[str] = None

class PlaylistCreate(PlaylistBase):
    org_id: Optional[UUID] = None

class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None
    image_bytes: Optional[str] = None
    image_filename: Optional[str] = None

class PlaylistCourseOut(BaseModel):
    playlist_id: UUID
    course_id: UUID
    order_index: int
    added_at: datetime
    course: CourseOut

    class Config:
        from_attributes = True

class PlaylistOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    rating: float
    is_public: bool
    creator_id: UUID
    org_id: Optional[UUID] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    Organisation: Optional[str] = None
    
    playlist_courses: List[PlaylistCourseOut] = []

    class Config:
        from_attributes = True

class PlaylistEnrollmentOut(BaseModel):
    id: UUID
    playlist_id: UUID
    student_id: UUID
    enrolled_at: datetime
    completed_at: Optional[datetime] = None
    progress: float

    class Config:
        from_attributes = True