import asyncio
import sys
from fastapi import *
from routers import enrollments, media, users,courses,categories, organisations, curriculum,chat,certificate,study,subscriptions,network, playlists, cohorts, platform_admin, notifications, discussions, payments
from models import Base
from database import engine
Base.metadata.create_all(bind=engine)

# Auto-heal missing columns on existing tables
def _run_migrations():
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            migration_statements = [
                "ALTER TABLE cohort_exams ADD COLUMN IF NOT EXISTS security_mode VARCHAR DEFAULT 'monitored';",
                "ALTER TABLE cohort_exams ADD COLUMN IF NOT EXISTS max_violations INTEGER DEFAULT 2;",
                "ALTER TABLE cohort_exams ADD COLUMN IF NOT EXISTS calculator_type VARCHAR DEFAULT 'none';",
                "ALTER TABLE cohort_exams ADD COLUMN IF NOT EXISTS show_immediate_results BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE cohort_exam_submissions ADD COLUMN IF NOT EXISTS violations_count INTEGER DEFAULT 0;",
                "ALTER TABLE cohort_exam_submissions ADD COLUMN IF NOT EXISTS violation_log JSONB DEFAULT '[]'::jsonb;",
                "ALTER TABLE cohort_exam_submissions ADD COLUMN IF NOT EXISTS session_token VARCHAR;",
                "ALTER TABLE cohort_exam_questions ADD COLUMN IF NOT EXISTS scenario_text TEXT;",
                """
                CREATE TABLE IF NOT EXISTS user_notifications (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    sender_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
                    title VARCHAR NOT NULL,
                    body TEXT NOT NULL,
                    category VARCHAR DEFAULT 'general',
                    action_route VARCHAR,
                    data_payload JSONB DEFAULT '{}'::jsonb,
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """,
                "CREATE INDEX IF NOT EXISTS ix_user_notif_user_created ON user_notifications(user_id, created_at DESC);",
                "CREATE INDEX IF NOT EXISTS ix_user_notif_unread ON user_notifications(user_id, is_read);",
                """
                CREATE TABLE IF NOT EXISTS course_discussions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    course_id UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    module_id UUID REFERENCES modules(id) ON DELETE SET NULL,
                    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    category VARCHAR(50) DEFAULT 'question',
                    upvotes_count INTEGER DEFAULT 0,
                    replies_count INTEGER DEFAULT 0,
                    is_pinned BOOLEAN DEFAULT FALSE,
                    is_resolved BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """,
                "ALTER TABLE course_discussions ADD COLUMN IF NOT EXISTS module_id UUID REFERENCES modules(id) ON DELETE SET NULL;",
                "CREATE INDEX IF NOT EXISTS ix_course_disc_course_created ON course_discussions(course_id, created_at DESC);",
                "CREATE INDEX IF NOT EXISTS ix_course_disc_module ON course_discussions(module_id);",
                "CREATE INDEX IF NOT EXISTS ix_course_disc_category ON course_discussions(course_id, category);",
                """
                CREATE TABLE IF NOT EXISTS course_discussion_replies (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    discussion_id UUID NOT NULL REFERENCES course_discussions(id) ON DELETE CASCADE,
                    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    upvotes_count INTEGER DEFAULT 0,
                    is_endorsed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """,
                "CREATE INDEX IF NOT EXISTS ix_course_reply_disc_created ON course_discussion_replies(discussion_id, created_at ASC);",
                """
                CREATE TABLE IF NOT EXISTS course_discussion_upvotes (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    discussion_id UUID REFERENCES course_discussions(id) ON DELETE CASCADE,
                    reply_id UUID REFERENCES course_discussion_replies(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT uq_user_discussion_upvote UNIQUE (user_id, discussion_id),
                    CONSTRAINT uq_user_reply_upvote UNIQUE (user_id, reply_id)
                );
                """,
            ]
            for stmt in migration_statements:
                conn.execute(text(stmt))
            conn.commit()
    except Exception as e:
        print(f"Auto-migration notice: {e}")

_run_migrations()

from fastapi.middleware.cors import CORSMiddleware

tags_metadata = [
    {
        "name": "Users",
        "description": "Operations involving **login**, registration, and profile management.",
    },
    {
        "name": "Courses",
        "description": "Create and manage courses. **Admin only**.",
    },
    {
        "name": "Curriculum Management",
        "description": "Manage course curricula. **Admin only**.",
    },
    {
        "name": "Certificates",
        "description": "Generate and manage certificates.",
    },
    {
        "name": "Subscription Management",
        "description": "Manage user subscriptions and plans.",
    }

]
app = FastAPI(openapi_tags=tags_metadata)
app.include_router(users.router, tags=["Users"])
app.include_router(courses.router, tags=["Courses"])
app.include_router(categories.router, tags=["Categories"])
app.include_router(enrollments.router,tags=["enrollments"])
app.include_router(organisations.router,tags=["organisations"])
app.include_router(cohorts.router, tags=["Cohorts & Trainings"])
app.include_router(cohorts.learner_router, tags=["Learner Cohorts"])
app.include_router(media.router, tags=["Media Handling"])
app.include_router(curriculum.router, tags=["Curriculum Management"])
app.include_router(playlists.router, tags=["Playlists"])
app.include_router(chat.router, tags=["Chat"])
app.include_router(certificate.router, tags=["Certificates"])
app.include_router(study.router, tags=["Self Study"])
app.include_router(subscriptions.router, tags=["Subscription Management"])
app.include_router(network.router, tags=["Friends Management"])
app.include_router(platform_admin.router, tags=["Platform Super Admin"])
app.include_router(notifications.router, tags=["Notifications"])
app.include_router(discussions.router, tags=["Course Discussions"])
app.include_router(payments.router, tags=["Payments Foundation"])

# Add this right after you declare: app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nu-age.name.ng",
        "https://www.nu-age.name.ng",
        "https://learn.nu-age.name.ng",
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_origin_regex=r"^https://.*\.nu-age\.name\.ng$",
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)
 

import sys
from fastapi.routing import APIRoute
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import get_db # Ensure this matches your import path

@app.get("/health", tags=["System"])
async def health_check(db: Session = Depends(get_db)):
    """
    A lightweight endpoint to keep the server awake and verify database connectivity.
    """
    try:
        # The ultimate lightweight query: ask PostgreSQL to literally just return the number 1
        db.execute(text("SELECT 1"))
        
        return {
            "status": "active",
            "backend": "online",
            "database": "connected"
        }
        
    except Exception as e:
        # If the database is unreachable, we fail loudly with a 503 Service Unavailable
        print(f"Health Check Failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend is running, but database connection failed."
        )
    
@app.on_event("startup")
def print_routes():
    print("\n" + "="*50)
    print("  AVAILABLE ROUTES (Use these URLs!)")
    print("="*50)
    found_any = False
    for route in app.routes:
        if isinstance(route, APIRoute):
            found_any = True
            print(f"METHOD: {route.methods}  |  PATH: {route.path}")
    
    if not found_any:
        print(">> NO ROUTES FOUND! check app.include_router() lines.")
    print("="*50 + "\n")