import os
import sys
sys.path.insert(0, os.getcwd())

import database
import models

def test_query():
    db = database.SessionLocal()
    try:
        exams = db.query(models.CohortExam).limit(5).all()
        print(f"Query succeeded! Found {len(exams)} exams.")
        for ex in exams:
            print(f"- Exam '{ex.title}': calculator_type='{ex.calculator_type}', show_immediate_results={ex.show_immediate_results}")
    finally:
        db.close()

if __name__ == "__main__":
    test_query()
