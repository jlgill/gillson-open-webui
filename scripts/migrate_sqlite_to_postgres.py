#!/usr/bin/env python3
"""
SQLite to PostgreSQL Migration Script for Open WebUI

This script migrates all data from a SQLite database to PostgreSQL.
The PostgreSQL schema should already exist (created by Alembic migrations).

Usage:
    python scripts/migrate_sqlite_to_postgres.py

Environment:
    - SQLITE_PATH: Path to SQLite database (default: backend/data/webui.db)
    - DATABASE_URL: PostgreSQL connection string
"""

import os
import sys
import sqlite3
import json
from pathlib import Path

# Add backend to path for imports
backend_path = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_path))

import psycopg2
from psycopg2.extras import execute_values

# Configuration
SQLITE_PATH = os.environ.get("SQLITE_PATH", "backend/data/webui.db")
POSTGRES_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:jamesram@localhost:5432/openwebui"
)

# Tables to migrate in order (respecting foreign key dependencies)
# Tables with no dependencies first, then dependent tables
MIGRATION_ORDER = [
    # Core tables (no FK dependencies)
    "user",
    "auth", 
    "config",
    "document",
    "migratehistory",
    "alembic_version",
    
    # Tables depending on user
    "api_key",
    "oauth_session",
    "chat",
    "file",
    "folder",
    "function",
    "knowledge",
    "memory",
    "model",
    "prompt",
    "tag",
    "tool",
    "feedback",
    "note",
    "group",
    "channel",
    
    # Tables with multiple dependencies
    "chatidtag",
    "group_member",
    "knowledge_file",
    "channel_member",
    "channel_webhook",
    "message",
    "message_reaction",
]


def get_sqlite_connection():
    """Create SQLite connection."""
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_postgres_connection():
    """Create PostgreSQL connection."""
    return psycopg2.connect(POSTGRES_URL)


def get_table_columns(sqlite_cursor, table_name):
    """Get column names for a table from SQLite."""
    sqlite_cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in sqlite_cursor.fetchall()]


def get_postgres_columns(pg_cursor, table_name):
    """Get column names for a table from PostgreSQL."""
    pg_cursor.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    return [row[0] for row in pg_cursor.fetchall()]


def get_postgres_boolean_columns(pg_cursor, table_name):
    """Get column names that are boolean type in PostgreSQL."""
    pg_cursor.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_schema = 'public' 
        AND table_name = %s 
        AND data_type = 'boolean'
    """, (table_name,))
    return {row[0] for row in pg_cursor.fetchall()}


def migrate_table(sqlite_conn, pg_conn, table_name):
    """Migrate a single table from SQLite to PostgreSQL."""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Handle reserved word 'group'
    sqlite_table_name = f'"{table_name}"' if table_name == 'group' else table_name
    
    # Get columns that exist in both databases
    sqlite_cols = set(get_table_columns(sqlite_cursor, table_name))
    pg_cols = set(get_postgres_columns(pg_cursor, table_name))
    common_cols = sqlite_cols & pg_cols
    
    if not common_cols:
        print(f"  ⚠️  No common columns found for {table_name}, skipping")
        return 0
    
    # Get boolean columns for type conversion
    boolean_cols = get_postgres_boolean_columns(pg_cursor, table_name)
    
    # Order columns consistently
    columns = sorted(list(common_cols))
    columns_str = ", ".join(f'"{col}"' for col in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    
    # Fetch data from SQLite
    sqlite_cursor.execute(f"SELECT {columns_str} FROM {sqlite_table_name}")
    rows = sqlite_cursor.fetchall()
    
    if not rows:
        print(f"  ℹ️  {table_name}: 0 rows (empty)")
        return 0
    
    # Clear existing data in PostgreSQL (if any)
    pg_cursor.execute(f'DELETE FROM "{table_name}"')
    
    # Convert rows to list of tuples, handling booleans and JSON properly
    data = []
    for row in rows:
        converted_row = []
        for i, value in enumerate(row):
            col_name = columns[i]
            # Convert SQLite integers to Python booleans for boolean columns
            if col_name in boolean_cols:
                if value is None:
                    converted_row.append(None)
                else:
                    converted_row.append(bool(value))
            elif value is None:
                converted_row.append(None)
            else:
                converted_row.append(value)
        data.append(tuple(converted_row))
    
    # Insert data into PostgreSQL
    insert_sql = f'INSERT INTO "{table_name}" ({columns_str}) VALUES ({placeholders})'
    
    try:
        pg_cursor.executemany(insert_sql, data)
        pg_conn.commit()
        print(f"  ✅ {table_name}: {len(rows)} rows migrated")
        return len(rows)
    except Exception as e:
        pg_conn.rollback()
        print(f"  ❌ {table_name}: Error - {e}")
        # Try one by one to identify problematic rows
        success_count = 0
        for i, row_data in enumerate(data):
            try:
                pg_cursor.execute(insert_sql, row_data)
                pg_conn.commit()
                success_count += 1
            except Exception as row_error:
                pg_conn.rollback()
                print(f"     Row {i} failed: {row_error}")
        print(f"  ⚠️  {table_name}: {success_count}/{len(rows)} rows migrated with errors")
        return success_count


def reset_sequences(pg_conn):
    """Reset PostgreSQL sequences after data import."""
    pg_cursor = pg_conn.cursor()
    
    # Tables with serial/sequence columns
    sequence_tables = [
        ("config", "id", "config_id_seq"),
        ("document", "id", "document_id_seq"),
        ("prompt", "id", "prompt_id_seq"),
        ("migratehistory", "id", "migratehistory_id_seq"),
    ]
    
    for table, column, seq_name in sequence_tables:
        try:
            pg_cursor.execute(f"""
                SELECT setval('{seq_name}', COALESCE((SELECT MAX("{column}") FROM "{table}"), 0) + 1, false)
            """)
            pg_conn.commit()
            print(f"  ✅ Reset sequence {seq_name}")
        except Exception as e:
            pg_conn.rollback()
            print(f"  ⚠️  Could not reset {seq_name}: {e}")


def main():
    print("=" * 60)
    print("Open WebUI: SQLite to PostgreSQL Migration")
    print("=" * 60)
    print(f"\nSource: {SQLITE_PATH}")
    print(f"Target: {POSTGRES_URL.split('@')[1] if '@' in POSTGRES_URL else POSTGRES_URL}")
    print()
    
    # Verify SQLite database exists
    if not Path(SQLITE_PATH).exists():
        print(f"❌ SQLite database not found: {SQLITE_PATH}")
        sys.exit(1)
    
    # Connect to databases
    print("Connecting to databases...")
    sqlite_conn = get_sqlite_connection()
    pg_conn = get_postgres_connection()
    print("  ✅ Connected to SQLite")
    print("  ✅ Connected to PostgreSQL")
    print()
    
    # Migrate tables
    print("Migrating tables...")
    total_rows = 0
    
    for table in MIGRATION_ORDER:
        try:
            rows = migrate_table(sqlite_conn, pg_conn, table)
            total_rows += rows
        except Exception as e:
            print(f"  ❌ {table}: Unexpected error - {e}")
    
    print()
    print("Resetting sequences...")
    reset_sequences(pg_conn)
    
    # Close connections
    sqlite_conn.close()
    pg_conn.close()
    
    print()
    print("=" * 60)
    print(f"Migration complete! Total rows migrated: {total_rows}")
    print("=" * 60)


if __name__ == "__main__":
    main()
