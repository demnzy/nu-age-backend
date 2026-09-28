import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import engine
from sqlalchemy import text

def run_migration():
    with engine.connect() as conn:
        print("Applying migration...")
        conn.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR;'))
        conn.commit()
        res = conn.execute(text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'user' AND column_name = 'profile_picture_url';")).fetchall()
        print("Migration Result:", res)

if __name__ == "__main__":
    run_migration()
