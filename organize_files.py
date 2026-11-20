#!/usr/bin/env python3
"""
Organize files in a directory into subdirectories
by modification date and extension.

Files are moved into: <source_dir>/YYYY/MM/<extension>/
Files with no extension are moved into: <source_dir>/YYYY/MM/misc/

Usage:
    python3 organize_files.py -d /path/to/directory
    python3 organize_files.py -d /path/to/directory --dry-run
    python3 organize_files.py -d /path/to/directory -r
    python3 organize_files.py -d /path/to/directory -r -p

Other options: -c/--change-name replaces whitespace in file names with
underscores; -u/--dedupe moves files with identical content into
<source_dir>/.trash, keeping the one with the oldest modification time.

With -p/--purge, empty directories are removed afterwards: only immediate
subdirectories by default, the whole tree when combined with -r.
"""

import argparse
import hashlib
import logging
import os
import platform
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# OS metadata files that don't stop a directory counting as empty when purging.
IGNORABLE_FILES = {".DS_Store"}

# Duplicates are moved here (inside the source directory) instead of deleted.
# It's hidden, so it is never scanned, organized, or purged.
TRASH_DIR_NAME = ".trash"

# Bytes hashed in the cheap first pass when looking for duplicates.
QUICK_HASH_BYTES = 64 * 1024


def detect_os() -> str:
    """Detect and return a human-friendly OS name."""
    system_map = {
        "Linux": "Linux",
        "Darwin": "MacOS",
        "Windows": "Windows",
    }
    system = platform.system()
    return system_map.get(system, system)


def get_file_details(filepath: Path) -> tuple[str, str, str]:
    """
    Extract the year, month, and extension from a file's modification time.

    Returns:
        A tuple of (year, month, extension).
        Extension is "misc" when the file has none.
    """
    mtime_timestamp = filepath.stat().st_mtime
    mtime = datetime.fromtimestamp(mtime_timestamp)
    year = mtime.strftime("%Y")
    month = mtime.strftime("%m")

    extension = filepath.suffix.lstrip(".").lower()
    if not extension:
        extension = "misc"

    return year, month, extension


def sanitize_filename(name: str) -> str:
    """Replace consecutive whitespace in a filename with underscores."""
    return re.sub(r"\s+", "_", name)


def resolve_duplicate(
    destination: Path, planned_destinations: set[Path]
) -> Path:
    """
    Return a non-colliding destination path.

    Checks both the filesystem and a set of already-planned destinations
    (needed for accurate dry-run output when multiple source files
    would map to the same target).

    Duplicates are resolved by appending _1, _2, etc. before the extension.
    """
    not_planned = destination not in planned_destinations
    if not_planned and not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    parent = destination.parent
    counter = 1

    while True:
        new_name = f"{stem}_{counter}{suffix}"
        candidate = parent / new_name
        not_planned = candidate not in planned_destinations
        if not_planned and not candidate.exists():
            return candidate
        counter += 1


def build_destination(
    source_dir: Path,
    filename: str,
    year: str,
    month: str,
    extension: str,
) -> Path:
    """Build destination: source_dir/YEAR/MONTH/EXT/filename."""
    return source_dir / year / month / extension / filename


def gather_files(source_dir: Path, recurse: bool = False) -> list[Path]:
    """
    Return all regular, non-hidden files inside source_dir.

    By default only files directly inside source_dir are returned. With
    recurse=True, subdirectories are searched too (hidden and symlinked
    directories are never entered).

    Directories, hidden files (starting with '.'), and symlinks
    are excluded. Sorted by path for deterministic output.
    """
    if not recurse:
        candidates = source_dir.iterdir()
    else:
        candidates = []
        for root, dirnames, filenames in os.walk(source_dir):
            # Prune in place so os.walk skips hidden and symlinked dirs.
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".")
                and not (Path(root) / d).is_symlink()
            ]
            candidates.extend(Path(root) / name for name in filenames)

    return sorted(
        item
        for item in candidates
        if item.is_file()
        and not item.is_symlink()
        and not item.name.startswith(".")
    )


def purge_empty_dirs(
    source_dir: Path,
    recurse: bool,
    dry_run: bool,
    gone: set[Path],
) -> int:
    """
    Remove empty directories under source_dir (never source_dir itself).

    Without recurse, only immediate subdirectories are considered; with
    recurse, the whole tree is, deepest first so that a directory emptied
    by removing its children is removed too. Hidden and symlinked
    directories are never touched. A directory holding only OS metadata
    files (IGNORABLE_FILES, e.g. .DS_Store) counts as empty and those
    files are deleted with it; any other hidden file keeps it alive.

    `gone` holds paths that are about to be (or have been) moved away.
    Treating them as absent makes dry-run output match a real run.

    Returns:
        The number of directories removed (or that would be removed).
    """
    if recurse:
        candidates = []
        for root, dirnames, _ in os.walk(source_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            candidates.extend(Path(root) / d for d in dirnames)
        candidates.reverse()  # parents are listed before children
    else:
        candidates = sorted(source_dir.iterdir())

    removed = 0
    for directory in candidates:
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or directory.name.startswith(".")
        ):
            continue
        try:
            entries = list(directory.iterdir())
            if any(
                entry not in gone and entry.name not in IGNORABLE_FILES
                for entry in entries
            ):
                continue
            if dry_run:
                logger.info("[DRY RUN] Remove empty dir: %s", directory)
            else:
                for entry in entries:
                    if entry.name in IGNORABLE_FILES:
                        entry.unlink()
                directory.rmdir()
                logger.info("Removed empty dir: %s", directory)
            gone.add(directory)
            removed += 1
        except OSError as exc:
            logger.error("Failed to remove '%s': %s", directory, exc)

    return removed


def file_hash(
    filepath: Path,
    limit: int | None = None,
    chunk_size: int = 1024 * 1024,
) -> str:
    """
    Return the SHA-256 hex digest of a file, read in chunks.

    SHA-256 was benchmarked against blake2b/blake2s/md5/sha1/sha3 and is
    the fastest reliable stdlib option on current CPUs (hardware
    accelerated). With `limit`, only the first `limit` bytes are hashed.
    """
    digest = hashlib.sha256()
    remaining = limit
    with filepath.open("rb") as handle:
        while True:
            size = chunk_size if remaining is None else min(
                chunk_size, remaining
            )
            chunk = handle.read(size) if size else b""
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def _group_by_hash(paths: list[Path], limit: int | None) -> list[list[Path]]:
    """Group paths by hash (of the first `limit` bytes); keep groups of 2+."""
    groups: dict[str, list[Path]] = {}
    for path in paths:
        try:
            groups.setdefault(file_hash(path, limit), []).append(path)
        except OSError as exc:
            logger.error("Failed to hash '%s': %s", path, exc)
    return [group for group in groups.values() if len(group) > 1]


def dedupe_files(
    files: list[Path], source_dir: Path, dry_run: bool
) -> tuple[list[Path], list[Path]]:
    """
    Move files whose content matches another file's into source_dir/.trash.

    Within each group of identical files, the one with the oldest
    modification time is kept (ties broken by path, so the result is
    deterministic). To avoid hashing whole large files needlessly,
    candidates are narrowed by size, then by a hash of the first
    QUICK_HASH_BYTES, and only then fully hashed.

    Duplicates keep their relative path inside .trash so they can be
    restored; name clashes there get _1, _2, ... suffixes.

    Returns:
        (remaining, removed): files still to be organized, and the
        duplicates trashed (or that would be trashed in a dry run).
    """
    by_size: dict[int, list[Path]] = {}
    for filepath in files:
        try:
            by_size.setdefault(filepath.stat().st_size, []).append(filepath)
        except OSError as exc:
            logger.error("Failed to stat '%s': %s", filepath, exc)

    trash_dir = source_dir / TRASH_DIR_NAME
    planned_trash: set[Path] = set()
    removed: list[Path] = []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue

        groups = [
            full
            for quick in _group_by_hash(same_size, QUICK_HASH_BYTES)
            for full in _group_by_hash(quick, None)
        ]
        for group in groups:
            group.sort(key=lambda p: (p.stat().st_mtime, str(p)))
            keeper = group[0]
            for duplicate in group[1:]:
                try:
                    target = resolve_duplicate(
                        trash_dir / duplicate.relative_to(source_dir),
                        planned_trash,
                    )
                    planned_trash.add(target)
                    if dry_run:
                        logger.info(
                            "[DRY RUN] Duplicate: %s -> %s (keeping %s)",
                            duplicate,
                            target,
                            keeper,
                        )
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(duplicate), str(target))
                        logger.info(
                            "Trashed duplicate: %s -> %s (kept %s)",
                            duplicate,
                            target,
                            keeper,
                        )
                    removed.append(duplicate)
                except OSError as exc:
                    logger.error(
                        "Failed to trash '%s': %s", duplicate, exc
                    )

    removed_set = set(removed)
    remaining = [f for f in files if f not in removed_set]
    return remaining, removed


def organize_files(
    source_dir: Path,
    dry_run: bool = False,
    recurse: bool = False,
    purge: bool = False,
    change_name: bool = False,
    dedupe: bool = False,
) -> dict[str, int]:
    """
    Scan source_dir and move files into dated/extension subfolders.

    With recurse=True, files in subdirectories are organized too. Files
    already at their correct location are left untouched, so re-running
    is safe. With purge=True, empty directories are removed afterwards
    (same scope as recurse; see purge_empty_dirs). With change_name=True,
    whitespace in file names is replaced with underscores. With
    dedupe=True, identical files are first moved to .trash, keeping the
    oldest (see dedupe_files).

    Returns:
        A dict with counts: {"moved", "skipped", "in_place", "purged",
        "duplicates"}
    """
    files = gather_files(source_dir, recurse=recurse)

    duplicates: list[Path] = []
    if dedupe:
        files, duplicates = dedupe_files(files, source_dir, dry_run)

    if files:
        logger.info(
            "Found %d eligible file(s) in '%s'.",
            len(files),
            source_dir,
        )
    else:
        logger.info("No eligible files found in '%s'.", source_dir)

    moved = 0
    skipped = 0
    in_place = 0
    planned_destinations: set[Path] = set()
    moved_sources: set[Path] = set()

    for filepath in files:
        try:
            year, month, extension = get_file_details(filepath)
            sanitized_name = (
                sanitize_filename(filepath.name)
                if change_name
                else filepath.name
            )
            destination = build_destination(
                source_dir, sanitized_name, year, month, extension
            )

            # Already organized (idempotency): don't rename it to name_1.ext.
            if destination == filepath:
                in_place += 1
                continue

            destination = resolve_duplicate(destination, planned_destinations)
            planned_destinations.add(destination)

            if dry_run:
                logger.info("[DRY RUN] %s -> %s", filepath, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(filepath), str(destination))
                logger.info("Moved: %s -> %s", filepath, destination)

            moved += 1
            moved_sources.add(filepath)

        except OSError as exc:
            logger.error("Failed to process '%s': %s", filepath, exc)
            skipped += 1

    purged = 0
    if purge:
        purged = purge_empty_dirs(
            source_dir, recurse, dry_run, moved_sources | set(duplicates)
        )

    return {
        "moved": moved,
        "skipped": skipped,
        "in_place": in_place,
        "purged": purged,
        "duplicates": len(duplicates),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and return command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Organize files into subdirectories "
            "by modification date and extension."
        ),
    )
    parser.add_argument(
        "-d",
        "--directory",
        type=Path,
        required=True,
        help="Path to the source directory to organize.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview file moves without making any changes.",
    )
    parser.add_argument(
        "-r",
        "--recurse",
        action="store_true",
        default=False,
        help="Also organize files in subdirectories (hidden and symlinked "
        "directories are skipped).",
    )
    parser.add_argument(
        "-p",
        "--purge",
        action="store_true",
        default=False,
        help="Remove empty directories afterwards. Only immediate "
        "subdirectories are checked, unless -r/--recurse is also given.",
    )
    parser.add_argument(
        "-c",
        "--change-name",
        action="store_true",
        default=False,
        help="Replace whitespace in file names with underscores.",
    )
    parser.add_argument(
        "-u",
        "--dedupe",
        action="store_true",
        default=False,
        help="Move duplicate files (same content) into <directory>/.trash, "
        "keeping only the one with the oldest modification time.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, detect OS, and run the organizer."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args(argv)
    source_dir = args.directory.expanduser().resolve()

    if not source_dir.is_dir():
        logger.error("'%s' is not a valid directory.", source_dir)
        return 1

    os_name = detect_os()
    logger.info("Operating system: %s", os_name)
    logger.info("Source directory:  %s", source_dir)

    if args.dry_run:
        logger.info("Mode: DRY RUN (no files will be moved)")

    if args.recurse:
        logger.info("Mode: RECURSIVE (including subdirectories)")

    if args.purge:
        logger.info("Mode: PURGE (empty directories will be removed)")

    if args.change_name:
        logger.info("Mode: CHANGE NAME (whitespace -> underscores)")
    if args.dedupe:
        logger.info("Mode: DEDUPE (duplicates moved to .trash, oldest kept)")

    results = organize_files(
        source_dir,
        dry_run=args.dry_run,
        recurse=args.recurse,
        purge=args.purge,
        change_name=args.change_name,
        dedupe=args.dedupe,
    )

    action = "Would move" if args.dry_run else "Moved"
    logger.info(
        "Done. %s %d file(s), skipped %d file(s), "
        "%d already in place.",
        action,
        results["moved"],
        results["skipped"],
        results["in_place"],
    )
    if args.dedupe:
        verb = "Would trash" if args.dry_run else "Trashed"
        logger.info("%s %d duplicate file(s).", verb, results["duplicates"])
    if args.purge:
        verb = "Would remove" if args.dry_run else "Removed"
        logger.info("%s %d empty directory(ies).", verb, results["purged"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
