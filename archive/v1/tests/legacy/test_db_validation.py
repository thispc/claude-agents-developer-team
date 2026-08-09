#!/usr/bin/env python3
"""
Test script to verify SQL identifier validation fix in conductor/app/db.py

This test demonstrates:
1. Current vulnerability: update_task and update_contender allow SQL injection via field names
2. Expected fix: validation that rejects invalid identifiers before executing SQL
"""

import sqlite3
import tempfile
import os
import sys
import json
import time
from pathlib import Path

# Adjust Python path to import from conductor
sys.path.insert(0, 'conductor')

# Create a temporary database for testing
test_db_file = tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False)
test_db_path = test_db_file.name
test_db_file.close()

print(f"Using temporary test database: {test_db_path}")

# Mock the config module to use our test database
import conductor.app.config as config
config.DB_PATH = test_db_path
config.ANTHROPIC_API_KEY = ""  # Mock API key

try:
    # Import db module
    from conductor.app import db

    print("\n=== Test 1: App initialization ===")
    db.init()
    print("✓ Database initialized successfully")

    print("\n=== Test 2: Create a test project ===")
    project_id = db.create_project(
        name="Test Project",
        brief="Test brief",
        repo="https://github.com/test/repo",
        budget_usd=10.0,
        max_workers=1
    )
    print(f"✓ Created project with ID: {project_id}")

    print("\n=== Test 3: Create a test task ===")
    task_id = db.create_task(
        project_id=project_id,
        role="tester",
        title="Test Task",
        description="Test task for verification"
    )
    print(f"✓ Created task with ID: {task_id}")

    print("\n=== Test 4: Test valid update_task ===")
    db.update_task(task_id, status='queued')
    task = db.get_task(task_id)
    assert task['status'] == 'queued', f"Expected status='queued', got '{task['status']}'"
    print(f"✓ Valid update succeeded: status = {task['status']}")

    print("\n=== Test 5: Test VULNERABLE update_task with injection ===")
    # This should FAIL if the fix is applied, but currently SUCCEEDS (vulnerability!)
    try:
        malicious_field = "status; DROP TABLE tasks; --"
        print(f"  Attempting to execute: update_task(task_id, {{{malicious_field!r}: 'bad'}})")
        db.update_task(task_id, **{malicious_field: 'bad'})

        # Check if tasks table still exists
        result = db.get_task(task_id)
        if result is None:
            print("✗ VULNERABILITY: Tasks table was dropped! (This is bad!)")
            print("  The fix should have prevented this SQL injection.")
        else:
            print("✗ VULNERABILITY: Injection was accepted without raising an error")
            print("  The fix should raise ValueError for invalid field names.")
    except ValueError as e:
        print(f"✓ FIXED: Correctly rejected invalid identifier with error: {e}")
    except Exception as e:
        print(f"? Unexpected error (might indicate partial fix): {type(e).__name__}: {e}")

    print("\n=== Test 6: Create a test contender ===")
    contender_id = db.create_contender(
        task_id=task_id,
        idx=1,
        branch="test/branch",
        model="test-model"
    )
    print(f"✓ Created contender with ID: {contender_id}")

    print("\n=== Test 7: Test valid update_contender ===")
    db.update_contender(contender_id, status='pushed')
    contender = db.get_contender(contender_id)
    assert contender['status'] == 'pushed', f"Expected status='pushed', got '{contender['status']}'"
    print(f"✓ Valid update succeeded: status = {contender['status']}")

    print("\n=== Test 8: Test VULNERABLE update_contender with injection ===")
    # This should FAIL if the fix is applied, but currently SUCCEEDS (vulnerability!)
    try:
        malicious_field = "status; DROP TABLE contenders; --"
        print(f"  Attempting to execute: update_contender(contender_id, {{{malicious_field!r}: 'bad'}})")
        db.update_contender(contender_id, **{malicious_field: 'bad'})

        # Check if contenders table still exists
        result = db.get_contender(contender_id)
        if result is None:
            print("✗ VULNERABILITY: Contenders table was dropped! (This is bad!)")
            print("  The fix should have prevented this SQL injection.")
        else:
            print("✗ VULNERABILITY: Injection was accepted without raising an error")
            print("  The fix should raise ValueError for invalid field names.")
    except ValueError as e:
        print(f"✓ FIXED: Correctly rejected invalid identifier with error: {e}")
    except Exception as e:
        print(f"? Unexpected error (might indicate partial fix): {type(e).__name__}: {e}")

    print("\n=== Test 9: Verify tables are still intact ===")
    # Verify data integrity after injection attempts
    task_check = db.get_task(task_id)
    contender_check = db.get_contender(contender_id)

    if task_check and contender_check:
        print(f"✓ Both tables intact after injection attempts")
        print(f"  Task status: {task_check['status']}")
        print(f"  Contender status: {contender_check['status']}")
    else:
        if not task_check:
            print(f"✗ Tasks table was corrupted!")
        if not contender_check:
            print(f"✗ Contenders table was corrupted!")

    print("\n=== Test 10: Test valid identifiers ===")
    # Test that valid field names still work
    valid_identifiers = [
        'status',
        'report',
        '_internal_field',
        'field_name_123',
    ]
    for field_name in valid_identifiers:
        try:
            # Only test if field is in schema
            db.update_task(task_id, **{field_name: 'test'})
            print(f"✓ Valid identifier '{field_name}' accepted")
        except ValueError:
            print(f"? Identifier '{field_name}' was rejected (might not be in schema)")
        except Exception as e:
            # Field might not exist in schema, that's ok
            pass

    print("\n=== SUMMARY ===")
    print("If you see '✗ VULNERABILITY' messages, the fix has NOT been applied.")
    print("If you see '✓ FIXED' messages, the fix HAS been applied correctly.")

finally:
    # Clean up test database
    if os.path.exists(test_db_path):
        os.unlink(test_db_path)
        print(f"\nCleaned up test database: {test_db_path}")
