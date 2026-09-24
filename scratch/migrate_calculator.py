import os
import sys
sys.path.insert(0, os.getcwd())

import database
from sqlalchemy import text

def run():
    print("Connecting to database...")
    with database.engine.connect() as conn:
        print("Running migrations on cohort_exams...")
        conn.execute(text("ALTER TABLE cohort_exams ADD COLUMN IF NOT EXISTS calculator_type VARCHAR DEFAULT 'none';"))
        conn.execute(text("ALTER TABLE cohort_exams ADD COLUMN IF NOT EXISTS show_immediate_results BOOLEAN DEFAULT FALSE;"))
        conn.commit()
    print("Migrations completed successfully!")

if __name__ == "__main__":
    run()
