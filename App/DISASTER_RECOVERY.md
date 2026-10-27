# Disaster Recovery Guide

## Purpose

This guide explains how to back up and restore persistent data for the chat application.

The backup utility covers the core SQLite database, the web application's SQLite database, and uploaded files such as profile avatars and message attachments. If both database roles use the same SQLite file, the utility records one database snapshot for both roles.

Application source code should be recovered from Git. Environment variables and deployment secrets must be restored separately from the project's approved configuration or secret store.

## 1. Create a Backup

For a consistent application-wide snapshot, stop the web application and socket server before creating the backup. SQLite provides consistent individual database snapshots, but concurrent changes to database records and uploaded files are not captured atomically.

Run the command from the `App` directory:

    python .\backup_restore.py backup --output-dir .\backups

The utility creates a timestamped ZIP archive in the selected directory. The archive contains database snapshots, upload files, and a manifest containing SHA-256 checksums.

Use a different output directory if you do not want backups stored inside the application directory.

## 2. Verify the Backup Utility

Run the disaster-recovery tests:

    python -m pytest .\test_backup_restore.py -v

These tests verify database and upload restoration, shared database configuration, and rejection of a backup containing an invalid database checksum.

Run the full regression suite before deployment:

    python -m pytest -v

Review failures and warnings against the project's existing test baseline.

## 3. Store Backups Safely

- Copy completed archives to a separate, access-controlled storage location.
- Keep at least one backup separate from the machine hosting the application.
- Restrict access because backups may contain messages, account data, password hashes, and uploaded files.
- The ZIP archive is not encrypted by this utility. Use encrypted storage or an approved encryption process when required.
- Retain multiple backup generations so accidental deletion or corruption can be recovered from an earlier backup.
- Periodically test restoration rather than relying only on successful backup creation.

## 4. Restore from a Backup

Stop the web application and socket server before restoring. Ensure no other process is writing to the configured databases or upload directory.

Select a known backup archive and run:

    python .\backup_restore.py restore .\backups\chat-backup-YYYYMMDDTHHMMSSZ.zip --yes

Replace the example archive name with the actual filename. The `--yes` flag explicitly confirms that the configured databases and uploads may be replaced.

The utility validates the archive structure, manifest, file checksums, and SQLite database integrity before installing restored data.

The restore operation targets the database paths and upload directory configured for the current environment. It does not restore environment variables, application source code, installed dependencies, or deployment secrets.

## 5. Validate After Restoration

Before restarting the application:

1. Confirm the restore command completed successfully.
2. Verify that expected database records and uploaded files are present.
3. Start the application and check login, contacts, message history, profiles, and file access.
4. Review application logs for database or file errors.
5. Run relevant regression tests against an isolated test environment.

Do not run restore against production data without confirming the selected archive and obtaining the required operational approval.

## 6. Recovery Limitations

A backup only contains persistent data captured when the snapshot was created. Messages or uploaded files created after that backup may be lost during recovery.

The utility does not automatically schedule backups, upload archives to remote storage, encrypt archives, or stop running application services. These responsibilities must be handled by the deployment and operations process.
