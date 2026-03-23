# -*- coding: utf-8 -*-
"""Quick syntax check for modified files"""
import ast
import os
import sys

src_dir = os.path.dirname(os.path.abspath(__file__))
files_to_check = [
    'prompts.py',
    'brain.py',
    'app.py',
    'skills/settings.py',
    'skills/voice_journal.py',
    'skills/checkin_flow.py',
    'skills/daily_report.py',
    'skills/weekly_review.py',
    'skills/monthly_review.py',
]

errors = []
for f in files_to_check:
    path = os.path.join(src_dir, f)
    if not os.path.exists(path):
        print(f"  MISSING: {f}")
        errors.append(f)
        continue
    try:
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
        ast.parse(source)
        print(f"  OK: {f}")
    except SyntaxError as e:
        print(f"  ERROR: {f} - {e}")
        errors.append(f)

# Also check that deleted files are gone
deleted = [
    'skills/mood_diary.py',
    'skills/reflect.py',
    'skills/book_notes.py',
    'skills/media_notes.py',
    'skills/habit_coach.py',
]
for f in deleted:
    path = os.path.join(src_dir, f)
    if os.path.exists(path):
        print(f"  STILL EXISTS (should be deleted): {f}")
        errors.append(f)
    else:
        print(f"  DELETED OK: {f}")

# Check prompts.py specific content
sys.path.insert(0, src_dir)
try:
    import prompts
    print(f"\n  SKILL_PROMPT_LINES keys: {len(prompts.SKILL_PROMPT_LINES)}")
    # Check no deleted module references
    for key in prompts.SKILL_PROMPT_LINES:
        if key.startswith(('book.', 'media.', 'mood.', 'habit.', 'reflect.')):
            print(f"  WARNING: {key} still in SKILL_PROMPT_LINES!")
            errors.append(key)
    # Check RULES_BOOKS_MEDIA and RULES_HABITS are gone
    if hasattr(prompts, 'RULES_BOOKS_MEDIA'):
        print("  WARNING: RULES_BOOKS_MEDIA still exists!")
        errors.append('RULES_BOOKS_MEDIA')
    if hasattr(prompts, 'RULES_HABITS'):
        print("  WARNING: RULES_HABITS still exists!")
        errors.append('RULES_HABITS')
    if hasattr(prompts, 'MOOD_SYSTEM'):
        print("  WARNING: MOOD_SYSTEM still exists!")
        errors.append('MOOD_SYSTEM')
    if hasattr(prompts, 'REFLECT_RESPONSE'):
        print("  WARNING: REFLECT_RESPONSE still exists!")
        errors.append('REFLECT_RESPONSE')
    if hasattr(prompts, 'WEEKLY_SYSTEM'):
        print("  WARNING: WEEKLY_SYSTEM still exists!")
        errors.append('WEEKLY_SYSTEM')
    if hasattr(prompts, 'MONTHLY_SYSTEM'):
        print("  WARNING: MONTHLY_SYSTEM still exists!")
        errors.append('MONTHLY_SYSTEM')
    if hasattr(prompts, 'BOOK_SUMMARY_SYSTEM'):
        print("  WARNING: BOOK_SUMMARY_SYSTEM still exists!")
        errors.append('BOOK_SUMMARY_SYSTEM')
    if hasattr(prompts, 'DAILY_SYSTEM'):
        print("  WARNING: DAILY_SYSTEM still exists!")
        errors.append('DAILY_SYSTEM')
    # Check RULES_TOP3 exists
    if hasattr(prompts, 'RULES_TOP3'):
        print("  RULES_TOP3 exists: OK")
    else:
        print("  WARNING: RULES_TOP3 missing!")
        errors.append('RULES_TOP3 missing')
    print(f"  prompts import OK")
except Exception as e:
    print(f"  prompts import FAILED: {e}")
    import traceback
    traceback.print_exc()
    errors.append('prompts import')

if errors:
    print(f"\n=== {len(errors)} errors found ===")
    sys.exit(1)
else:
    print(f"\n=== All checks passed ===")
    sys.exit(0)
