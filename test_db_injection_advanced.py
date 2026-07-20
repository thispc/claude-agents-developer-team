#!/usr/bin/env python3
"""
Advanced SQL injection test for conductor/app/db.py

This demonstrates more sophisticated injection techniques that can bypass
basic error detection but should be caught by proper identifier validation.
"""

import sqlite3
import tempfile
import os
import sys

# Adjust Python path to import from conductor
sys.path.insert(0, 'conductor')

# Create a temporary database for testing
test_db_file = tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False)
test_db_path = test_db_file.name
test_db_file.close()

print(f"Using temporary test database: {test_db_path}")

# Mock the config module
import conductor.app.config as config
config.DB_PATH = test_db_path
config.ANTHROPIC_API_KEY = ""

try:
    from conductor.app import db

    db.init()
    project_id = db.create_project(
        name="Test", brief="Test", repo="https://github.com/test/repo",
        budget_usd=10.0, max_workers=1
    )
    task_id = db.create_task(project_id, "tester", "Test", "Test task")

    print("\n=== Testing SQL Injection Vectors ===\n")

    # Test 1: Column name injection (less obvious)
    print("Test 1: Column rename/alias injection")
    print("  Field: 'status AS admin_flag_1' or 'status) ; UPDATE tasks SET'")
    injection_fields = [
        'status AS foo',
        'status) ; UPDATE tasks SET status=',
        'status\'; UPDATE tasks SET status=\'',
        'status` or 1=1 --',
    ]

    for inj_field in injection_fields:
        try:
            print(f"  Trying: {inj_field!r}", end=" ... ")
            db.update_task(task_id, **{inj_field: 'injected_value'})
            print("ACCEPTED (VULNERABLE)")
        except ValueError as e:
            print(f"REJECTED (FIXED): {e}")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {str(e)[:60]}")

    print("\n=== Analyzing Vulnerable Code ===\n")
    print("Current update_task implementation (line 247-250 of db.py):")
    print("""
    def update_task(task_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        _execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
    """)
    print("\nPROBLEM: Field names (keys from **fields dict) are directly interpolated")
    print("into the SQL string without validation. While parameter values are safe")
    print("(using ? placeholders), column/table names CANNOT be parameterized in SQLite.")
    print("\nSOLUTION: Validate field names against a whitelist of allowed columns or")
    print("use a regex pattern like: ^[A-Za-z_][A-Za-z0-9_]*$")

finally:
    if os.path.exists(test_db_path):
        os.unlink(test_db_path)
        print(f"\nCleaned up: {test_db_path}")
