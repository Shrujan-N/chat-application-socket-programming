# Maintainability Guide

## Purpose

This document describes the application's main modules, database configuration, testing practices, and conventions for future maintenance.

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `main.py` | Application routes, request handling, and application integration |
| `server.py` | Server startup and connection handling |
| `auth_service.py` | Authentication-related business logic |
| `security.py` | Password hashing, verification, and security helpers |
| `database.py` | SQLite connection management and core schema initialization |
| `web_database.py` | Web application database setup and schema initialization |
| `profile_service.py` | User profile operations |
| `push_service.py` | Push notification operations |
| `mailer.py` | Email-related functionality |
| `static/index.html` | Client-side interface and interaction logic |
| `static/sw.js` | Service worker functionality |

## Database Configuration

The core database module reads its path from `CHAT_DB_PATH`. If the variable is not set, the application uses `chat_app.db` in the application directory.

The web database module supports `CHAT_WEB_DB_PATH` and falls back to `CHAT_DB_PATH` when appropriate. Check both database modules when changing database schemas or initialization behavior.

Database connections should enable foreign-key enforcement and return named rows through `sqlite3.Row`.

## Development Guidelines

1. Keep request handlers focused on input validation, orchestration, and responses.
2. Place reusable business logic in suitable service modules rather than duplicating it across routes.
3. Keep database connection and schema initialization logic in the database modules.
4. Add regression tests when changing database behavior, authentication, or other shared functionality.
5. Use temporary database paths in tests to avoid modifying a developer's database.
6. Keep credentials, API keys, session tokens, and local database files out of commits.
7. Prefer focused changes and avoid unrelated modifications when addressing an issue.

## Testing

Run the complete test suite from the repository root:

    python -m pytest -v

Run the database maintainability tests from the App directory:

    python -m pytest .\test_maintainability.py -v

Run an individual test module from the App directory:

    python -m pytest .\test_auth.py -v

Review failures against the relevant test expectations and existing behavior before changing unrelated code.
