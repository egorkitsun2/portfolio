"""Операции с файлами: уникальные имена, перемещение, очистка пустых папок."""
from __future__ import annotations

import os
import hashlib
import stat
import shutil
import time
from uuid import uuid4
from pathlib import Path

from .reporting import ReportCancelled, ReportWriteError, capture_file, exception_details


def unique_dest(folder: Path, filename: str) -> Path:
    """Свободный путь в folder: file.ext → file-1.ext → file-2.ext → ..."""
    dest = folder / filename
    if not os.path.lexists(dest):
        return dest

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        dest = folder / f"{stem}-{counter}{suffix}"
        if not os.path.lexists(dest):
            return dest
        counter += 1


def unique_dir(parent: Path, name: str) -> Path:
    """Свободное имя папки: name → name-1 → name-2 ..."""
    path = parent / name
    counter = 1
    while os.path.lexists(path):
        path = parent / f"{name}-{counter}"
        counter += 1
    return path


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _has_link(path):
    for part in (path, *path.parents):
        try:
            state = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(state.st_mode) or getattr(state, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _same_owner(path, reserved):
    try:
        current = path.lstat()
        return (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino)
    except OSError:
        return False


def move_file(src: Path, dest_folder: Path, dry_run: bool = False, audit=None, expected=None,
              *, filename=None, cancel_event=None, on_reserved=None, journal=None, kind="sort") -> Path:
    """Перенос без перезаписи: xb → проверенная копия → удаление источника.

    Намерение журнала сохраняется до копирования. Если запись завершения позже
    не удастся, журнал может восстановить действие по неизменяемому хешу.
    """
    src, dest_folder = Path(src).absolute(), Path(dest_folder).absolute()
    transfer_id = uuid4().hex
    def event(name, **details):
        if audit:
            audit.event(name, transfer_id=transfer_id, source=str(src), **details)
    name = filename or src.name
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("Недопустимое имя назначения")
    if dry_run:
        return unique_dest(dest_folder, name)
    if cancel_event and cancel_event.is_set():
        raise ReportCancelled()
    if _has_link(src) or _has_link(dest_folder) or not src.is_file():
        raise OSError("Перенос ссылок, специальных файлов и каталогов не поддерживается")
    event('move_started', requested_destination=str(dest_folder / name), kind=kind)
    try:
        expected = dict(expected) if expected is not None else capture_file(src, cancel_event)
        before = src.stat()
        if before.st_size != expected.get("size"):
            raise OSError(f"Размер исходного файла изменился: {src}")
        if expected.get("mtime_ns") is not None and before.st_mtime_ns != expected["mtime_ns"]:
            raise OSError(f"Время изменения исходного файла изменилось: {src}")
        if not expected.get("sha256"):
            raise ValueError("Для безопасного переноса требуется SHA-256")
        dest_folder.mkdir(parents=True, exist_ok=True)
        if _has_link(dest_folder):
            raise OSError("Папка назначения заменена ссылкой")
        for _attempt in range(128):
            target = unique_dest(dest_folder, name)
            if audit:
                event("planned_move", destination=str(target), expected=expected,
                      renamed=target.name != name, reason='name_collision' if target.name != name else 'requested_name')
            try:
                out = target.open("xb")
                break
            except FileExistsError:
                event('destination_collision', destination=str(target), reason='reserved_by_another_writer')
                continue
        else:
            raise FileExistsError("Не удалось зарезервировать назначение без перезаписи")
    except BaseException as exc:
        if audit:
            audit.event('exception', **exception_details(exc), operation='move_prepare', transfer_id=transfer_id, source=str(src), requested_destination=str(dest_folder / name))
        raise
    reserved = os.fstat(out.fileno())
    removed = False
    try:
        with out:
            event('destination_reserved', destination=str(target))
            if journal is not None:
                try:
                    journal.prepare_move(kind, src, target, expected)
                except OSError as exc:
                    raise ReportWriteError("Не удалось сохранить намерение перемещения") from exc
            if on_reserved:
                on_reserved(target, expected)
            digest = hashlib.sha256()
            copied = 0
            last_progress = time.monotonic()
            event('copy_started', destination=str(target), expected=expected)
            with src.open("rb") as stream:
                if _identity(os.fstat(stream.fileno())) != _identity(before):
                    raise OSError("Исходник заменён перед копированием")
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise ReportCancelled()
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    written = out.write(chunk)
                    if written != len(chunk):
                        raise OSError("Неполная запись копии")
                    digest.update(chunk)
                    copied += len(chunk)
                    if time.monotonic() - last_progress >= 5:
                        event('copy_progress', destination=str(target), bytes_copied=copied, total_bytes=expected['size'])
                        last_progress = time.monotonic()
                if _identity(os.fstat(stream.fileno())) != _identity(before):
                    raise OSError("Исходник изменился во время копирования")
            out.flush()
            os.fsync(out.fileno())
        event('copy_finished', destination=str(target), bytes_copied=copied, sha256=digest.hexdigest())
        if copied != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise OSError("Содержимое исходника изменилось после планирования")
        shutil.copystat(src, target)
        event('destination_verification_started', destination=str(target))
        actual = capture_file(target, cancel_event)
        event('destination_verification_finished', destination=str(target), expected=expected, actual=actual,
              passed=actual['size'] == expected['size'] and actual['sha256'] == expected['sha256'])
        if actual["size"] != expected["size"] or actual["sha256"] != expected["sha256"]:
            raise OSError("Записанная копия не прошла проверку SHA-256")
        if not _same_owner(target, reserved) or _has_link(src) or _identity(src.stat()) != _identity(before):
            raise OSError("Файл заменён перед завершением переноса")
        if cancel_event and cancel_event.is_set():
            raise ReportCancelled()
        event('source_delete_started', destination=str(target), reason='copy_sha256_verified')
        src.unlink()
        removed = True
        event('move_source_deleted', destination=str(target))
        return target
    except BaseException as exc:
        # Не удалять чужой файл либо последнюю доступную копию данных.
        cleanup = 'retained'
        if not removed and os.path.lexists(src) and _same_owner(target, reserved):
            try:
                target.unlink()
                cleanup = 'partial_copy_removed'
            except OSError as cleanup_exc:
                cleanup = str(cleanup_exc)
        if audit:
            audit.event('exception', **exception_details(exc), operation='move', transfer_id=transfer_id, source=str(src),
                            destination=str(target), source_deleted=removed, cleanup=cleanup)
        raise


def cleanup_empty_dirs(root: Path, logger_callback=None, audit=None, skip=None) -> None:
    """Удаляет пустые подпапки внутри root (сам root остаётся)."""
    if not root.is_dir():
        return
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root or skip and skip(path):
            continue
        try:
            if audit:
                audit.event('empty_directory_cleanup_attempt', path=str(path))
            path.rmdir()
        except ReportWriteError:
            raise
        except OSError as exc:
            if audit:
                audit.event('empty_directory_retained', path=str(path), reason=str(exc), errno=exc.errno)
            continue  # не пустая
        if audit:
            audit.event('empty_directory_removed', path=str(path))
        if logger_callback:
            logger_callback(f"[ОЧИСТКА] Удалена пустая папка: {path.relative_to(root)}")


def is_inside(child: Path, parent: Path) -> bool:
    """True, если child == parent или лежит внутри parent (с учётом регистра ОС)."""
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent)).rstrip(os.sep)
    return c == p or c.startswith(p + os.sep)
