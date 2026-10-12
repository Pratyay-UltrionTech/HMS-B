"""
Migration: Hospital Onboarding, Lifecycle Management & Hardening.

Delegates directly to canonical template migration runner scripts/migrate.py.
Idempotent and safe to run multiple times.

Usage: python scripts/migrate_hospital_onboarding_and_hardening.py
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.migrate import run_migration


if __name__ == "__main__":
    run_migration()
