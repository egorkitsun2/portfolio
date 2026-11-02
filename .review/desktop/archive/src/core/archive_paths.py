"""Проверка оглавления архива до извлечения на целевой файловой системе."""
from __future__ import annotations

import re
import os
import stat
import subprocess
import zipfile
import zlib
from pathlib import Path

from ..config import EXTRACTION_STATE_FILE
from ..utils.reporting import ReportCancelled, ReportWriteError


class UnsafeArchive(ValueError):
    """Оглавление содержит опасные либо неоднозначные пути."""


class UninspectableArchive(UnsafeArchive):
    """Повреждённое/защищённое оглавление нельзя прочитать; оригинал идёт в карантин."""


def _validate(entries):
    # zipfile/Win32 and 7-Zip can map trailing dots and spaces differently.
    # Check both mappings without rejecting an otherwise unambiguous name.
    paths = ({}, {})
    explicit = (set(), set())
    for name, is_dir, is_link in entries:
        if is_link:
            raise UnsafeArchive(f"Ссылка в архиве: {name!r}")
        if not name or name.startswith(('/', '\\')) or ':' in name or '\x00' in name:
            raise UnsafeArchive(f"Недопустимый путь: {name!r}")
        pieces = name.replace('\\', '/').split('/')
        if is_dir and pieces[-1] == '':
            pieces.pop()
        if not pieces or any(p in ('', '.', '..') for p in pieces):
            raise UnsafeArchive(f"Недопустимый путь: {name!r}")
        keys = ([], [])
        for piece in pieces:
            if piece.casefold() == EXTRACTION_STATE_FILE.casefold():
                raise UnsafeArchive(f"Зарезервированное служебное имя: {name!r}")
            cleaned = re.sub(r'[<>"|?*\x01-\x1f]', '_', piece)
            normalized = cleaned.rstrip(' .').casefold()
            if normalized == EXTRACTION_STATE_FILE.casefold():
                raise UnsafeArchive(f"Зарезервированное служебное имя: {name!r}")
            if not normalized or re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', normalized):
                raise UnsafeArchive(f"Недопустимое имя Windows: {name!r}")
            keys[0].append(normalized)
            keys[1].append(re.sub(r'[ .]+$', lambda m: '_' * len(m.group()), cleaned).casefold())
        for aliases, seen_paths, seen_explicit in zip(keys, paths, explicit):
            for i in range(1, len(aliases) + 1):
                key = tuple(aliases[:i])
                display = tuple(pieces[:i])
                kind = 'dir' if i < len(aliases) or is_dir else 'file'
                previous = seen_paths.get(key)
                if previous and (previous[0] != display or previous[1] != kind):
                    raise UnsafeArchive(f"Коллизия путей: {name!r} и {'/'.join(previous[0])!r}")
                if i == len(aliases):
                    if key in seen_explicit:
                        raise UnsafeArchive(f"Повтор записи: {name!r}")
                    seen_explicit.add(key)
                seen_paths[key] = (display, kind)


def _zip_entries(archive):
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.orig_filename != info.filename:
                raise UnsafeArchive(f'Недопустимое имя ZIP: {info.orig_filename!r}')
            mode = info.external_attr >> 16
            special = stat.S_IFMT(mode)
            link = (special not in (0, stat.S_IFREG, stat.S_IFDIR) or
                    bool(info.external_attr & 0x400))
            is_dir = info.is_dir() or info.filename.endswith('\\') or special == stat.S_IFDIR or bool(info.external_attr & 0x10)
            yield info.filename, is_dir, link


def _seven_records(sz, archive, audit=None):
    command = [sz, 'l', '-slt', '-ba', '-sccUTF-8', str(archive)]
    if audit:
        audit.event('archive_listing_started', command=command, archive=str(archive))
    result = subprocess.run(command,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            encoding='utf-8', errors='strict',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if audit:
        audit.event('archive_listing_result', command=command, returncode=result.returncode,
                    stdout=result.stdout[:65536], stderr=result.stderr[:65536],
                    stdout_chars=len(result.stdout), stderr_chars=len(result.stderr),
                    stdout_truncated=len(result.stdout) > 65536, stderr_truncated=len(result.stderr) > 65536)
    if result.returncode != 0:
        raise UninspectableArchive('Не удалось получить оглавление 7-Zip')
    blocks = re.split(r'\r?\n\s*\r?\n', result.stdout.strip('\r\n'))
    if not result.stdout.strip():
        raise UnsafeArchive('Пустое или нераспознанное оглавление 7-Zip')
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            if ' = ' not in line:
                raise UnsafeArchive('Неоднозначное оглавление 7-Zip')
            key, value = line.split(' = ', 1)
            if key in fields:
                raise UnsafeArchive('Повтор поля в оглавлении 7-Zip')
            fields[key] = value
        if 'Path' not in fields or 'Attributes' not in fields or 'Size' not in fields:
            raise UnsafeArchive('Неполное оглавление 7-Zip')
        attrs = fields['Attributes']
        is_dir = fields.get('Folder') == '+' or attrs.startswith('D')
        link = any('link' in key.casefold() or 'reparse' in key.casefold() for key in fields)
        link = link or bool(re.search(r'(?:^|\s)l[rwx-]{9}(?:\s|$)', attrs))
        yield fields, is_dir, link


def _seven_entries(sz, archive, audit=None):
    for fields, is_dir, link in _seven_records(sz, archive, audit):
        yield fields['Path'], is_dir, link


def check_archive_paths(archive: Path, sz: str | None, audit=None):
    try:
        # ZIP's central directory is authoritative even when extraction uses 7-Zip.
        if archive.suffix.lower() == '.zip' and not re.search(r'\.zip\.\d{3}$', archive.name, re.I):
            entries = _zip_entries(archive)
        elif sz:
            entries = _seven_entries(sz, archive, audit)
        else:
            raise UnsafeArchive('Нет средства проверки оглавления архива')
        def observed():
            for name, is_dir, is_link in entries:
                if audit:
                    audit.event('archive_entry', archive=str(archive), entry_name=name, is_directory=is_dir, is_link=is_link)
                yield name, is_dir, is_link
        _validate(observed())
        if audit:
            audit.event('archive_preflight_finished', archive=str(archive), passed=True)
    except (UnsafeArchive, ReportWriteError):
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        raise UninspectableArchive(f'Не удалось проверить оглавление: {exc}') from exc


def verify_extracted_contents(archive: Path, out_dir: Path, sz: str | None, cancel_event=None):
    """Сверить файлы и каталоги, размеры и доступные CRC с оглавлением.

    Возвращает количество файлов, проверенных по CRC и только по размеру.
    Отсутствие CRC не подтверждает целостность содержимого.
    """
    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise ReportCancelled()

    def mapped(name):
        # A ZIP without 7-Zip uses zipfile's platform-specific extraction rules.
        if sz or os.name == 'nt':
            pieces = name.replace('\\', '/').split('/')
            if os.name == 'nt':
                pieces = [re.sub(r'[<>"|?*\x01-\x1f]', '_', p) for p in pieces]
                if sz:
                    pieces = [re.sub(r'[ .]+$', lambda m: '_' * len(m.group()), p) for p in pieces]
                else:
                    pieces = [p.rstrip(' .') for p in pieces]
            return '/'.join(pieces).casefold()
        return name.casefold()

    try:
        cancelled()
        archive, out_dir = Path(archive), Path(out_dir)
        entries = []
        directory_names = []
        if archive.suffix.lower() == '.zip':
            with zipfile.ZipFile(archive) as zf:
                infos = zf.infolist()
                # Reuse preflight's ZIP link and directory interpretation.
                validated = list(_zip_entries(archive))
                _validate(validated)
                for info, (name, is_dir, _) in zip(infos, validated):
                    if is_dir:
                        directory_names.append(name)
                    else:
                        entries.append((name, info.file_size, info.CRC))
        elif sz:
            records = list(_seven_records(sz, archive))
            _validate((fields['Path'], is_dir, link) for fields, is_dir, link in records)
            for fields, is_dir, _ in records:
                if is_dir:
                    directory_names.append(fields['Path'])
                    continue
                if not re.fullmatch(r'\d+', fields['Size']):
                    raise UnsafeArchive(f"Некорректный размер: {fields['Path']!r}")
                crc = fields.get('CRC', '')
                if crc and not re.fullmatch(r'[0-9a-fA-F]{8}', crc):
                    raise UnsafeArchive(f"Некорректный CRC: {fields['Path']!r}")
                entries.append((fields['Path'], int(fields['Size']), int(crc, 16) if crc else None))
        else:
            raise UnsafeArchive('Нет средства проверки оглавления архива')
        expected = {}
        expected_dirs = set()
        def include_parents(key):
            pieces = key.split('/')
            expected_dirs.update('/'.join(pieces[:index]) for index in range(1, len(pieces)))
        for name in directory_names:
            key = mapped(name.rstrip('/\\'))
            expected_dirs.add(key)
            include_parents(key)
        for name, size, crc in entries:
            key = mapped(name)
            if key in expected:
                raise UnsafeArchive(f'Неоднозначное имя извлечённого файла: {name!r}')
            expected[key] = (size, crc)
            include_parents(key)
        actual = {}
        actual_dirs = set()
        def scan_error(exc):
            raise exc
        root_stat = out_dir.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or getattr(root_stat, 'st_file_attributes', 0) & 0x400:
            raise UnsafeArchive('Каталог извлечения недоступен или является ссылкой')
        for parent, dirs, files in os.walk(out_dir, followlinks=False, onerror=scan_error):
            cancelled()
            for name in dirs + files:
                path = Path(parent) / name
                state = path.lstat()
                if stat.S_ISLNK(state.st_mode) or getattr(state, 'st_file_attributes', 0) & 0x400:
                    raise UnsafeArchive(f'Ссылка в результатах распаковки: {path}')
                if name in dirs:
                    if not stat.S_ISDIR(state.st_mode):
                        raise UnsafeArchive(f'Недопустимый объект: {path}')
                    key = path.relative_to(out_dir).as_posix().casefold()
                    if key in actual_dirs:
                        raise UnsafeArchive(f'Неоднозначное имя каталога: {path}')
                    actual_dirs.add(key)
                    continue
                if not stat.S_ISREG(state.st_mode):
                    raise UnsafeArchive(f'Недопустимый объект: {path}')
                if path.parent == out_dir and name == EXTRACTION_STATE_FILE:
                    continue
                key = path.relative_to(out_dir).as_posix().casefold()
                if key in actual:
                    raise UnsafeArchive(f'Неоднозначное имя результата: {path}')
                actual[key] = path
        if expected.keys() != actual.keys():
            raise UnsafeArchive(f'Набор файлов не совпадает: отсутствуют {sorted(expected.keys() - actual.keys())}, лишние {sorted(actual.keys() - expected.keys())}')
        if expected_dirs != actual_dirs:
            raise UnsafeArchive(f'Набор каталогов не совпадает: отсутствуют {sorted(expected_dirs - actual_dirs)}, лишние {sorted(actual_dirs - expected_dirs)}')
        crc_verified = size_only = 0
        for key, (size, crc) in expected.items():
            cancelled()
            path = actual[key]
            before = path.stat()
            if before.st_size != size:
                raise UnsafeArchive(f'Размер извлечённого файла не совпадает: {path}')
            if crc is None:
                size_only += 1
                continue
            value = 0
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    cancelled()
                    value = zlib.crc32(chunk, value)
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or value != crc:
                raise UnsafeArchive(f'CRC извлечённого файла не совпадает: {path}')
            crc_verified += 1
        return {'files': len(expected), 'crc_verified': crc_verified, 'size_only': size_only}
    except (UnsafeArchive, ReportCancelled):
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        raise UnsafeArchive(f'Не удалось проверить извлечённые файлы: {exc}') from exc
