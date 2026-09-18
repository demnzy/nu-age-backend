from datetime import datetime, timezone
import random
from database import Base
import uuid
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum, Boolean, Float, ForeignKeyConstraint, UniqueConstraint, Date, Index, Text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from schemas import Roles, Gender
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql import false
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
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
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
    owner_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), index=True)
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
    cohorts = relationship("Cohort", back_populates="organisation", cascade="all, delete-orphan")
    
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
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("Organisations.id", ondelete="CASCADE"), primary_key=True, index=True)
    role  = Column(String, nullable=False, default="student")

    __table_args__ = (
        Index("ix_org_members_org_role", "organisation_id", "role"),
    )

class Invitations(Base):
    __tablename__ = "invitations"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    target_email = Column(String, nullable=True)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("Organisations.id", ondelete="CASCADE"), nullable=False, index=True)
    uses_left = Column(Integer, default=1, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    role = Column(String,nullable=False, server_default = "student")

    __table_args__ = (
        Index("ix_invitations_org_email", "organisation_id", "target_email"),
        Index("ix_invitations_org_expires", "organisation_id", "expires_at"),
    )
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
    admin_id = Column(UUID(as_uuid=True), ForeignKey(User.id, ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=False, default=name)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    category_id = Column(UUID(as_uuid=True), ForeignKey(Category.id), index=True)
    objectives = Column(ARRAY(String), nullable=True)
    public = Column(String, default="false")
    org_id = Column(UUID(as_uuid=True), ForeignKey(Organisation.id, ondelete = "CASCADE"), nullable = True, index=True)
    image_url= Column(String, nullable=True)
    teacher_id = Column(UUID(as_uuid=True), ForeignKey(User.id, ondelete="SET NULL"), nullable=True, index=True)
    supervised = Column(Boolean, default=False)
    chat_id = Column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="SET NULL", use_alter=True,), nullable=True)
    is_freelance = Column(Boolean, nullable=False, server_default=false())
    auto_certificate = Column(Boolean, nullable=False, server_default="true")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    rating = Column(Float, default=lambda: random.uniform(4.5, 5.0))
    rating_count = Column(Integer, default=1, server_default="1")
    
    category = relationship("Category", back_populates= "courses", lazy="joined")
    modules = relationship("Module", back_populates="course", order_by="Module.order_index")
    Students = relationship("User", secondary= "enrollments", back_populates="courses")
    admin = relationship("User", foreign_keys=[admin_id], back_populates="created_courses", lazy="joined")
    organisation = relationship("Organisation", back_populates="courses")
    teacher = relationship("User", foreign_keys=[teacher_id], back_populates="teaches")

    @property
    def total_modules(self) -> int:
        if hasattr(self, "_total_modules") and self._total_modules is not None:
            return self._total_modules
        try:
            return len(self.modules) if self.modules else 0
        except Exception:
            return 0

    @total_modules.setter
    def total_modules(self, value: int):
        self._total_modules = value
     
class Enrollment(Base):
    __tablename__ = 'enrollments'
    student_id = Column(UUID(as_uuid=True),ForeignKey(User.id, ondelete = "CASCADE"), primary_key=True)
    course_id = Column(UUID(as_uuid=True), ForeignKey(Course.id, ondelete= "CASCADE"), nullable=False, primary_key=True, index=True)
    final_score= Column(Integer, default=0)
    certificate_url = Column(String, nullable=True)
    credential_id = Column(String, nullable=True)
    enrolled_at  = Column(DateTime(timezone=True), server_default=func.now())
    progress = Column(Float, nullable=False, server_default="0.0", default=0.0)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    user_rating = Column(Float, nullable=True)
    course = relationship("Course",overlaps="Students,courses")
    student = relationship("User",overlaps="Students,courses")

    __table_args__ = (
        Index("ix_enrollments_course_completed", "course_id", "completed_at"),
    )
    
#lessons and Modules
class Module(Base):
    __tablename__ = 'modules'
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    title = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    course_id = Column(UUID(as_uuid=True), ForeignKey(Course.id, ondelete = "CASCADE"), nullable = False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    
    # UPDATE 1: Changed from String to Integer for proper sorting
    order_index = Column(Integer, nullable = False, default=0) 
    
    course = relationship("Course", back_populates="modules")
    lessons = relationship("Lesson", back_populates='modules', order_by="Lesson.order_index")
    
class Lesson(Base):
    __tablename__ = 'lessons'
    id = Column(UUID(as_uuid=True), primary_key = True, default=uuid.uuid4, index=True)
    title = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    module_id = Column(UUID(as_uuid=True), ForeignKey(Module.id, ondelete = "CASCADE"), nullable = False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
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
        Index("ix_lesson_progress_student_course", "student_id", "course_id"),
        Index("ix_lesson_progress_student_lesson", "student_id", "lesson_id"),
        Index("ix_lesson_progress_course_id", "course_id"),
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
    
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
    created_by_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="SET NULL"), nullable=True)

    # Relationships
    members = relationship("ChannelMember", back_populates="channel", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="channel", cascade="all, delete-orphan")


# 3. The Junction Table (Who is in the Room?)
class ChannelMember(Base):
    __tablename__ = 'channel_members'
    
    channel_id = Column(UUID(as_uuid=True), ForeignKey('channels.id', ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="CASCADE"), primary_key=True, index=True)
    
    # Distinguish between an admin (can post in announcement chats) and a standard member
    role = Column(String, default="member", nullable=False) 
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    last_read_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    channel = relationship("Channel", back_populates="members")
    user = relationship("User") # Assuming your User table is named 'user'


# 4. The Message Table (The Payload)
class Message(Base):
    __tablename__ = 'messages'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    channel_id = Column(UUID(as_uuid=True), ForeignKey('channels.id', ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="CASCADE"), nullable=False)
    
    type = Column(String, default="text", nullable=False)
    
    # The actual text, or the CDN link if type == FILE
    content = Column(String, nullable=False) 
    
    # Requirements Check: Polls
    # Storing poll data as JSONB allows us to keep the schema clean without needing 3 more tables
    # Example payload: {"question": "Best day for test?", "options": {"A": "Monday", "B": "Friday"}}
    metadata_payload = Column(JSONB, nullable=True) 
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_messages_channel_created_at", "channel_id", "created_at"),
    )

    # Relationships
    channel = relationship("Channel", back_populates="messages")
    sender = relationship("User")

# --- SELF STUDY & AI ASSESSMENTS MODULE ---

class StudyMaterial(Base):
    __tablename__ = "study_materials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    
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
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    material_id = Column(UUID(as_uuid=True), ForeignKey("study_materials.id", ondelete="SET NULL"), nullable=True, index=True)
    
    front = Column(String, nullable=False)
    back = Column(String, nullable=False)
    
    # --- Spaced Repetition (SRS) Core Data (SM-2 Algorithm) ---
    repetitions = Column(Integer, default=0, nullable=False) 
    ease_factor = Column(Float, default=2.5, nullable=False) 
    interval_days = Column(Integer, default=0, nullable=False) 
    next_review_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_flashcards_user_next_review", "user_id", "next_review_date"),
    )

    # Relationships
    material = relationship("StudyMaterial", back_populates="flashcards")
    user = relationship("User", backref="flashcards")

class Question(Base):
    __tablename__ = "questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    material_id = Column(UUID(as_uuid=True), ForeignKey("study_materials.id", ondelete="CASCADE"), nullable=True, index=True)
    
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
        Index('ix_connections_addressee_status', 'addressee_id', 'status'),
        Index('ix_connections_requester_status', 'requester_id', 'status'),
    )

    # Relationships to easily fetch the User objects
    requester = relationship("User", foreign_keys=[requester_id])
    addressee = relationship("User", foreign_keys=[addressee_id])
class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
from sqlalchemy import Column, String, DateTime, Enum as SAEnum, JSON, Text
class CourseDraftJob(Base):
    __tablename__ = "course_draft_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)
    topic = Column(String, nullable=False)
    context = Column(Text, nullable=True)

    status = Column(SAEnum(JobStatus), default=JobStatus.PENDING, nullable=False)
    result = Column(JSON, nullable=True)     # populated on SUCCESS
    error = Column(Text, nullable=True)      # populated on FAILED, safe-to-show message

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

# Add this alongside your existing models

class Playlist(Base):
    __tablename__ = 'playlists'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    
    
    # Links to the existing 'user' table
    creator_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="CASCADE"), nullable=False, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey(Organisation.id, ondelete = "CASCADE"), nullable = True, index=True)
    image_url= Column(String, nullable=True)
    rating = Column(Float, default=0.0, nullable=False)
    is_public = Column(Boolean, default=True, nullable=False)
    
    # Matches your existing timestamp pattern
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    creator = relationship("User", backref="playlists")
    playlist_courses = relationship("PlaylistCourse", back_populates="playlist", order_by="PlaylistCourse.order_index", cascade="all, delete-orphan")


class PlaylistCourse(Base):
    __tablename__ = 'playlist_courses'
    
    # Links the specific playlist and course together
    playlist_id = Column(UUID(as_uuid=True), ForeignKey('playlists.id', ondelete="CASCADE"), primary_key=True)
    course_id = Column(UUID(as_uuid=True), ForeignKey('courses.id', ondelete="CASCADE"), primary_key=True, index=True)
    
    # Mirrors the sorting logic you used in your Module and Lesson tables
    order_index = Column(Integer, nullable=False, default=0) 
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    playlist = relationship("Playlist", back_populates="playlist_courses")
    course = relationship("Course") # Allows you to easily query the full course data via the playlist

class PlaylistEnrollment(Base):
    __tablename__ = 'playlist_enrollments'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    playlist_id = Column(UUID(as_uuid=True), ForeignKey('playlists.id', ondelete="CASCADE"), nullable=False, index=True)
    student_id = Column(UUID(as_uuid=True), ForeignKey('user.id', ondelete="CASCADE"), nullable=False, index=True)
    
    enrolled_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    progress = Column(Float, default=0.0) # 0 to 100
    
    __table_args__ = (
        Index("ix_playlist_enrollments_student_playlist", "student_id", "playlist_id"),
    )

    # Relationships
    playlist = relationship("Playlist", backref="enrollments")
    student = relationship("User", backref="playlist_enrollments")

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
 
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=False, index=True)
 
    # The opaque token string itself. Store a HASH of it, not the raw value —
    # same principle as passwords: if your DB leaks, raw refresh tokens in
    # plaintext are instant account takeover for every user. Hash with sha256
    # (fast hash is fine here — these are already high-entropy random tokens,
    # unlike passwords, so we're not defending against brute force, just DB leaks).
    token_hash = Column(String, unique=True, nullable=False, index=True)
 
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)
 
    # Set when this token has been used to mint a new one (rotation) or
    # explicitly revoked (logout). A non-null revoked_at means "dead, don't honor."
    revoked_at = Column(DateTime(timezone=True), nullable=True)
 
    # Optional but recommended: track device/user-agent so users can see
    # "active sessions" and revoke individual ones later.
    device_label = Column(String, nullable=True)
 
    user = relationship("models.User", backref="refresh_tokens")


# =========================================================================
# COHORTS, TRAININGS & SCHEDULED EXAMS
# =========================================================================

class Cohort(Base):
    __tablename__ = "cohorts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("Organisations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, nullable=False, default="upcoming")  # upcoming, active, completed, archived
    banner_url = Column(String, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    organisation = relationship("Organisation", back_populates="cohorts")
    creator = relationship("User", foreign_keys=[created_by])
    members = relationship("CohortMember", back_populates="cohort", cascade="all, delete-orphan")
    courses = relationship("CohortCourse", back_populates="cohort", cascade="all, delete-orphan", order_by="CohortCourse.order_index")
    exams = relationship("CohortExam", back_populates="cohort", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_cohorts_org_status", "organisation_id", "status"),
        Index("ix_cohorts_dates", "organisation_id", "start_date", "end_date"),
    )


class CohortMember(Base):
    __tablename__ = "cohort_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    cohort_id = Column(UUID(as_uuid=True), ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, nullable=False, default="enrolled")  # enrolled, active, completed, dropped
    enrolled_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    cohort = relationship("Cohort", back_populates="members")
    user = relationship("User", backref="cohort_memberships")

    __table_args__ = (
        UniqueConstraint("cohort_id", "user_id", name="uq_cohort_member"),
        Index("ix_cohort_members_user", "user_id", "cohort_id"),
    )


class CohortCourse(Base):
    __tablename__ = "cohort_courses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    cohort_id = Column(UUID(as_uuid=True), ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    cohort = relationship("Cohort", back_populates="courses")
    course = relationship("Course")

    __table_args__ = (
        UniqueConstraint("cohort_id", "course_id", name="uq_cohort_course"),
        Index("ix_cohort_courses_cohort", "cohort_id", "order_index"),
    )


class CohortExam(Base):
    __tablename__ = "cohort_exams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    cohort_id = Column(UUID(as_uuid=True), ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    instructions = Column(Text, nullable=True)
    opens_at = Column(DateTime(timezone=True), nullable=False)
    closes_at = Column(DateTime(timezone=True), nullable=False)
    duration_minutes = Column(Integer, nullable=False, default=60)
    pass_percentage = Column(Float, nullable=False, default=70.0)
    max_attempts = Column(Integer, nullable=False, default=1)
    shuffle_questions = Column(Boolean, nullable=False, default=True)
    show_immediate_results = Column(Boolean, nullable=False, default=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    cohort = relationship("Cohort", back_populates="exams")
    creator = relationship("User", foreign_keys=[created_by])
    questions = relationship("CohortExamQuestion", back_populates="exam", cascade="all, delete-orphan", order_by="CohortExamQuestion.order_index")
    submissions = relationship("CohortExamSubmission", back_populates="exam", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_cohort_exams_cohort_window", "cohort_id", "opens_at", "closes_at"),
    )


class CohortExamQuestion(Base):
    __tablename__ = "cohort_exam_questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    exam_id = Column(UUID(as_uuid=True), ForeignKey("cohort_exams.id", ondelete="CASCADE"), nullable=False, index=True)
    question_text = Column(Text, nullable=False)
    options = Column(JSONB, nullable=False)  # List of strings e.g. ["Option A", "Option B", "Option C", "Option D"]
    correct_index = Column(Integer, nullable=False, default=0)  # 0-indexed correct option
    explanation = Column(Text, nullable=True)
    points = Column(Float, nullable=False, default=1.0)
    order_index = Column(Integer, nullable=False, default=0)

    # Relationships
    exam = relationship("CohortExam", back_populates="questions")


class CohortExamSubmission(Base):
    __tablename__ = "cohort_exam_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    exam_id = Column(UUID(as_uuid=True), ForeignKey("cohort_exams.id", ondelete="CASCADE"), nullable=False, index=True)
    cohort_id = Column(UUID(as_uuid=True), ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False, default=1)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, default=0)
    score = Column(Float, default=0.0)
    max_score = Column(Float, default=0.0)
    percentage = Column(Float, default=0.0)
    passed = Column(Boolean, default=False)
    answers = Column(JSONB, nullable=True)  # List of {question_id, chosen_index, correct_index, is_correct, points}
    status = Column(String, default="in_progress")  # in_progress, submitted, timed_out, graded

    # Relationships
    exam = relationship("CohortExam", back_populates="submissions")
    user = relationship("User", backref="cohort_exam_submissions")

    __table_args__ = (
        Index("ix_cohort_exam_sub_user_exam", "exam_id", "user_id"),
        Index("ix_cohort_exam_sub_cohort", "cohort_id", "user_id"),
    )