#!/usr/bin/env python3
"""
Organize files into <source_dir>/YYYY/MM/<extension>/<filename>
(or 'misc' when a file has no extension) based on modification time.

Usage:
    python3 organize_files.py -d /path/to/folder [options]

Options:
    -d, --directory    Source directory (required).
    --dry-run          Preview all changes without touching any file.
    -r, --recurse      Include files in subdirectories (hidden dirs, symlinks
                       and junctions are never entered).
    -s, --sort         Organize files into YYYY/MM/<ext> folders. Also the
                       default when no action option is given.
    -p, --purge        Clean up: PERMANENTLY delete the .trash directories,
                       then remove empty directories (immediate
                       subdirectories only, or the whole tree with -r).
                       Preview with --dry-run.
    -c, --change-name  Replace whitespace in file names with underscores
                       (requires -s).
    -u, --dedupe       Move files with identical content into
                       <source_dir>/.trash, keeping the oldest copy.
    -t, --trash        List the hidden .trash directories (whole tree with -r).

Actions (-s, -u, -p, -t) are independent: only the ones you give run.
With -p and -u together, trash from earlier runs is deleted first, so the
duplicates trashed by this run are kept. -t lists what remains at the end.

Notes:
    - Safe to re-run: files already in place are left untouched.
    - Year/month use the machine's local time zone.
    - Exit codes: 0 success, 1 invalid directory, 3 some files were skipped.
    - Works the same on Windows, macOS and Linux; name comparisons are
      case-insensitive and Unicode-normalized on every OS.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import re
import shutil
import stat
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Lowercase OS metadata files ignored when checking if a directory is empty.
IGNORABLE_FILES = {".ds_store", "thumbs.db", "desktop.ini"}
# Hidden, so it is never scanned, organized, or purged.
TRASH_DIR_NAME = ".trash"
# Bytes hashed in the cheap first pass when looking for duplicates.
QUICK_HASH_BYTES = 64 * 1024
# Exit code when the run finished but some files could not be processed.
EXIT_PARTIAL = 3


def normalize_path_key(path: Path) -> str:
    """
    Return a key for comparing paths identically on every OS.

    Casefolded and Unicode-NFC normalized, because Windows/macOS are
    usually case-insensitive and macOS stores names in decomposed form,
    while Linux is neither. (os.path.normcase only lowercases on Windows.)
    """
    return unicodedata.normalize("NFC", str(path)).casefold()


def safe_extension(filepath: Path) -> str:
    """
    Return a lowercase, filesystem-safe folder name for a file's extension.

    Anything outside [a-z0-9_-] is dropped (spaces, ':' and '?' are invalid
    on Windows); falls back to 'misc' when nothing usable remains.
    """
    ext = re.sub(r"[^a-z0-9_-]", "", filepath.suffix.lstrip(".").lower())
    return ext or "misc"


def is_hidden_or_special(path: Path) -> bool:
    """Return True for dotfiles, symlinks, Windows junctions, and Windows hidden/system files."""
    if path.name.startswith(".") or path.is_symlink():
        return True
    if hasattr(path, "is_junction") and path.is_junction():
        return True
    if os.name == "nt":
        try:
            attrs = path.stat().st_file_attributes
            return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
        except OSError:
            return True
    return False


def pick_unique_destination(
    desired: Path, reserved_keys: set[str], source_path: Path | None = None
) -> Path:
    """Return `desired` if free, otherwise append _1, _2, ... before the extension."""
    desired_key = normalize_path_key(desired)

    # Allow case-only renames (e.g., IMG.JPG -> IMG.jpg) on case-insensitive OSes.
    is_case_rename = (
        source_path is not None
        and desired_key == normalize_path_key(source_path)
        and desired_key not in reserved_keys
    )
    if is_case_rename or (desired_key not in reserved_keys and not desired.exists()):
        return desired

    counter = 1
    while True:
        candidate = desired.parent / f"{desired.stem}_{counter}{desired.suffix}"
        if normalize_path_key(candidate) not in reserved_keys and not candidate.exists():
            return candidate
        counter += 1


def find_eligible_files(source_dir: Path, recurse: bool = False) -> list[tuple[Path, os.stat_result]]:
    """Scan `source_dir` for non-hidden regular files, returning sorted (Path, stat) pairs."""
    collected: list[tuple[Path, os.stat_result]] = []
    queue = [source_dir]

    while queue:
        current = queue.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if is_hidden_or_special(path):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        logger.error("Could not read metadata for '%s': %s", path, exc)
                        continue

                    if stat.S_ISDIR(st.st_mode) and recurse:
                        queue.append(path)
                    elif stat.S_ISREG(st.st_mode):
                        collected.append((path, st))
        except OSError as exc:
            logger.error("Could not open directory '%s': %s", current, exc)

    return sorted(collected, key=lambda item: item[0])


def compute_sha256(filepath: Path, max_bytes: int | None = None) -> str:
    """Return SHA-256 hex digest of a file (or its first `max_bytes`), read in 1 MB chunks."""
    hasher = hashlib.sha256()
    remaining = max_bytes
    with filepath.open("rb") as f:
        while remaining is None or remaining > 0:
            chunk = f.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return hasher.hexdigest()


def group_identical_hashes(
    items: list[tuple[Path, os.stat_result]], max_bytes: int | None
) -> list[list[tuple[Path, os.stat_result]]]:
    """Group (Path, stat) items by SHA-256 digest, returning only duplicate groups (size 2+)."""
    buckets: dict[str, list[tuple[Path, os.stat_result]]] = {}
    for path, st in items:
        try:
            buckets.setdefault(compute_sha256(path, max_bytes), []).append((path, st))
        except OSError as exc:
            logger.error("Could not hash '%s': %s", path, exc)
    return [group for group in buckets.values() if len(group) > 1]


def remove_duplicates(
    files: list[tuple[Path, os.stat_result]], source_dir: Path, dry_run: bool
) -> tuple[list[tuple[Path, os.stat_result]], list[Path]]:
    """
    Move duplicate files into <source_dir>/.trash, keeping the oldest copy.
    Narrows candidates by byte size -> first 64 KB hash -> full SHA-256 hash.
    """
    by_size: dict[int, list[tuple[Path, os.stat_result]]] = {}
    for item in files:
        by_size.setdefault(item[1].st_size, []).append(item)

    trash_root = source_dir / TRASH_DIR_NAME
    reserved_keys: set[str] = set()
    trashed: list[Path] = []

    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        for quick in group_identical_hashes(same_size, QUICK_HASH_BYTES):
            for exact in group_identical_hashes(quick, None):
                exact.sort(key=lambda item: (item[1].st_mtime, str(item[0])))
                keeper = exact[0][0]

                for dup_path, _ in exact[1:]:
                    try:
                        target = pick_unique_destination(
                            trash_root / dup_path.relative_to(source_dir), reserved_keys
                        )
                        reserved_keys.add(normalize_path_key(target))
                        if dry_run:
                            logger.info("[DRY RUN] Duplicate: %s -> %s (keeping %s)", dup_path, target, keeper)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(dup_path), str(target))
                            logger.info("Trashed duplicate: %s -> %s (kept %s)", dup_path, target, keeper)
                        trashed.append(dup_path)
                    except OSError as exc:
                        logger.error("Failed to trash duplicate '%s': %s", dup_path, exc)

    trashed_set = set(trashed)
    return [item for item in files if item[0] not in trashed_set], trashed


def purge_empty_dirs(source_dir: Path, recurse: bool, dry_run: bool, gone: set[Path]) -> int:
    """Delete empty subdirectories under `source_dir` (deepest first when `recurse=True`)."""
    candidates: list[Path] = []
    if recurse:
        for root, dirnames, _ in os.walk(source_dir):
            root_path = Path(root)
            dirnames[:] = [d for d in dirnames if not is_hidden_or_special(root_path / d)]
            candidates.extend(root_path / d for d in dirnames)
        candidates.reverse()
    else:
        try:
            candidates = sorted(
                p for p in source_dir.iterdir() if p.is_dir() and not is_hidden_or_special(p)
            )
        except OSError as exc:
            logger.error("Failed to list '%s': %s", source_dir, exc)
            return 0

    removed = 0
    for folder in candidates:
        if not folder.is_dir() or is_hidden_or_special(folder):
            continue
        try:
            contents = list(folder.iterdir())
            if any(item not in gone and item.name.lower() not in IGNORABLE_FILES for item in contents):
                continue

            if dry_run:
                logger.info("[DRY RUN] Remove empty dir: %s", folder)
            else:
                for item in contents:
                    if item not in gone and item.name.lower() in IGNORABLE_FILES and not item.is_dir():
                        item.unlink()
                folder.rmdir()
                logger.info("Removed empty dir: %s", folder)

            gone.add(folder)
            removed += 1
        except OSError as exc:
            logger.error("Failed to remove directory '%s': %s", folder, exc)

    return removed


def move_file_safely(src: Path, dst: Path) -> None:
    """Move `src` to `dst`, handling case-only renames safely on macOS/Windows."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src != dst and normalize_path_key(src) == normalize_path_key(dst):
        # Two-step rename via a short temp name (avoids name-length limits).
        temp = src.parent / f".rename_{os.getpid()}.tmp"
        os.replace(src, temp)
        try:
            os.replace(temp, dst)
        except OSError:
            os.replace(temp, src)  # restore so no temp file is left behind
            raise
    else:
        shutil.move(str(src), str(dst))


def organize_files(
    source_dir: Path,
    dry_run: bool = False,
    recurse: bool = False,
    purge: bool = False,
    change_name: bool = False,
    dedupe: bool = False,
    sort: bool = True,
    extra_gone: set[Path] | None = None,
) -> dict[str, int]:
    """
    Scan `source_dir`, optionally dedupe, optionally move files into
    YYYY/MM/<ext> (`sort`), and optionally purge empty directories.
    """
    root = source_dir.expanduser().resolve()
    files: list[tuple[Path, os.stat_result]] = []
    duplicates: list[Path] = []

    if sort or dedupe:  # a purge-only run needs no file scan
        files = find_eligible_files(root, recurse=recurse)
        if files:
            logger.info("Found %d eligible file(s) in '%s'.", len(files), root)
        else:
            logger.info("No eligible files found in '%s'.", root)

    if dedupe:
        files, duplicates = remove_duplicates(files, root, dry_run)

    moved = skipped = in_place = 0
    reserved_keys: set[str] = set()
    moved_sources: set[Path] = set()

    for filepath, st in (files if sort else []):
        try:
            dt = datetime.fromtimestamp(st.st_mtime)
            name = re.sub(r"\s+", "_", filepath.name) if change_name else filepath.name
            # Derive the extension from the final name, not the original, or a
            # renamed file (e.g. "a.b c" -> "a.b_c") would move again next run.
            ext = safe_extension(Path(name))
            # Explicit zero-padding: strftime("%Y") differs across OSes for years < 1000.
            desired = root / f"{dt.year:04d}" / f"{dt.month:02d}" / ext / name

            if desired == filepath:
                in_place += 1
                reserved_keys.add(normalize_path_key(filepath))
                continue

            final_dest = pick_unique_destination(desired, reserved_keys, source_path=filepath)
            reserved_keys.add(normalize_path_key(final_dest))

            if dry_run:
                logger.info("[DRY RUN] %s -> %s", filepath, final_dest)
            else:
                move_file_safely(filepath, final_dest)
                logger.info("Moved: %s -> %s", filepath, final_dest)

            moved += 1
            moved_sources.add(filepath)
        except (OSError, OverflowError, ValueError) as exc:
            # OverflowError/ValueError: out-of-range timestamps (seen on Windows).
            logger.error("Failed to process '%s': %s", filepath, exc)
            skipped += 1

    gone = moved_sources | set(duplicates) | (extra_gone or set())
    purged = purge_empty_dirs(root, recurse, dry_run, gone) if purge else 0
    return {
        "moved": moved,
        "skipped": skipped,
        "in_place": in_place,
        "purged": purged,
        "duplicates": len(duplicates),
    }


def _is_link(path: Path) -> bool:
    """True for symlinks and Windows junctions (never followed or deleted)."""
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def find_trash_dirs(root: Path, recurse: bool) -> list[Path]:
    """
    Return the hidden trash directories under `root`, sorted.

    Only <root>/.trash unless `recurse`, which searches the whole tree
    (other hidden directories, symlinks and junctions are not entered,
    and trash directories are not searched inside).
    """
    found: list[Path] = []
    for current, dirnames, _ in os.walk(root):
        keep: list[str] = []
        for name in dirnames:
            path = Path(current) / name
            if _is_link(path):
                continue
            if name.casefold() == TRASH_DIR_NAME:
                found.append(path)
            elif not is_hidden_or_special(path):
                keep.append(name)
        dirnames[:] = keep if recurse else []
    return sorted(found)


def describe_dir(path: Path) -> str:
    """Return e.g. '3 file(s), 1.2 MB' for the files under `path`."""
    count = total = 0
    for current, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(current) / name).stat().st_size
                count += 1
            except OSError:
                pass  # unreadable entries just aren't counted
    size = float(total)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            break
        size /= 1024
    return f"{count} file(s), {size:.1f} {unit}"


def delete_trash_dirs(trash_dirs: list[Path], dry_run: bool) -> tuple[int, int]:
    """Permanently delete the given trash directories. Returns (deleted, failed)."""
    deleted = failed = 0
    for path in trash_dirs:
        try:
            summary = describe_dir(path)
            if dry_run:
                logger.info("[DRY RUN] Delete trash: %s (%s)", path, summary)
            else:
                shutil.rmtree(path)
                logger.info("Deleted trash: %s (%s)", path, summary)
            deleted += 1
        except OSError as exc:
            logger.error("Failed to delete '%s': %s", path, exc)
            failed += 1
    return deleted, failed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(description="Organize files into YYYY/MM/<ext> folders.")
    parser.add_argument("-d", "--directory", type=Path, required=True, help="Source directory.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files.")
    parser.add_argument("-r", "--recurse", action="store_true", help="Include subdirectories.")
    parser.add_argument(
        "-s", "--sort", action="store_true",
        help="Organize files into YYYY/MM/<ext> folders. This is also the "
        "default when no action (-s, -u, -p, -t) is given.",
    )
    parser.add_argument(
        "-p", "--purge", action="store_true",
        help="PERMANENTLY delete the .trash directories, then remove empty "
        "folders (preview with --dry-run).",
    )
    parser.add_argument("-c", "--change-name", action="store_true", help="Replace filename whitespace with '_' (with --sort).")
    parser.add_argument("-u", "--dedupe", action="store_true", help="Move duplicates to .trash (keeps oldest).")
    parser.add_argument(
        "-t", "--trash", action="store_true",
        help="List the hidden .trash directories (with -r, search subdirectories too).",
    )
    args = parser.parse_args(argv)

    # Legacy behavior: with no explicit action, organize files.
    if not (args.sort or args.dedupe or args.purge or args.trash):
        args.sort = True
    if args.change_name and not args.sort:
        parser.error("--change-name requires --sort.")
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args = parse_args(argv)
    source_dir = args.directory.expanduser().resolve()

    if not source_dir.is_dir():
        logger.error("'%s' is not a valid directory.", source_dir)
        return 1

    os_name = "macOS" if platform.system() == "Darwin" else platform.system()
    modes = [
        label for enabled, label in [
            (args.dry_run, "DRY RUN"),
            (args.recurse, "RECURSIVE"),
            (args.sort, "SORT"),
            (args.purge, "PURGE"),
            (args.change_name, "CHANGE NAME"),
            (args.dedupe, "DEDUPE"),
            (args.trash, "TRASH"),
        ] if enabled
    ]
    logger.info("OS: %s | Source: %s | Modes: %s", os_name, source_dir, ", ".join(modes))

    # Dry-run uses conditional wording ("Would move"), real runs past tense.
    dry = args.dry_run
    failures = 0

    # Purge deletes trash first, before this run can create any, so that
    # "-u -p" keeps the duplicates it trashes. It also lets directories that
    # held only trash count as empty in the purge step below.
    deleted_trash: set[Path] = set()
    if args.purge:
        trash_dirs = find_trash_dirs(source_dir, args.recurse)
        if not trash_dirs:
            logger.info("No %s directories to delete.", TRASH_DIR_NAME)
        _, failed = delete_trash_dirs(trash_dirs, args.dry_run)
        failures += failed
        deleted_trash = set(trash_dirs)

    if args.sort or args.dedupe or args.purge:
        results = organize_files(
            source_dir,
            dry_run=args.dry_run,
            recurse=args.recurse,
            purge=args.purge,
            change_name=args.change_name,
            dedupe=args.dedupe,
            sort=args.sort,
            extra_gone=deleted_trash,
        )
        failures += results["skipped"]
        if args.sort:
            logger.info(
                "Done. %s: %d | Skipped: %d | Already in place: %d",
                "Would move" if dry else "Moved",
                results["moved"], results["skipped"], results["in_place"],
            )
        if args.dedupe:
            logger.info(
                "%s duplicates: %d", "Would trash" if dry else "Trashed", results["duplicates"]
            )
        if args.purge:
            logger.info(
                "%s empty dirs: %d", "Would remove" if dry else "Removed", results["purged"]
            )

    # Listing runs last, so it shows what remains after everything above.
    if args.trash:
        trash_dirs = find_trash_dirs(source_dir, args.recurse)
        if not trash_dirs:
            logger.info("No %s directories found.", TRASH_DIR_NAME)
        for path in trash_dirs:
            logger.info("Trash: %s (%s)", path, describe_dir(path))

    return EXIT_PARTIAL if failures else 0


if __name__ == "__main__":
    sys.exit(main())
