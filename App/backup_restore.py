"""Create and restore validated backups of the chat application's data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

FORMAT_NAME = "chat-application-backup"
FORMAT_VERSION = 1
DATABASE_ROLES = {"core", "web"}
CHUNK_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    """Raised when a backup cannot safely be created or restored."""


def configured_paths() -> tuple[dict[str, Path], Path]:
    app_dir = Path(__file__).resolve().parent
    shared_database = os.environ.get("CHAT_DB_PATH", str(app_dir / "chat_app.db"))

    database_paths = {
        "core": Path(shared_database).resolve(),
        "web": Path(
            os.environ.get("CHAT_WEB_DB_PATH", shared_database)
        ).resolve(),
    }
    uploads_dir = Path(
        os.environ.get("CHAT_UPLOADS_DIR", str(app_dir / "uploads"))
    ).resolve()

    return database_paths, uploads_dir


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def hash_archive_member(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name, "r") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def copy_archive_member(
    archive: zipfile.ZipFile,
    member: str,
    destination: Path,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()

    with archive.open(member, "r") as source, destination.open("wb") as output:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            output.write(chunk)

    return digest.hexdigest()


def write_file_to_archive(
    archive: zipfile.ZipFile,
    source_path: Path,
    member: str,
) -> str:
    digest = hashlib.sha256()

    with source_path.open("rb") as source, archive.open(member, "w") as output:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            output.write(chunk)

    return digest.hexdigest()


def validate_relative_path(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupError(f"Invalid {description} in backup manifest.")

    parts = value.split("/")
    path = PurePosixPath(value)

    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or path.as_posix() != value
    ):
        raise BackupError(f"Unsafe {description} in backup manifest: {value!r}")

    return value


def validate_hash(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value.lower())
    ):
        raise BackupError(f"Invalid SHA-256 digest for {description}.")

    return value.lower()


def collect_upload_paths(
    uploads_dir: Path,
) -> tuple[list[str], list[Path]]:
    directories: list[str] = []
    files: list[Path] = []

    if not uploads_dir.exists():
        return directories, files

    if uploads_dir.is_symlink() or not uploads_dir.is_dir():
        raise BackupError(f"Uploads path is not a regular directory: {uploads_dir}")

    for current, directory_names, file_names in os.walk(
        uploads_dir, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()

        for name in directory_names:
            directory = current_path / name
            if directory.is_symlink():
                raise BackupError(f"Symlink found in uploads: {directory}")
            directories.append(directory.relative_to(uploads_dir).as_posix())

        for name in file_names:
            file_path = current_path / name
            if file_path.is_symlink() or not file_path.is_file():
                raise BackupError(f"Unsupported file in uploads: {file_path}")
            files.append(file_path)

    directories.sort()
    files.sort(key=lambda item: item.relative_to(uploads_dir).as_posix())
    return directories, files


def check_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise BackupError(f"SQLite integrity check failed: {path}")
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError(f"Invalid SQLite database: {path}: {error}") from error


def create_backup(
    output_dir: Path,
    database_paths: dict[str, Path] | None = None,
    uploads_dir: Path | None = None,
) -> Path:
    if database_paths is None or uploads_dir is None:
        configured_databases, configured_uploads = configured_paths()
        database_paths = database_paths or configured_databases
        uploads_dir = uploads_dir or configured_uploads

    database_paths = {
        role: Path(path).resolve() for role, path in database_paths.items()
    }
    uploads_dir = Path(uploads_dir).resolve()
    output_dir = Path(output_dir).resolve()

    if set(database_paths) != DATABASE_ROLES:
        raise BackupError("Database paths must define exactly the core and web roles.")

    if output_dir == uploads_dir or uploads_dir in output_dir.parents:
        raise BackupError("Backup output must not be inside the uploads directory.")

    for role, path in database_paths.items():
        if not path.is_file():
            raise BackupError(f"The {role} database does not exist: {path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    upload_directories, upload_files = collect_upload_paths(uploads_dir)

    groups: dict[Path, list[str]] = {}
    for role, path in database_paths.items():
        groups.setdefault(path, []).append(role)

    timestamp = datetime.now(timezone.utc)
    archive_name = f"chat-backup-{timestamp.strftime('%Y%m%dT%H%M%SZ')}.zip"
    archive_path = output_dir / archive_name
    suffix = 1

    while archive_path.exists():
        archive_path = output_dir / (
            f"chat-backup-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{suffix:02d}.zip"
        )
        suffix += 1

    fd, temporary_archive_name = tempfile.mkstemp(
        prefix=".chat-backup-", suffix=".tmp", dir=output_dir
    )
    os.close(fd)
    temporary_archive = Path(temporary_archive_name)

    try:
        with tempfile.TemporaryDirectory(
            prefix=".chat-backup-build-", dir=output_dir
        ) as build_directory:
            build_dir = Path(build_directory)
            database_entries = []
            snapshots: list[tuple[Path, str]] = []

            for number, (source_path, roles) in enumerate(
                sorted(groups.items(), key=lambda item: str(item[0])), start=1
            ):
                snapshot = build_dir / f"database-{number:03d}.sqlite3"

                source = sqlite3.connect(str(source_path), timeout=30)
                destination = sqlite3.connect(str(snapshot), timeout=30)
                try:
                    source.backup(destination)
                except sqlite3.Error as error:
                    raise BackupError(
                        f"Could not back up database {source_path}: {error}"
                    ) from error
                finally:
                    destination.close()
                    source.close()

                check_database(snapshot)
                member = f"databases/database-{number:03d}.sqlite3"
                snapshots.append((snapshot, member))
                database_entries.append({
                    "roles": sorted(roles),
                    "archive_path": member,
                    "sha256": sha256_file(snapshot),
                })

            upload_entries = []
            manifest = {
                "format": FORMAT_NAME,
                "version": FORMAT_VERSION,
                "created_at": timestamp.isoformat(),
                "databases": database_entries,
                "upload_directories": upload_directories,
                "upload_files": upload_entries,
            }

            with zipfile.ZipFile(
                temporary_archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                for snapshot, member in snapshots:
                    archive.write(snapshot, member)

                for source_path in upload_files:
                    relative_path = source_path.relative_to(uploads_dir).as_posix()
                    member = f"uploads/{relative_path}"
                    digest = write_file_to_archive(archive, source_path, member)
                    upload_entries.append({
                        "relative_path": relative_path,
                        "archive_path": member,
                        "sha256": digest,
                    })

                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True),
                )

        with zipfile.ZipFile(temporary_archive, "r") as archive:
            damaged_member = archive.testzip()
            if damaged_member:
                raise BackupError(f"Generated backup failed CRC check: {damaged_member}")

        os.replace(temporary_archive, archive_path)
        return archive_path
    finally:
        temporary_archive.unlink(missing_ok=True)


def read_and_validate_manifest(
    archive: zipfile.ZipFile,
) -> tuple[dict, list[dict], list[dict], list[str]]:
    names = archive.namelist()

    if len(names) != len(set(names)):
        raise BackupError("Backup contains duplicate archive entries.")

    if "manifest.json" not in names:
        raise BackupError("Backup has no manifest.json.")

    damaged_member = archive.testzip()
    if damaged_member:
        raise BackupError(f"Backup CRC check failed: {damaged_member}")

    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError("Backup manifest is not valid JSON.") from error

    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT_NAME
        or manifest.get("version") != FORMAT_VERSION
    ):
        raise BackupError("Unsupported backup format or version.")

    databases = manifest.get("databases")
    upload_files = manifest.get("upload_files")
    upload_directories = manifest.get("upload_directories")

    if not isinstance(databases, list) or not databases:
        raise BackupError("Backup manifest contains no databases.")
    if not isinstance(upload_files, list) or not isinstance(upload_directories, list):
        raise BackupError("Backup manifest has invalid uploads metadata.")

    covered_roles: list[str] = []
    expected_names = {"manifest.json"}
    checked_databases = []
    checked_upload_files = []
    checked_directories = []

    for entry in databases:
        if not isinstance(entry, dict):
            raise BackupError("Invalid database entry in manifest.")

        roles = entry.get("roles")
        member = validate_relative_path(
            entry.get("archive_path"), "database archive path"
        )
        digest = validate_hash(entry.get("sha256"), member)

        if (
            not isinstance(roles, list)
            or not roles
            or member.split("/")[0] != "databases"
            or len(PurePosixPath(member).parts) != 2
            or any(role not in DATABASE_ROLES for role in roles)
            or len(roles) != len(set(roles))
        ):
            raise BackupError(f"Invalid database entry: {member}")

        covered_roles.extend(roles)
        expected_names.add(member)
        checked_databases.append({
            "roles": roles,
            "archive_path": member,
            "sha256": digest,
        })

    if sorted(covered_roles) != sorted(DATABASE_ROLES):
        raise BackupError("Backup must cover core and web database roles exactly once.")

    for entry in upload_files:
        if not isinstance(entry, dict):
            raise BackupError("Invalid upload-file entry in manifest.")

        relative_path = validate_relative_path(
            entry.get("relative_path"), "upload relative path"
        )
        member = validate_relative_path(
            entry.get("archive_path"), "upload archive path"
        )
        digest = validate_hash(entry.get("sha256"), relative_path)

        if member != f"uploads/{relative_path}":
            raise BackupError(f"Upload path does not match its archive entry: {member}")

        expected_names.add(member)
        checked_upload_files.append({
            "relative_path": relative_path,
            "archive_path": member,
            "sha256": digest,
        })

    for directory in upload_directories:
        checked = validate_relative_path(directory, "upload directory path")
        if checked in checked_directories:
            raise BackupError(f"Duplicate upload directory: {checked}")
        checked_directories.append(checked)

    for file_entry in checked_upload_files:
        if file_entry["relative_path"] in checked_directories:
            raise BackupError("An upload path cannot be both a file and a directory.")

    if set(names) != expected_names:
        raise BackupError("Backup contains unexpected or missing archive entries.")

    return manifest, checked_databases, checked_upload_files, checked_directories


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def restore_backup(
    archive_path: Path,
    database_paths: dict[str, Path] | None = None,
    uploads_dir: Path | None = None,
) -> None:
    if database_paths is None or uploads_dir is None:
        configured_databases, configured_uploads = configured_paths()
        database_paths = database_paths or configured_databases
        uploads_dir = uploads_dir or configured_uploads

    database_paths = {
        role: Path(path).resolve() for role, path in database_paths.items()
    }
    uploads_dir = Path(uploads_dir).resolve()
    archive_path = Path(archive_path).resolve()

    if set(database_paths) != DATABASE_ROLES:
        raise BackupError("Database paths must define exactly the core and web roles.")

    if not archive_path.is_file():
        raise BackupError(f"Backup archive not found: {archive_path}")

    for database_path in database_paths.values():
        if database_path == uploads_dir or uploads_dir in database_path.parents:
            raise BackupError(
                "A database file cannot be located inside the uploads directory."
            )

    temporary_roots: list[Path] = []
    previous_paths: list[tuple[Path, Path]] = []
    installed_paths: list[tuple[Path, bool]] = []

    try:
        with zipfile.ZipFile(archive_path, "r") as archive, tempfile.TemporaryDirectory(
            prefix="chat-restore-validate-"
        ) as validation_directory:
            _, databases, upload_files, upload_directories = (
                read_and_validate_manifest(archive)
            )
            validation_dir = Path(validation_directory)
            validated_databases: dict[str, Path] = {}

            for entry in databases:
                member = entry["archive_path"]
                destination = validation_dir / Path(*PurePosixPath(member).parts)
                digest = copy_archive_member(archive, member, destination)

                if digest != entry["sha256"]:
                    raise BackupError(f"Database digest mismatch: {member}")

                check_database(destination)
                validated_databases[member] = destination

            for entry in upload_files:
                if hash_archive_member(archive, entry["archive_path"]) != entry["sha256"]:
                    raise BackupError(
                        f"Upload digest mismatch: {entry['relative_path']}"
                    )

            # Map current role paths to validated database snapshots. If the
            # configured roles share a path, their snapshots must agree.
            target_databases: dict[Path, tuple[str, str]] = {}

            for entry in databases:
                for role in entry["roles"]:
                    target = database_paths[role].resolve()
                    snapshot = (entry["archive_path"], entry["sha256"])
                    existing = target_databases.get(target)

                    if existing and existing[1] != snapshot[1]:
                        raise BackupError(
                            "The selected backup has different core and web "
                            "databases, but both roles currently use the same path."
                        )

                    target_databases[target] = snapshot

            staging_databases: list[tuple[Path, Path]] = []

            for target, (member, _) in target_databases.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                stage_dir = Path(tempfile.mkdtemp(
                    prefix=".db-restore-stage-", dir=target.parent
                ))
                temporary_roots.append(stage_dir)
                staged_file = stage_dir / target.name
                shutil.copy2(validated_databases[member], staged_file)
                staging_databases.append((target, staged_file))

            uploads_dir.parent.mkdir(parents=True, exist_ok=True)
            staged_uploads = Path(tempfile.mkdtemp(
                prefix=f".{uploads_dir.name}.restore-stage-",
                dir=uploads_dir.parent,
            ))
            temporary_roots.append(staged_uploads)

            for relative_path in upload_directories:
                (staged_uploads / Path(*PurePosixPath(relative_path).parts)).mkdir(
                    parents=True, exist_ok=True
                )

            for entry in upload_files:
                relative = Path(*PurePosixPath(entry["relative_path"]).parts)
                destination = staged_uploads / relative
                digest = copy_archive_member(
                    archive, entry["archive_path"], destination
                )
                if digest != entry["sha256"]:
                    raise BackupError(
                        f"Upload changed while staging: {entry['relative_path']}"
                    )

        def move_previous(path: Path) -> None:
            if path.exists() or path.is_symlink():
                previous = path.with_name(
                    f".{path.name}.pre-restore-{uuid.uuid4().hex}"
                )
                os.replace(path, previous)
                previous_paths.append((path, previous))

        # The service must be stopped before restoring its databases.
        try:
            for target, staged_file in staging_databases:
                for sidecar in (
                    target,
                    Path(f"{target}-wal"),
                    Path(f"{target}-shm"),
                ):
                    move_previous(sidecar)

                os.replace(staged_file, target)
                installed_paths.append((target, False))

            move_previous(uploads_dir)
            os.replace(staged_uploads, uploads_dir)
            installed_paths.append((uploads_dir, True))

        except Exception:
            for target, is_directory in reversed(installed_paths):
                try:
                    remove_path(target)
                except OSError:
                    pass

            for original, previous in reversed(previous_paths):
                if previous.exists() or previous.is_symlink():
                    os.replace(previous, original)
            raise

        for _, previous in previous_paths:
            try:
                remove_path(previous)
            except OSError:
                pass

    finally:
        for temporary_root in reversed(temporary_roots):
            if temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up or restore chat application databases and uploads."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup_parser = commands.add_parser("backup", help="Create a backup archive.")
    backup_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "backups",
        help="Directory for the archive (default: App/backups).",
    )

    restore_parser = commands.add_parser(
        "restore", help="Validate and restore a backup archive."
    )
    restore_parser.add_argument("archive", type=Path, help="Backup ZIP archive.")
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm replacement of the configured databases and uploads.",
    )

    args = parser.parse_args()

    try:
        if args.command == "backup":
            archive = create_backup(args.output_dir)
            print(f"Backup created: {archive}")
        else:
            if not args.yes:
                parser.error(
                    "restore replaces current databases and uploads; "
                    "rerun with --yes after stopping the application."
                )

            print("Restore requested. Ensure the chat server is stopped.")
            restore_backup(args.archive)
            print("Restore completed successfully.")
    except (BackupError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(1, f"Backup/restore error: {error}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
