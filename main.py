import asyncio
import logging
import os
import re
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import *
from models import Base
from routers import enrollments, media, users,courses,categories, organisations, curriculum,chat,certificate,study,subscriptions,network, playlists, cohorts, platform_admin, notifications, discussions, payments, study_marketplace, study_partners, objectives, tts, promo
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
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS image_url VARCHAR;",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS topic VARCHAR(150);",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS exam_type VARCHAR(50);",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS exam_year INTEGER;",
    """
    CREATE TABLE IF NOT EXISTS study_bundles (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        title VARCHAR(255) NOT NULL,
        description TEXT,
        category VARCHAR(100) DEFAULT 'General' NOT NULL,
        theme_gradient VARCHAR(100) DEFAULT 'purple_indigo' NOT NULL,
        banner_url VARCHAR,
        price_coins INTEGER DEFAULT 0 NOT NULL,
        discount_percentage INTEGER DEFAULT 0 NOT NULL,
        is_official BOOLEAN DEFAULT TRUE NOT NULL,
        status VARCHAR(50) DEFAULT 'approved' NOT NULL,
        downloads_count INTEGER DEFAULT 0 NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS study_bundle_items (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        bundle_id UUID NOT NULL REFERENCES study_bundles(id) ON DELETE CASCADE,
        pack_id UUID NOT NULL REFERENCES study_packs(id) ON DELETE CASCADE,
        order_index INTEGER DEFAULT 0 NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_bundle_items_bundle ON study_bundle_items(bundle_id);",
    """
    CREATE TABLE IF NOT EXISTS study_partnerships (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_a_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        user_b_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        status VARCHAR(30) DEFAULT 'pending' NOT NULL,
        goal_type VARCHAR(50) DEFAULT 'cbt_exam' NOT NULL,
        target_exam VARCHAR(100) DEFAULT 'General CBT',
        target_subjects JSONB DEFAULT '[]'::jsonb NOT NULL,
        daily_target_minutes INTEGER DEFAULT 30 NOT NULL,
        daily_target_questions INTEGER DEFAULT 15 NOT NULL,
        duo_streak INTEGER DEFAULT 0 NOT NULL,
        best_duo_streak INTEGER DEFAULT 0 NOT NULL,
        last_duo_streak_date DATE,
        user_a_last_study_date DATE,
        user_b_last_study_date DATE,
        user_a_today_minutes INTEGER DEFAULT 0 NOT NULL,
        user_b_today_minutes INTEGER DEFAULT 0 NOT NULL,
        user_a_today_questions INTEGER DEFAULT 0 NOT NULL,
        user_b_today_questions INTEGER DEFAULT 0 NOT NULL,
        last_nudge_at TIMESTAMPTZ,
        last_nudge_by_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_study_partnerships_users ON study_partnerships(user_a_id, user_b_id);",
    "CREATE INDEX IF NOT EXISTS ix_study_partnerships_status ON study_partnerships(status);",
    """
    CREATE TABLE IF NOT EXISTS partner_challenges (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        partnership_id UUID NOT NULL REFERENCES study_partnerships(id) ON DELETE CASCADE,
        challenger_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        challenged_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        challenge_type VARCHAR(50) DEFAULT 'cbt_past_questions' NOT NULL,
        title VARCHAR(255) NOT NULL,
        subject VARCHAR(100) DEFAULT 'General' NOT NULL,
        question_snapshot JSONB DEFAULT '[]'::jsonb NOT NULL,
        total_questions INTEGER DEFAULT 10 NOT NULL,
        duration_seconds INTEGER DEFAULT 600 NOT NULL,
        status VARCHAR(30) DEFAULT 'challenger_done' NOT NULL,
        challenger_score INTEGER,
        challenger_time_seconds INTEGER,
        challenger_breakdown JSONB DEFAULT '{}'::jsonb,
        challenger_completed_at TIMESTAMPTZ,
        challenged_score INTEGER,
        challenged_time_seconds INTEGER,
        challenged_breakdown JSONB DEFAULT '{}'::jsonb,
        challenged_completed_at TIMESTAMPTZ,
        winner_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        coins_awarded INTEGER DEFAULT 0 NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_partner_challenges_partnership ON partner_challenges(partnership_id);",
    "CREATE INDEX IF NOT EXISTS ix_partner_challenges_status ON partner_challenges(status);",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS coins_balance INTEGER DEFAULT 0;",
    "ALTER TABLE partner_challenges ADD COLUMN IF NOT EXISTS pack_id UUID REFERENCES study_packs(id) ON DELETE SET NULL;",
    "ALTER TABLE partner_challenges ADD COLUMN IF NOT EXISTS wager_amount INTEGER DEFAULT 0;",
    "ALTER TABLE partner_challenges ADD COLUMN IF NOT EXISTS is_escrowed BOOLEAN DEFAULT FALSE;",
    """
    CREATE TABLE IF NOT EXISTS partnership_shared_packs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        partnership_id UUID NOT NULL REFERENCES study_partnerships(id) ON DELETE CASCADE,
        pack_id UUID NOT NULL REFERENCES study_packs(id) ON DELETE CASCADE,
        sender_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        recipient_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        status VARCHAR(30) DEFAULT 'pending' NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        accepted_at TIMESTAMPTZ
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_shared_packs_partnership ON partnership_shared_packs(partnership_id);",
    "CREATE INDEX IF NOT EXISTS ix_shared_packs_recipient ON partnership_shared_packs(recipient_id, status);",
    "UPDATE study_packs SET status = 'partner_only' WHERE category = 'Duo Study' AND status = 'approved';",
    "UPDATE study_partnerships SET target_exam = NULL WHERE target_exam IN ('JAMB UTME', 'JAMB', 'UTME', 'General CBT', 'General');",
    "UPDATE study_bundles SET category = 'General' WHERE category = 'JAMB UTME';",
    """
    CREATE TABLE IF NOT EXISTS coin_transactions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        amount INTEGER NOT NULL,
        category VARCHAR(50) NOT NULL,
        source_type VARCHAR(50),
        reference_id VARCHAR(100),
        description VARCHAR(255),
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_coin_trans_user ON coin_transactions(user_id, created_at DESC);",
    """
    CREATE TABLE IF NOT EXISTS platform_objectives (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        code VARCHAR(50) UNIQUE NOT NULL,
        title VARCHAR(100) NOT NULL,
        description VARCHAR(255) NOT NULL,
        category VARCHAR(50) DEFAULT 'general' NOT NULL,
        target_value INTEGER DEFAULT 1 NOT NULL,
        target_unit VARCHAR(30) DEFAULT 'count' NOT NULL,
        coin_reward INTEGER DEFAULT 50 NOT NULL,
        icon VARCHAR(50) DEFAULT 'EMOJI_TROPHY' NOT NULL,
        is_active BOOLEAN DEFAULT TRUE NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS user_objective_progress (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        objective_code VARCHAR(50) NOT NULL,
        current_value INTEGER DEFAULT 0 NOT NULL,
        target_value INTEGER DEFAULT 1 NOT NULL,
        is_completed BOOLEAN DEFAULT FALSE NOT NULL,
        completed_at TIMESTAMPTZ,
        is_claimed BOOLEAN DEFAULT FALSE NOT NULL,
        claimed_at TIMESTAMPTZ,
        CONSTRAINT uq_user_objective UNIQUE (user_id, objective_code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_user_obj_user ON user_objective_progress(user_id);",
    """
    ALTER TABLE platform_objectives ALTER COLUMN id SET DEFAULT gen_random_uuid();
    """,
    """
    ALTER TABLE platform_objectives ALTER COLUMN is_active SET DEFAULT TRUE;
    """,
    """
    ALTER TABLE platform_objectives ADD COLUMN IF NOT EXISTS metric_event VARCHAR(50) DEFAULT 'general';
    """,
    "CREATE INDEX IF NOT EXISTS ix_platform_obj_metric ON platform_objectives(metric_event);",
    """
    INSERT INTO platform_objectives (id, code, title, description, category, metric_event, target_value, target_unit, coin_reward, icon, is_active)
    VALUES
        (gen_random_uuid(), 'finish_course', 'Course Finisher', 'Complete all lessons and exams in any course', 'courses', 'finish_course', 1, 'courses', 150, 'SCHOOL', true),
        (gen_random_uuid(), 'streak_5_day', '5-Day Streak Master', 'Maintain a study streak for 5 consecutive days', 'streaks', 'login_streak', 5, 'days', 100, 'LOCAL_FIRE_DEPARTMENT', true),
        (gen_random_uuid(), 'study_time_60m', 'Deep Focus Hour', 'Log 60 minutes of active study in Self-Study Hub in one day', 'self_study', 'study_time', 60, 'minutes', 40, 'TIMER', true),
        (gen_random_uuid(), 'practice_100q', 'Question Grinder', 'Answer 100 practice questions across CBT or Flashcards', 'self_study', 'practice_questions', 100, 'questions', 50, 'QUIZ', true),
        (gen_random_uuid(), 'win_duel', 'Duel Champion', 'Win a 15-question Quickfire Duel against your study partner', 'duels', 'win_duel', 1, 'duels', 35, 'MILITARY_TECH', true),
        (gen_random_uuid(), 'share_pack', 'Knowledge Partner', 'Share a Study Pack with your study partner', 'sharing', 'share_pack', 1, 'packs', 20, 'SHARE', true)
    ON CONFLICT (code) DO UPDATE SET
        metric_event = EXCLUDED.metric_event;
    """,
    """
    CREATE TABLE IF NOT EXISTS lesson_audio_cache (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        lesson_id UUID NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
        language VARCHAR(10) NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        audio_url VARCHAR NOT NULL,
        duration_seconds FLOAT,
        file_size_bytes INTEGER,
        format VARCHAR(10) DEFAULT 'mp3',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ,
        CONSTRAINT uq_lesson_audio_lang_hash UNIQUE (lesson_id, language, content_hash)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_lesson_audio_lookup ON lesson_audio_cache(lesson_id, language);",
    "ALTER TABLE platform_objectives ADD COLUMN IF NOT EXISTS badge_reward_slug VARCHAR(50);",
    """
    CREATE TABLE IF NOT EXISTS promo_codes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        code VARCHAR(50) UNIQUE NOT NULL,
        title VARCHAR(100) NOT NULL,
        description VARCHAR(255),
        reward_coins INTEGER DEFAULT 0 NOT NULL,
        reward_ai_credits INTEGER DEFAULT 0 NOT NULL,
        reward_trial_days INTEGER DEFAULT 0 NOT NULL,
        reward_badge_slug VARCHAR(50),
        reward_pack_ids JSONB DEFAULT '[]'::jsonb,
        ambassador_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        ambassador_coin_reward INTEGER DEFAULT 0 NOT NULL,
        max_uses INTEGER,
        uses_count INTEGER DEFAULT 0 NOT NULL,
        expires_at TIMESTAMPTZ,
        is_active BOOLEAN DEFAULT TRUE NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_promo_codes_code ON promo_codes(code);",
    "CREATE INDEX IF NOT EXISTS ix_promo_codes_active ON promo_codes(is_active);",
    """
    CREATE TABLE IF NOT EXISTS promo_code_redemptions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        promo_code_id UUID NOT NULL REFERENCES promo_codes(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        redeemed_at TIMESTAMPTZ DEFAULT NOW(),
        rewards_granted JSONB DEFAULT '{}'::jsonb NOT NULL,
        CONSTRAINT uq_user_promo_redemption UNIQUE (promo_code_id, user_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_promo_redemptions_user ON promo_code_redemptions(user_id);",
    """
    CREATE TABLE IF NOT EXISTS member_badges (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        slug VARCHAR(50) UNIQUE NOT NULL,
        name VARCHAR(100) NOT NULL,
        description VARCHAR(255),
        icon VARCHAR(50) DEFAULT 'STAR_ROUNDED' NOT NULL,
        color_hex VARCHAR(20) DEFAULT '#4CAF50' NOT NULL,
        gradient_start VARCHAR(20) DEFAULT '#2E7D32',
        gradient_end VARCHAR(20) DEFAULT '#1B5E20',
        is_automated BOOLEAN DEFAULT FALSE NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_member_badges_slug ON member_badges(slug);",
    """
    CREATE TABLE IF NOT EXISTS user_badges (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        badge_id UUID NOT NULL REFERENCES member_badges(id) ON DELETE CASCADE,
        is_primary BOOLEAN DEFAULT FALSE NOT NULL,
        awarded_at TIMESTAMPTZ DEFAULT NOW(),
        CONSTRAINT uq_user_badge UNIQUE (user_id, badge_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_user_badges_user ON user_badges(user_id);",
    """
    CREATE TABLE IF NOT EXISTS campus_ambassadors (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        email VARCHAR NOT NULL UNIQUE,
        university VARCHAR NOT NULL,
        status VARCHAR(30) DEFAULT 'invited' NOT NULL,
        invite_token VARCHAR(64) UNIQUE NOT NULL,
        assigned_promo_code_id UUID REFERENCES promo_codes(id) ON DELETE SET NULL,
        invited_at TIMESTAMPTZ DEFAULT NOW(),
        accepted_at TIMESTAMPTZ,
        notes VARCHAR(255)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_campus_ambassadors_email ON campus_ambassadors(email);",
    "CREATE INDEX IF NOT EXISTS ix_campus_ambassadors_status ON campus_ambassadors(status);",
    "CREATE INDEX IF NOT EXISTS ix_campus_ambassadors_uni ON campus_ambassadors(university);",
    "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS scoped_university VARCHAR(150);",
    "CREATE INDEX IF NOT EXISTS ix_promo_codes_scoped_uni ON promo_codes(scoped_university);",
    """
    CREATE TABLE IF NOT EXISTS campus_ambassador_applications (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        full_name VARCHAR(150) NOT NULL,
        email VARCHAR NOT NULL,
        university VARCHAR NOT NULL,
        department VARCHAR(150) NOT NULL,
        level VARCHAR(50) NOT NULL,
        phone VARCHAR(50),
        notes TEXT,
        status VARCHAR(30) DEFAULT 'pending' NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ,
        reviewed_by UUID REFERENCES "user"(id) ON DELETE SET NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_ambassador_apps_email ON campus_ambassador_applications(email);",
    "CREATE INDEX IF NOT EXISTS ix_ambassador_apps_status ON campus_ambassador_applications(status);",
    "CREATE INDEX IF NOT EXISTS ix_ambassador_apps_uni ON campus_ambassador_applications(university);",
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
app.include_router(study_partners.router, tags=["Study Partnerships"])
app.include_router(objectives.router, tags=["Objectives & Nu-Coins"])
app.include_router(tts.router, tags=["Lesson TTS Audio"])
app.include_router(promo.router, tags=["Promo Codes & Badges"])

# Add this right after you declare: app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nu-age.name.ng",
        "https://www.nu-age.name.ng",
        "https://nu-age.com.ng",
        "https://www.nu-age.com.ng",
        "https://learn.nu-age.name.ng",
        "https://learn.nu-age.com.ng",
        "https://nu-web.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5500",
        "http://localhost:8000",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5500",
        "http://127.0.0.1:8000",
    ],
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)(:\d+)?|https://(.*\.)?nu-age\.(name|com)\.ng|https://.*\.vercel\.app)$",
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


async def _reengagement_worker():
    """
    Autonomous background worker daemon:
    Wakes up periodically (every 60 minutes) to:
    1. Scan subscriptions for 3-day renewal notices, grace periods, and hard expiration suspensions.
    2. Evaluate active learners and duos, dispatching Duolingo-style streak warnings, duo nudges,
       objective quest nudges, and comeback alerts.
    """
    await asyncio.sleep(15)  # Grace period after server startup
    while True:
        try:
            from database import SessionLocal
            from services.subscription_service import check_subscription_renewals
            from services.reengagement_engine import evaluate_and_dispatch_nudges

            with SessionLocal() as db:
                # 1. Autonomous Subscription Expiration & Renewal Warnings
                try:
                    sub_summary = check_subscription_renewals(db=db)
                    if any(sub_summary.get(k, 0) > 0 for k in ("reminders_3day_sent", "grace_warnings_sent", "suspensions_sent")):
                        print(f"[AUTONOMOUS-WORKER] Subscriptions updated: {sub_summary}")
                except Exception as sub_err:
                    print(f"[AUTONOMOUS-WORKER] Subscription renewal evaluation warning: {sub_err}")

                # 2. Autonomous Duolingo Re-engagement Engine (Streaks, Duos, Quests, Focus)
                try:
                    stats = evaluate_and_dispatch_nudges(db=db, force=False)
                    dispatched = stats.get("dispatched_records", [])
                    if dispatched:
                        print(f"[AUTONOMOUS-WORKER] Dispatched {len(dispatched)} automated nudges at {stats.get('timestamp_wat')}")
                except Exception as nudge_err:
                    print(f"[AUTONOMOUS-WORKER] Re-engagement engine warning: {nudge_err}")

        except Exception as e:
            print(f"[AUTONOMOUS-WORKER] Background loop encountered error: {e}")
        await asyncio.sleep(3600)


@app.on_event("startup")
async def start_reengagement_worker():
    asyncio.create_task(_reengagement_worker())