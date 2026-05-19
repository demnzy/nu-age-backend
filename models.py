from datetime import datetime, timezone

from database import Base
import uuid
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum,Boolean,Float, ForeignKeyConstraint, UniqueConstraint,Date
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from schemas import Roles, Gender
from sqlalchemy.dialects.postgresql import ARRAY
#entities
import enum

# 1. Define the types of chats Nu-Age will support
class ChannelType(str, enum.Enum):
    COURSE = "course"             # Auto-created for a course cohort
    ORGANISATION = "organisation" # Org-wide general chat
    CUSTOM = "custom"             # Created by a teacher/admin (e.g., "Study Group A")
    DIRECT = "direct"             # 1-on-1 DM between users


class User(Base):
    __tablename__ = 'user'
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    first_name = Column(String, nullable=False )
    last_name = Column(String, nullable=False )
    gender = Column(Enum(Gender), nullable=False)
    email = Column(String, unique= True )
    password = Column(String, nullable= False)
    username = Column(String, nullable= False, unique= True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    number = Column(String, nullable= True)
    role = Column(Enum(Roles), nullable=False)
    university = Column(String, nullable=True)
    streak = Column(Integer, default=0, nullable=True)
    last_login_date = Column(Date, nullable=True)
    is_verified = Column(Boolean, default=False)
     
    organisations = relationship("Organisation", secondary="OrganisationMembers", back_populates="members")
    courses= relationship("Course", secondary= "enrollments", back_populates="Students")
    created_courses = relationship("Course", back_populates="admin", foreign_keys="[Course.admin_id]")
    teaches = relationship("Course", back_populates="teacher", foreign_keys="[Course.teacher_id]")
    device_tokens = relationship("DeviceToken", backref="user", cascade="all, delete")
class DeviceToken(Base):
    __tablename__ = "device_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    token = Column(String, unique=True, index=True, nullable=False)
    device_type = Column(String) # e.g., 'android', 'ios', 'desktop'
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SignupOTP(Base):
    __tablename__ = "signup_otps"
    email = Column(String, nullable=False, unique=True, primary_key=True)
    code = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PasswordResetOTP(Base):
    __tablename__ = "password_reset_otps"
    email = Column(String, nullable=False, unique=True, primary_key=True)
    code = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Organisation(Base):
    __tablename__ = "Organisations"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=False, unique=True)
    email = Column(String, unique=True, nullable=False)
    number = Column(String, nullable=False)
    website = Column(String, nullable=True)
    address = Column(String, nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"))
    logo = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), default="e8b15d94-8a43-4f11-9238-a5c2d6e7f8b9",nullable=True)
    plan_expires_at = Column(DateTime(timezone=True), nullable=True) # Null for lifetime/free plans
    theme_color = Column(String, nullable=True)
    # Relationships
    members = relationship("User", secondary="OrganisationMembers", back_populates="organisations")
    owner = relationship("User", foreign_keys=[owner_id], backref="owns")
    plan = relationship("Plan", back_populates="organisations", lazy="joined")
    courses = relationship("Course", back_populates="organisation")
    
class Plan(Base):
    __tablename__ = "plans"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=False, unique=True) # e.g., "Free", "Pro", "Enterprise"
    description = Column(String, nullable=True)
    price = Column(Float, nullable=False, default=0.0)
    
    max_members = Column(Integer, nullable=True)
    max_courses = Column(Integer, nullable=True)
    features = Column(ARRAY(String), nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    organisations = relationship("Organisation", back_populates="plan")
    
class OrganisationMember(Base):
    __tablename__ = "OrganisationMembers"
    user_id = Column(UUID(as_uuid=True), ForeignKey(User.id, ondelete="CASCADE"), primary_key=True)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("Organisations.id", ondelete="CASCADE"), primary_key=True)
    role  = Column(String, nullable=False, default="student")
#courses and categories

class Category(Base):
    __tablename__ = "categories"
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    name= Column(String, nullable=False, unique=True)
    description = Column(String, nullable=False)
    
    courses = relationship("Course", primaryjoin="Category.id==foreign(Course.category_id)", back_populates="category")

class Course(Base):
    __tablename__ = "courses"
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey(User.id, ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=False, default=name)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    category_id = Column(UUID(as_uuid=True), ForeignKey(Category.id))
    objectives = Column(ARRAY(String), nullable=True)
    public = Column(Boolean, default=False)
    org_id = Column(UUID(as_uuid=True), ForeignKey(Organisation.id, ondelete = "CASCADE"), nullable = True)
    image_url= Column(String, nullable=True)
    teacher_id = Column(UUID(as_uuid=True), ForeignKey(User.id, ondelete="SET NULL"), nullable=True)
    supervised = Column(Boolean, default=False)
    chat_id = Column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="SET NULL", use_alter=True,), nullable=True)
    
    category = relationship("Category", back_populates= "courses", lazy="joined")
    modules = relationship("Module", back_populates="course", order_by="Module.order_index")
    Students = relationship("User", secondary= "enrollments", back_populates="courses")
    admin = relationship("User", foreign_keys=[admin_id], back_populates="created_courses", lazy="joined")
    organisation = relationship("Organisation", back_populates="courses")
    teacher = relationship("User", foreign_keys=[teacher_id], back_populates="teaches")
     
class Enrollment(Base):
    __tablename__ = 'enrollments'
    student_id = Column(UUID(as_uuid=True),ForeignKey(User.id, ondelete = "CASCADE"), primary_key=True)
    course_id = Column(UUID(as_uuid=True), ForeignKey(Course.id, ondelete= "CASCADE"), nullable=False, primary_key=True)
    final_score= Column(Integer, default=0)
    certificate_url = Column(String, nullable=True)
    credential_id = Column(String, nullable=True)
    enrolled_at  = Column(DateTime(timezone=True), server_default=func.now())
    progress = Column(Float, nullable=False, server_default="0.0", default=0.0)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    course = relationship("Course",overlaps="Students,courses")
    student = relationship("User",overlaps="Students,courses")

    
#lessons and Modules
class Module(Base):
    __tablename__ = 'modules'
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    title = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    course_id = Column(UUID(as_uuid=True), ForeignKey(Course.id, ondelete = "CASCADE"), nullable = False)

    
    # UPDATE 1: Changed from String to Integer for proper sorting
    order_index = Column(Integer, nullable = False, default=0) 
    
    course = relationship("Course", back_populates="modules")
    lessons = relationship("Lesson", back_populates='modules', order_by="Lesson.order_index")
    
class Lesson(Base):
    __tablename__ = 'lessons'
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    title = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    module_id = Column(UUID(as_uuid=True), ForeignKey(Module.id, ondelete = "CASCADE"), nullable = False)
    
    order_index = Column(Integer, nullable = False)
    
    type = Column(String, nullable=False, default='text')
    
    content = Column(JSONB, nullable=True)
    
    
    modules = relationship("Module", back_populates="lessons")


class LessonProgress(Base):
    __tablename__ = 'lesson_progress'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # Notice we removed the individual ForeignKeys from these two columns
    student_id = Column(UUID(as_uuid=True), nullable=False)
    course_id = Column(UUID(as_uuid=True), nullable=False)
    
    # Lesson link stays the same
    lesson_id = Column(UUID(as_uuid=True), ForeignKey('lessons.id', ondelete="CASCADE"), nullable=False)
    
    completed_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # THE MAGIC HAPPENS HERE: 
    # This explicitly links student_id and course_id to the Enrollment table
    __table_args__ = (
        ForeignKeyConstraint(
            ['student_id', 'course_id'], 
            ['enrollments.student_id', 'enrollments.course_id'], 
            ondelete="CASCADE" # If the enrollment is deleted, this progress is deleted!
        ),
    )


# 2. The Channel Table (The "Room")
class Channel(Base):
    __tablename__ = 'channels'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=True) # DMs don't need names, group chats do
    type = Column(SQLEnum(ChannelType), nullable=False)
    
    # If it's a course chat, link it directly to the course!
    course_id = Column(UUID(as_uuid=True), ForeignKey('courses.id', ondelete="CASCADE"), nullable=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey('Organisations.id', ondelete="CASCADE"), nullable=True)
    
    # Requirements Check: Admin Privileges / Broadcasts
    is_announcement_only = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="SET NULL"), nullable=True)

    # Relationships
    members = relationship("ChannelMember", back_populates="channel", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="channel", cascade="all, delete-orphan")


# 3. The Junction Table (Who is in the Room?)
class ChannelMember(Base):
    __tablename__ = 'channel_members'
    
    channel_id = Column(UUID(as_uuid=True), ForeignKey('channels.id', ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="CASCADE"), primary_key=True)
    
    # Distinguish between an admin (can post in announcement chats) and a standard member
    role = Column(String, default="member", nullable=False) 
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    channel = relationship("Channel", back_populates="members")
    user = relationship("User") # Assuming your User table is named 'user'


# 4. The Message Table (The Payload)
class Message(Base):
    __tablename__ = 'messages'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    channel_id = Column(UUID(as_uuid=True), ForeignKey('channels.id', ondelete="CASCADE"), nullable=False)
    sender_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="CASCADE"), nullable=False)
    
    type = Column(String, default="text", nullable=False)
    
    # The actual text, or the CDN link if type == FILE
    content = Column(String, nullable=False) 
    
    # Requirements Check: Polls
    # Storing poll data as JSONB allows us to keep the schema clean without needing 3 more tables
    # Example payload: {"question": "Best day for test?", "options": {"A": "Monday", "B": "Friday"}}
    metadata_payload = Column(JSONB, nullable=True) 
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    channel = relationship("Channel", back_populates="messages")
    sender = relationship("User")

# --- SELF STUDY & AI ASSESSMENTS MODULE ---

class StudyMaterial(Base):
    __tablename__ = "study_materials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    
    title = Column(String, nullable=False)
    source_type = Column(String, nullable=False)
    content = Column(String, nullable=True) 
    file_url = Column(String, nullable=True) 
    
    # --- ADD THIS LOCKING FLAG ---
    is_generating = Column(Boolean, default=False, nullable=False) 
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", backref="study_materials")
    flashcards = relationship("Flashcard", back_populates="material", cascade="all, delete-orphan")
    questions = relationship("Question", back_populates="material", cascade="all, delete-orphan")

class Flashcard(Base):
    __tablename__ = "flashcards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    material_id = Column(UUID(as_uuid=True), ForeignKey("study_materials.id", ondelete="SET NULL"), nullable=True)
    
    front = Column(String, nullable=False)
    back = Column(String, nullable=False)
    
    # --- Spaced Repetition (SRS) Core Data (SM-2 Algorithm) ---
    repetitions = Column(Integer, default=0, nullable=False) 
    ease_factor = Column(Float, default=2.5, nullable=False) 
    interval_days = Column(Integer, default=0, nullable=False) 
    next_review_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    material = relationship("StudyMaterial", back_populates="flashcards")
    user = relationship("User", backref="flashcards")

class Question(Base):
    __tablename__ = "questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    material_id = Column(UUID(as_uuid=True), ForeignKey("study_materials.id", ondelete="CASCADE"), nullable=True)
    
    question_text = Column(String, nullable=False)
    options = Column(JSONB, nullable=False) # Stores the array of strings: ["A", "B", "C", "D"]
    answer_index = Column(Integer, nullable=False) # The integer index of the correct option
    explanation = Column(String, nullable=True)
    
    difficulty = Column(String, default="standard") 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    material = relationship("StudyMaterial", back_populates="questions")
    user = relationship("User", backref="study_questions")

class StudyPlan(Base):
        __tablename__ = "study_plans"

        # We use strings like "free", "pro", "unlimited" as the primary ID
        id = Column(String, primary_key=True, index=True) 
        label = Column(String, nullable=False)
        
        # Nullable because "unlimited" will have None
        materials_limit = Column(Integer, nullable=True)
        generations_limit = Column(Integer, nullable=True)

# 2. The updated Subscription table
class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False)
    
    # Foreign Key pointing to the StudyPlan table
    plan_id = Column(String, ForeignKey("study_plans.id"), default="free", nullable=False) 
    
    materials_uploaded = Column(Integer, default=0, nullable=False)
    generations_used = Column(Integer, default=0, nullable=False)
    cycle_start_date = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", backref="subscription")
    plan = relationship("StudyPlan") # Allows us to access sub.plan.materials_limit

class Connection(Base):
    __tablename__ = "connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # Who sent it
    requester_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    # Who received it
    addressee_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    
    # "pending", "accepted", "declined"
    status = Column(String, default="pending", nullable=False) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Prevent someone from spamming multiple requests to the same person
    __table_args__ = (
        UniqueConstraint('requester_id', 'addressee_id', name='_requester_addressee_uc'),
    )

    # Relationships to easily fetch the User objects
    requester = relationship("User", foreign_keys=[requester_id])
    addressee = relationship("User", foreign_keys=[addressee_id])