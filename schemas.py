from pydantic import BaseModel, field_serializer, EmailStr, Field
from typing import Optional, List
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

class CourseBase(BaseModel):
    name: str
    description: str
    category_id: str
    objectives: Optional[List[str]] = None
    public: bool = False
    org_id: Optional[str] = None
    teacher_id: Optional[str] = None
    supervised: bool = False
    
    # --- ADD THESE FOR BUNNY.NET ---
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
    public: bool | None= None
    

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
    public: Optional[bool] = None
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
    course: Optional[str] = None
    org: Optional[str] = None
    # avatar: Optional[str] = None  <-- Removed
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