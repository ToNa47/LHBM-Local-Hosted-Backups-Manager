# LHBM (Local Hosted Backup Management)

**LHBM** is a lightweight backup utility designed for users who prefer an automated approach to protecting their files. Instead of relying on manual backups, LHBM continuously monitors selected files and creates backup archives whenever changes are detected.

**LHBM** was created after seeing how easily important documents could be misplaced or accidentally lost during everyday administrative work. The goal is to make local backups automatic, lightweight, and easy to restore even for users who don't remember to back up their files regularly.

## Planned Features

LHBM is currently under development. Future updates will include:

* Automatic file change detection
* Deleted file tracking
* Scheduled backup creation
* Compressed ZIP archive generation
* Simple backup restoration

## How It Works

### Backup Setup

1. Select the files or folders you want LHBM to monitor.
2. Choose a destination folder for storing backups.
3. Configure the desired monitoring interval.
4. Start the tracking process.

LHBM will automatically detect modifications and create timestamped ZIP backups.

## Restoring a Backup

1. Open your backup destination folder.
2. Navigate to the `zips` directory.
3. Select the backup corresponding to the desired date and time.
4. Extract the ZIP archive.
5. Restore or replace your files as needed.

## Project Goal

The goal of LHBM is to make local backups effortless by automating the monitoring and backup process, allowing users to recover previous versions of their files quickly without relying on cloud services.
