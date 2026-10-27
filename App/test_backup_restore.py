"""Tests for validated application backups and disaster recovery."""

import json
import sqlite3
import zipfile

import pytest

import backup_restore
from backup_restore import BackupError, create_backup, restore_backup


def create_database(path, value):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recovery_test (value TEXT NOT NULL)"
        )
        connection.execute("DELETE FROM recovery_test")
        connection.execute(
            "INSERT INTO recovery_test (value) VALUES (?)", (value,)
        )
        connection.commit()
    finally:
        connection.close()


def read_database_value(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT value FROM recovery_test"
        ).fetchone()[0]
    finally:
        connection.close()


def test_backup_restores_databases_and_uploads(tmp_path):
    core_db = tmp_path / "core.db"
    web_db = tmp_path / "web.db"
    uploads = tmp_path / "uploads"
    output_dir = tmp_path / "backups"

    create_database(core_db, "core-before-backup")
    create_database(web_db, "web-before-backup")

    avatar = uploads / "avatars" / "profile.png"
    attachment = uploads / "files" / "document.txt"
    avatar.parent.mkdir(parents=True)
    attachment.parent.mkdir(parents=True)
    avatar.write_bytes(b"avatar-original")
    attachment.write_text("attachment-original", encoding="utf-8")

    database_paths = {"core": core_db, "web": web_db}
    archive_path = create_backup(output_dir, database_paths, uploads)

    create_database(core_db, "core-after-backup")
    create_database(web_db, "web-after-backup")
    avatar.write_bytes(b"avatar-modified")
    attachment.unlink()
    (uploads / "stale.txt").write_text("not in backup", encoding="utf-8")

    restore_backup(archive_path, database_paths, uploads)

    assert read_database_value(core_db) == "core-before-backup"
    assert read_database_value(web_db) == "web-before-backup"
    assert avatar.read_bytes() == b"avatar-original"
    assert attachment.read_text(encoding="utf-8") == "attachment-original"
    assert not (uploads / "stale.txt").exists()


def test_backup_supports_shared_core_and_web_database(tmp_path):
    shared_db = tmp_path / "shared.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    create_database(shared_db, "before")
    database_paths = {"core": shared_db, "web": shared_db}

    archive_path = create_backup(
        tmp_path / "backups", database_paths, uploads
    )

    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

    assert manifest["databases"][0]["roles"] == ["core", "web"]

    create_database(shared_db, "after")
    restore_backup(archive_path, database_paths, uploads)

    assert read_database_value(shared_db) == "before"


def test_invalid_database_digest_is_rejected_without_changing_data(tmp_path):
    core_db = tmp_path / "core.db"
    web_db = tmp_path / "web.db"
    uploads = tmp_path / "uploads"

    create_database(core_db, "original-core")
    create_database(web_db, "original-web")
    uploads.mkdir()
    (uploads / "original.txt").write_text("original", encoding="utf-8")

    database_paths = {"core": core_db, "web": web_db}
    archive_path = create_backup(
        tmp_path / "backups", database_paths, uploads
    )
    tampered_path = tmp_path / "tampered.zip"

    with zipfile.ZipFile(archive_path, "r") as source:
        entries = {
            name: source.read(name)
            for name in source.namelist()
        }

    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    manifest["databases"][0]["sha256"] = "0" * 64
    entries["manifest.json"] = json.dumps(manifest).encode("utf-8")

    with zipfile.ZipFile(
        tampered_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as destination:
        for name, content in entries.items():
            destination.writestr(name, content)

    with pytest.raises(BackupError, match="digest mismatch"):
        restore_backup(tampered_path, database_paths, uploads)

    assert read_database_value(core_db) == "original-core"
    assert read_database_value(web_db) == "original-web"
    assert (uploads / "original.txt").read_text(encoding="utf-8") == "original"
