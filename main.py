import asyncio
import logging
import os
import re
import sys
import time
from fastapi import *
from routers import enrollments, media, users,courses,categories, organisations, curriculum,chat,certificate,study,subscriptions,network, playlists, cohorts, platform_admin, notifications, discussions, payments, study_marketplace
from models import Base
from sqlalchemy.orm import configure_mappers
configure_mappers()
from database import engine

# ─────────────────────────────────────────────────────────────────────────
# STARTUP: logging + database migrations
# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _startup_log(msg: str):
    # print + flush: always shows up in Coolify, whatever the logging config is
    print(f"[STARTUP] {msg}", flush=True)


def _restore_logging():
    """alembic/env.py calls logging.config.fileConfig(), which by default DISABLES
    every logger that already exists (uvicorn's, email_service's, ...) and resets
    the root level. That is what made the container go silent. Undo it."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for _lg in list(logging.root.manager.loggerDict.values()):
        if isinstance(_lg, logging.Logger):
            _lg.disabled = False


_t0 = time.monotonic()
try:
    Base.metadata.create_all(bind=engine)
    _startup_log(f"create_all OK ({time.monotonic() - _t0:.1f}s)")
except Exception as ex:
    _startup_log(f"create_all FAILED (app will still start, /health will show DB status): {ex!r}")


def _run_alembic_migrations():
    _t = time.monotonic()
    # PGOPTIONS makes alembic's own connections give up on a lock after 5s instead of
    # waiting forever (a silent hang). It is removed again right after the run.
    _old_pgoptions = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = "-c lock_timeout=5000 -c statement_timeout=60000"
    try:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
        _startup_log(f"alembic upgrade head OK ({time.monotonic() - _t:.1f}s)")
    except Exception as ex:
        _startup_log(f"alembic upgrade FAILED (app will still start): {ex!r}")
    finally:
        if _old_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = _old_pgoptions
        _restore_logging()


_run_alembic_migrations()

# Auto-heal missing columns/tables on existing databases.
#
# Why this is written the way it is: "ALTER TABLE ... ADD COLUMN IF NOT EXISTS"
# takes an ACCESS EXCLUSIVE lock EVEN WHEN THE COLUMN ALREADY EXISTS, and the old
# version ran all of them in one transaction. Under real traffic (or while the old
# container is still serving during a deploy) those locks queue up, every query on
# the table stalls behind them, and the app looks dead. So now each statement:
#   * is skipped entirely, with no lock taken, if its column/table/index exists
#   * runs in its own short transaction
#   * gives up after 3s (lock_timeout) and is retried on the next start
from sqlalchemy import text

MIGRATION_STATEMENTS = [
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
    """
    CREATE TABLE IF NOT EXISTS study_packs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        creator_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        title VARCHAR(255) NOT NULL,
        description TEXT,
        category VARCHAR(100) DEFAULT 'General' NOT NULL,
        theme_gradient VARCHAR(100) DEFAULT 'purple_indigo' NOT NULL,
        cover_image_url VARCHAR,
        price_coins INTEGER DEFAULT 0 NOT NULL,
        is_official BOOLEAN DEFAULT FALSE NOT NULL,
        status VARCHAR(50) DEFAULT 'pending_review' NOT NULL,
        admin_review_notes TEXT,
        reward_coins_granted INTEGER DEFAULT 0 NOT NULL,
        downloads_count INTEGER DEFAULT 0 NOT NULL,
        likes_count INTEGER DEFAULT 0 NOT NULL,
        pack_data JSONB DEFAULT '{}'::jsonb NOT NULL,
        source_material_id UUID REFERENCES study_materials(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        approved_at TIMESTAMPTZ
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_study_packs_status_cat ON study_packs(status, category);",
    "CREATE INDEX IF NOT EXISTS ix_study_packs_creator ON study_packs(creator_id);",
    """
    CREATE TABLE IF NOT EXISTS study_pack_downloads (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        pack_id UUID NOT NULL REFERENCES study_packs(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        coins_spent INTEGER DEFAULT 0 NOT NULL,
        imported_material_id UUID REFERENCES study_materials(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        CONSTRAINT uq_study_pack_user_download UNIQUE (pack_id, user_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_study_pack_downloads_user ON study_pack_downloads(user_id);",
    """
    CREATE TABLE IF NOT EXISTS study_pack_likes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        pack_id UUID NOT NULL REFERENCES study_packs(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        CONSTRAINT uq_study_pack_user_like UNIQUE (pack_id, user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS credit_balances (
        user_id UUID PRIMARY KEY REFERENCES "user"(id) ON DELETE CASCADE,
        balance INTEGER DEFAULT 100 NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS credit_ledger_entries (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        delta INTEGER NOT NULL,
        reason VARCHAR(100) NOT NULL,
        reference_id VARCHAR(255),
        balance_after INTEGER NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    INSERT INTO credit_balances (user_id, balance, updated_at)
    SELECT id, 100, NOW() FROM "user"
    ON CONFLICT (user_id) DO NOTHING;
    """,
    # REMOVED: "UPDATE credit_balances SET balance = 100 WHERE balance = 0;"
    # It ran on EVERY startup, so anyone who had spent all their credits got
    # reset to 100 on each deploy/restart. Run it once by hand if you need it.
]

_ALTER_ADD_COL = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)
_CREATE_TABLE = re.compile(r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)
_CREATE_INDEX = re.compile(r"^\s*CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)


def _already_applied(conn, stmt: str) -> bool:
    m = _ALTER_ADD_COL.match(stmt)
    if m:
        return conn.execute(
            text("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                 "AND table_name = :t AND column_name = :c"),
            {"t": m.group(1), "c": m.group(2)},
        ).first() is not None
    m = _CREATE_TABLE.match(stmt)
    if m:
        return conn.execute(text("SELECT to_regclass(:n)"), {"n": m.group(1)}).scalar() is not None
    m = _CREATE_INDEX.match(stmt)
    if m:
        return conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :i"),
            {"i": m.group(1)},
        ).first() is not None
    return False


def _run_migrations():
    _t = time.monotonic()
    applied = skipped = failed = 0
    for stmt in MIGRATION_STATEMENTS:
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text("SET LOCAL statement_timeout = '30s'"))
                if _already_applied(conn, stmt):
                    skipped += 1
                    continue
                conn.execute(text(stmt))
                applied += 1
        except Exception as e:
            failed += 1
            first_line = (str(e).strip().splitlines() or [""])[0][:160]
            _startup_log(f"auto-migration skipped (retries next start): {' '.join(stmt.split())[:80]} -> {first_line}")
    _startup_log(f"auto-migrations: {applied} applied, {skipped} already present, {failed} failed ({time.monotonic() - _t:.1f}s)")


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

# Friendly 422 messages (services/validation_handler.py). Wrapped so a missing or
# broken file can never stop the app from starting.
try:
    from services.validation_handler import register_validation_handler
    register_validation_handler(app)
    _startup_log("validation handler registered")
except Exception as ex:
    _startup_log(f"validation handler NOT registered (app still starts): {ex!r}")

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
app.include_router(study_marketplace.router, tags=["Study Marketplace & Hub"])

# Add this right after you declare: app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nu-age.name.ng",
        "https://www.nu-age.name.ng",
        "https://learn.nu-age.name.ng",
        "https://learn.nu-age.com.ng",
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