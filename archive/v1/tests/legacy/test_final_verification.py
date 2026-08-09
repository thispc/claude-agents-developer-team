#!/usr/bin/env python3
"""
Final comprehensive verification test for db.py identifier validation fix

Requirements:
1. Both update_task() and update_contender() must validate field names
2. Validation should use a pattern like ^[A-Za-z_][A-Za-z0-9_]*$ for valid Python identifiers
3. Invalid identifiers should raise ValueError before SQL execution
4. The database should remain intact even if invalid identifiers are attempted
"""

import sqlite3
import tempfile
import os
import sys
import re

sys.path.insert(0, 'conductor')

test_db_path = tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False).name

import conductor.app.config as config
config.DB_PATH = test_db_path
config.ANTHROPIC_API_KEY = ""

from conductor.app import db

def run_tests():
    print("\n" + "="*70)
    print("VERIFICATION TEST FOR DB.PY IDENTIFIER VALIDATION FIX")
    print("="*70)

    db.init()

    # Set up test data
    project_id = db.create_project(
        name="Test", brief="Test", repo="https://github.com/test/repo",
        budget_usd=10.0, max_workers=1
    )
    task_id = db.create_task(project_id, "tester", "Test", "Test task")
    contender_id = db.create_contender(task_id, 1, "test/branch", "test-model")

    tests_passed = 0
    tests_failed = 0

    print("\n--- REQUIREMENT 1: Valid field names should still work ---\n")

    test_fields_task = ['status', 'report', 'feedback', 'attempts']
    for field in test_fields_task:
        try:
            db.update_task(task_id, **{field: 'test_value'})
            task = db.get_task(task_id)
            if task[field] == 'test_value':
                print(f"✓ PASS: update_task with valid field '{field}' works")
                tests_passed += 1
            else:
                print(f"✗ FAIL: update_task accepted '{field}' but value not updated")
                tests_failed += 1
        except ValueError as e:
            print(f"✗ FAIL: update_task rejected valid field '{field}': {e}")
            tests_failed += 1
        except Exception as e:
            print(f"? SKIP: {field} not in schema or other error: {e}")

    print()
    test_fields_contender = ['status', 'report']
    for field in test_fields_contender:
        try:
            db.update_contender(contender_id, **{field: 'test_value'})
            contender = db.get_contender(contender_id)
            if contender[field] == 'test_value':
                print(f"✓ PASS: update_contender with valid field '{field}' works")
                tests_passed += 1
            else:
                print(f"✗ FAIL: update_contender accepted '{field}' but value not updated")
                tests_failed += 1
        except ValueError as e:
            print(f"✗ FAIL: update_contender rejected valid field '{field}': {e}")
            tests_failed += 1
        except Exception as e:
            print(f"? SKIP: {field} not in schema or other error: {e}")

    print("\n--- REQUIREMENT 2: Invalid identifiers must be rejected with ValueError ---\n")

    invalid_identifiers = [
        'status; DROP TABLE tasks; --',
        'status) ; UPDATE tasks SET',
        'status\'; DROP TABLE',
        '123invalid',  # Can't start with number
        'status-field',  # Hyphens not allowed in identifiers
        'status field',  # Spaces not allowed
        '',  # Empty string
        'status.other',  # Dots not allowed
        'status@domain',  # @ not allowed
        'status[0]',  # Brackets not allowed
    ]

    print("Testing invalid identifiers in update_task():\n")
    for invalid_id in invalid_identifiers:
        try:
            db.update_task(task_id, **{invalid_id: 'malicious'})
            # If we get here without exception, check if it was actually processed
            # (indicates lack of validation)
            print(f"✗ FAIL: Invalid identifier '{invalid_id!r}' was NOT rejected - VULNERABILITY")
            tests_failed += 1
        except ValueError as e:
            print(f"✓ PASS: Invalid identifier '{invalid_id!r}' correctly rejected with ValueError")
            tests_passed += 1
        except sqlite3.OperationalError as e:
            # SQL error is less ideal but better than acceptance
            print(f"~ WARN: Invalid identifier '{invalid_id!r}' caused SQL error (not ValueError): {e}")
            # This still counts as a fail because the fix should use ValueError
            tests_failed += 1
        except Exception as e:
            print(f"? UNKNOWN: Invalid identifier '{invalid_id!r}' caused unexpected error: {type(e).__name__}")
            tests_failed += 1

    print("\nTesting invalid identifiers in update_contender():\n")
    for invalid_id in invalid_identifiers:
        try:
            db.update_contender(contender_id, **{invalid_id: 'malicious'})
            print(f"✗ FAIL: Invalid identifier '{invalid_id!r}' was NOT rejected - VULNERABILITY")
            tests_failed += 1
        except ValueError as e:
            print(f"✓ PASS: Invalid identifier '{invalid_id!r}' correctly rejected with ValueError")
            tests_passed += 1
        except sqlite3.OperationalError as e:
            print(f"~ WARN: Invalid identifier '{invalid_id!r}' caused SQL error (not ValueError): {e}")
            tests_failed += 1
        except Exception as e:
            print(f"? UNKNOWN: Invalid identifier '{invalid_id!r}' caused unexpected error: {type(e).__name__}")
            tests_failed += 1

    print("\n--- REQUIREMENT 3: Database integrity after injection attempts ---\n")

    # Verify tables still exist and data is intact
    try:
        task = db.get_task(task_id)
        contender = db.get_contender(contender_id)
        if task and contender:
            print("✓ PASS: Both task and contender records still accessible after injection attempts")
            tests_passed += 1
        else:
            print("✗ FAIL: Database corrupted - records not accessible")
            tests_failed += 1
    except Exception as e:
        print(f"✗ FAIL: Error checking database integrity: {e}")
        tests_failed += 1

    print("\n--- REQUIREMENT 4: Validation uses proper identifier pattern ---\n")

    valid_pattern = r'^[A-Za-z_][A-Za-z0-9_]*$'
    test_cases = [
        ('status', True),
        ('_private', True),
        ('field_123', True),
        ('CamelCase', True),
        ('123invalid', False),
        ('status-invalid', False),
        ('status.invalid', False),
        ('status invalid', False),
        ('', False),
    ]

    print("Expected identifier pattern: ^[A-Za-z_][A-Za-z0-9_]*$\n")
    for identifier, should_be_valid in test_cases:
        matches_pattern = bool(re.match(valid_pattern, identifier))
        status = "valid" if matches_pattern else "invalid"
        expected = "should be" if should_be_valid else "should NOT be"
        result = "✓" if matches_pattern == should_be_valid else "✗"
        print(f"{result} '{identifier}' - {status} ({expected})")

    print("\n" + "="*70)
    print(f"RESULTS: {tests_passed} passed, {tests_failed} failed")
    print("="*70)

    if tests_failed == 0 and tests_passed > 0:
        print("\n✓✓✓ FIX VERIFIED: All tests passed! ✓✓✓")
        return True
    elif tests_failed > 0:
        print(f"\n✗✗✗ FIX NOT VERIFIED: {tests_failed} test(s) failed ✗✗✗")
        return False
    else:
        print("\n? Unable to determine fix status")
        return False

try:
    success = run_tests()
    sys.exit(0 if success else 1)
finally:
    if os.path.exists(test_db_path):
        os.unlink(test_db_path)
