"""Regression tests for the core SQLite database contract."""

import sqlite3

import database


def test_get_db_enables_foreign_keys_and_named_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "connections.db")

    connection = database.get_db()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        connection.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO sample (value) VALUES (?)", ("maintainable",)
        )
        row = connection.execute("SELECT value FROM sample").fetchone()

        assert isinstance(row, sqlite3.Row)
        assert row["value"] == "maintainable"
    finally:
        connection.close()


def test_init_db_is_safe_to_run_repeatedly(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "initialized.db")

    database.init_db()
    database.init_db()

    connection = database.get_db()
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "users",
            "contacts",
            "messages",
            "login_logs",
            "password_resets",
            "profiles",
            "push_subscriptions",
        } <= tables

        seeded_users = connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        assert seeded_users >= 3
    finally:
        connection.close()
