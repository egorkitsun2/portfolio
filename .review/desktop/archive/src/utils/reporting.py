from __future__ import annotations

import hashlib
import json
import os
import tempfile
import sys
import time
import traceback
import stat
from collections import Counter
from importlib.metadata import version, PackageNotFoundError
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ReportWriteError(OSError):
    """Не удалось сохранить сведения аудита."""


def _report_write_errors(method):
    """Отделить ошибки сохранения отчёта от ошибок чтения исходных файлов."""
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ReportWriteError:
            raise
        except OSError as exc:
            raise ReportWriteError(f'Не удалось записать отчёт: {exc}') from exc
    return wrapped


class ReportCancelled(Exception):
    """Вычисление хеша прервано вызывающей стороной."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def exception_details(exc):
    return {'exception_type': type(exc).__name__, 'error': str(exc),
            'traceback': ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}


def _fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def capture_file(path: Path, cancel_event=None) -> dict:
    """Зафиксировать размер, SHA-256 и время изменения стабильного файла."""
    path = Path(path).absolute()
    if cancel_event is not None and cancel_event.is_set():
        raise ReportCancelled()
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ReportCancelled()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    after = path.stat()
    if _fingerprint(before) != _fingerprint(after):
        raise OSError(f'Файл изменился при вычислении хеша: {path}')
    return {'path': str(path), 'size': after.st_size, 'sha256': digest.hexdigest(), 'mtime_ns': after.st_mtime_ns}


class RunReport:
    """Журнал событий и проверяемые контрольные снимки операции."""

    _DETAIL_EVENTS = frozenset({
        'archive_entry', 'archive_discovered', 'archive_hash_started', 'archive_hash_finished',
        'file_discovered', 'file_decision', 'date_candidate', 'source_hash_started', 'source_hash_finished',
        'file_processing_started', 'file_processing_finished', 'copy_progress',
        'verification_hash_started', 'verification_hash_finished', 'verification_check',
        'source_reconciled', 'inventory_excluded', 'empty_directory_cleanup_attempt',
        'duplicate_file_discovered', 'directory_filtered', 'path_filtered',
    })

    @_report_write_errors
    def __init__(self, base_dir: Path, operation: str, metadata=None, log_cb=None, report_cb=None, cancel_event=None):
        self.directory = Path(base_dir).absolute() / ('run_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid4().hex)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.report_path = self.directory / 'report.txt'
        self._log_cb, self._report_cb = log_cb, report_cb
        self._cancel_event = cancel_event
        self.detailed = os.environ.get('ARCHIVARIUS_DIAGNOSTICS') == '1'
        self.run_id = self.directory.name
        self._clock = time.monotonic()
        self._sequence = 0
        self._event_counts = Counter()
        self._outcomes = {}
        self._data = {'version': 2, 'run_id': self.run_id, 'operation': operation, 'metadata': metadata or {}, 'started_at': _now(), 'finished_at': None, 'status': 'running', 'actions': [], 'checks': [], 'issues': [], 'stages': [], 'inventories': [], 'exceptions': []}
        self._current = None
        (self.directory / 'run.log').touch()
        (self.directory / 'events.jsonl').touch()
        self.event('run_started', operation=operation, metadata=self._data['metadata'])
        packages = {}
        for name in ('customtkinter', 'ExifRead'):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = None
        source_root = Path(__file__).resolve().parents[1]
        sources = {}
        for path in sorted(source_root.rglob('*.py')) if self.detailed else ():
            try:
                sources[path.relative_to(source_root.parent).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                sources[str(path)] = {'error': str(exc)}
        self._data['runtime'] = {'python': sys.version, 'executable': sys.executable,
                                 'platform': sys.platform, 'windows_version': str(sys.getwindowsversion()) if hasattr(sys, 'getwindowsversion') else None, 'cwd': str(Path.cwd()),
                                 'pid': os.getpid(), 'packages': packages, 'source_sha256': sources,
                                 'diagnostics_mode': 'detailed' if self.detailed else 'standard',
                                 'local_timezone': str(datetime.now().astimezone().tzinfo)}
        self.event('runtime', **self._data['runtime'])
        self._write_text(self.directory / 'HOW_TO_READ.txt',
                         ('Режим: подробная диагностика.\n' if self.detailed else
                          'Режим: стандартный журнал безопасности; пошаговая диагностика отключена.\n') +
                         'Для анализа передайте весь каталог этого запуска.\n'
                         'report.json: параметры, среда, действия, проверки, исключения, снимки дерева.\n'
                         'events.jsonl: записанная последовательность событий; seq задаёт порядок, elapsed_ms — время от старта.\n'
                         'inventory_*.jsonl: пути, типы, размеры и времена файлов; содержимое не сохраняется.\n'
                         'run.log и report.txt: читаемая сводка. Смотрите ошибки, причины пропусков и незавершённые операции.\n'
                         'czkawka_cli.log и czkawka_result.json: полный вывод и исходный результат поиска; при отмене JSON может быть неполным.\n'
                         'running или отсутствие run_finished означает незавершённый запуск. Последнее событие — последняя записанная точка, а не доказательство завершения следующего действия.\n'
                         'Проверяются зарегистрированные операции. Снимок дерева не атомарен, не доказывает полноту распаковки или поиска дубликатов.\n'
                         'Логи содержат полные имена и пути файлов, но не их содержимое и не переменные окружения.\n')
        self._snapshot()

    def _write_text(self, path, content):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                             prefix='.report_', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _write_json(self, path, data):
        self._write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')

    def _text(self, data):
        statuses = {'running': 'Выполняется', 'completed': 'Завершено',
                    'completed_with_issues': 'Завершено с замечаниями',
                    'verification_failed': 'Ошибка проверки', 'failed': 'Ошибка',
                    'cancelled': 'Отменено', 'canceled': 'Отменено', 'not_applicable': 'Неприменимо'}
        counter_labels = {'processed': 'Обработано', 'moved': 'Перемещено',
                          'deleted': 'Удалено', 'skipped': 'Пропущено',
                          'errors': 'Ошибок', 'total': 'Всего', 'duplicates': 'Дубликатов',
                          'checked': 'Проверок', 'issues': 'Замечаний'}
        def status(value):
            return f"{statuses.get(value, value)} ({value})"
        def counters(values):
            return ', '.join(f"{counter_labels.get(k, k)}: {v}" for k, v in (values or {}).items()) or '—'
        def detail(value, indent=0):
            if isinstance(value, dict):
                return '\n'.join(' ' * indent + str(key) + ': ' + (('\n' + detail(item, indent + 2)) if isinstance(item, (dict, list)) else str(item)) for key, item in value.items()) or '—'
            if isinstance(value, list):
                return '\n'.join(' ' * indent + '- ' + detail(item, indent + 2) for item in value) or '—'
            return str(value)
        checks = data.get('checks', []) + data.get('verification', [])
        passed = sum(c['passed'] is True for c in checks)
        failed = sum(c['passed'] is False for c in checks)
        unchecked = sum(c['passed'] is None for c in checks)
        lines = [f"Операция: {self._data['operation']}", f"Статус: {status(data.get('status'))}",
                 f"Начало: {data.get('started_at')}", f"Завершение: {data.get('finished_at') or '—'}",
                 'Параметры операции:', detail(self._data['metadata'])]
        lines.extend(['', f'Идентификатор запуска: {self.run_id}',
                      'Подробная хронология: events.jsonl; инструкция: HOW_TO_READ.txt',
                      'Среда и версия исходников:', detail(self._data.get('runtime', {})),
                      'Снимки дерева файлов:', detail(self._data['inventories']),
                      'Исключения (полные стеки):', detail(self._data['exceptions'])])
        if 'name' in data:
            lines.extend([f"Этап: {data['name']}", 'Счётчики: ' + counters(data.get('counters'))])
        if data.get('explanation'):
            lines.append(data['explanation'])
        lines.extend(['', 'Этапы:'])
        for stage in data.get('stages', []):
            lines.append(f"- {stage['name']}: {status(stage['status'])}; {counters(stage.get('counters'))}")
        lines.extend(['', 'Действия:'])
        for action in data.get('actions', []):
            lines.append(detail(action))
        lines.extend(['', f'Проверки: пройдено {passed}, ошибок {failed}, не проверено {unchecked}'])
        if not checks:
            lines.append('Проверки отсутствуют; корректность не подтверждена.')
        for check in checks:
            label = 'пройдено' if check['passed'] is True else 'ошибка' if check['passed'] is False else 'не проверено'
            lines.append(f"- {check['name']}: {label}\n{detail(check.get('details'))}")
        lines.extend(['', 'Проблемы:'])
        for issue in data.get('issues', []):
            lines.append(detail(issue))
        return '\n'.join(lines) + '\n'

    def _snapshot(self):
        self._data['diagnostics'] = {'last_seq': self._sequence, 'event_counts': dict(self._event_counts),
                                     'elapsed_ms': round((time.monotonic() - self._clock) * 1000, 3)}
        self._write_json(self.directory / 'report.json', self._data)
        self._write_text(self.report_path, self._text(self._data))
        self._write_json(self.report_path.with_suffix(".summary.json"),
                         {"status": self._data["status"], "operation": self._data["operation"],
                          "report_path": str(self.report_path), "finished_at": self._data["finished_at"],
                          "counters": {"actions": len(self._data["actions"]), "issues": len(self._data["issues"])}})

    @_report_write_errors
    def event(self, name, **details):
        # Учёт причин нужен для сверки источника даже без записи подробной трассировки.
        if details.get('path') and (name in ('file_processing_finished', 'file_skipped', 'archive_skipped', 'archive_processing_finished', 'extraction_directory_blocked') or name == 'file_discovered' and not details.get('eligible')):
            self._outcomes[os.path.normcase(str(Path(details['path']).absolute()))] = {'event': name, **details}
        if (not self.detailed and name in self._DETAIL_EVENTS
                and not (name == 'source_reconciled' and details.get('passed') is not True)):
            return
        sequence = self._sequence + 1
        # Не переиспользовать номер, если строка записалась, но fsync дал ошибку.
        self._sequence = sequence
        entry = {'seq': sequence, 'run_id': self.run_id, 'elapsed_ms': round((time.monotonic() - self._clock) * 1000, 3), 'timestamp': _now(), 'event': name, 'stage': self._current['name'] if self._current else None, 'details': details}
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
            stream.flush()
            if name in ('planned_move', 'planned_delete', 'source_delete_started', 'move_source_deleted', 'exception', 'run_finished'):
                os.fsync(stream.fileno())
        self._event_counts[name] += 1
        if name == 'exception':
            self._data['exceptions'].append(details)

    @_report_write_errors
    def exception(self, exc, **details):
        record = dict(details, **exception_details(exc))
        self.event('exception', **record)
        self._write_log_line(record['traceback'], True)

    @_report_write_errors
    def inventory(self, root, label, exclude_names=('_Archivarius_logs',)):
        """Потоковый снимок метаданных; ссылки и reparse-каталоги не обходим."""
        if not self.detailed and label not in ('source_before', 'source_after'):
            return None
        root = Path(root).absolute()
        filename = f'inventory_{len(self._data["inventories"]) + 1:02d}.jsonl'
        summary = {'label': label, 'root': str(root), 'file': filename, 'started_at': _now(),
                   'files': 0, 'directories': 0, 'links': 0, 'bytes': 0, 'errors': 0,
                   'complete': False, 'excluded_names': list(exclude_names),
                   'scope': 'metadata_only; not_atomic; no_symlink_traversal'}
        self._data['inventories'].append(summary)
        self.event('inventory_started', **summary)
        def failed(exc):
            summary['errors'] += 1
            self.exception(exc, operation='inventory', root=str(root), label=label)
        with (self.directory / filename).open('w', encoding='utf-8') as stream:
            def row(path):
                try:
                    state = path.lstat()
                except OSError as exc:
                    failed(exc)
                    return False
                link = stat.S_ISLNK(state.st_mode) or bool(getattr(state, 'st_file_attributes', 0) & 0x400)
                kind = 'link' if link else 'directory' if stat.S_ISDIR(state.st_mode) else 'file' if stat.S_ISREG(state.st_mode) else 'special'
                record = {'path': str(path), 'type': kind, 'size': state.st_size,
                          'mtime_ns': state.st_mtime_ns, 'ctime_ns': state.st_ctime_ns}
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                stream.flush()
                if kind == 'file':
                    summary['files'] += 1
                    summary['bytes'] += state.st_size
                elif kind == 'directory':
                    summary['directories'] += 1
                elif kind == 'link':
                    summary['links'] += 1
                return kind == 'directory'
            if os.path.lexists(root):
                if row(root):
                    for parent, dirs, files in os.walk(root, onerror=failed, followlinks=False):
                        if self._cancel_event and self._cancel_event.is_set():
                            summary['cancelled'] = True
                            break
                        for name in list(dirs):
                            if self._cancel_event and self._cancel_event.is_set():
                                summary['cancelled'] = True
                                dirs.clear()
                                break
                            if name in exclude_names:
                                self.event('inventory_excluded', path=str(Path(parent) / name), reason='diagnostic_directory', label=label)
                                dirs.remove(name)
                            elif not row(Path(parent) / name):
                                dirs.remove(name)
                        for name in files:
                            if self._cancel_event and self._cancel_event.is_set():
                                summary['cancelled'] = True
                                break
                            row(Path(parent) / name)
                else:
                    summary['not_directory'] = True
            else:
                summary['absent'] = True
        summary.update(finished_at=_now(), complete=not summary['errors'] and not summary.get('cancelled'))
        self.event('inventory_finished', **summary)
        self.check('Снимок дерева: ' + label, None if summary.get('cancelled') else summary['complete'], summary.copy())
        return summary

    @_report_write_errors
    def reconcile_source(self, before, after, interrupted=False):
        """Сопоставить исходный список с действиями и явными причинами пропусков."""
        def files(snapshot):
            with (self.directory / snapshot['file']).open(encoding='utf-8') as stream:
                return {os.path.normcase(row['path']): row for line in stream
                        if (row := json.loads(line))['type'] == 'file'}
        initial, remaining = files(before), files(after)
        actions = {os.path.normcase(action.get('source', action.get('path', ''))): action for action in self._data['actions']}
        problems = 0
        for key, item in initial.items():
            evidence = self._outcomes.get(key)
            if evidence is None:
                for parent in Path(item['path']).parents:
                    blocked = self._outcomes.get(os.path.normcase(str(parent)))
                    if blocked and blocked['event'] == 'extraction_directory_blocked':
                        evidence = blocked
                        break
            if key in actions:
                outcome, passed = 'recorded_action', True
            elif key not in remaining:
                outcome, passed = 'unrecorded_disappearance', False
            elif evidence:
                outcome, passed = 'retained_with_reason', True
            else:
                outcome, passed = 'not_processed', None if interrupted else False
            if key in remaining and any(item[field] != remaining[key][field] for field in ('size', 'mtime_ns')):
                outcome, passed = 'source_metadata_changed', False
            if not before['complete'] or not after['complete']:
                passed = None
            if passed is not True:
                problems += 1
            self.event('source_reconciled', path=item['path'], outcome=outcome, passed=passed,
                       before=item, after=remaining.get(key), decision=evidence)
        for key in remaining.keys() - initial.keys():
            evidence = self._outcomes.get(key)
            if evidence is None:
                for parent in Path(remaining[key]['path']).parents:
                    blocked = self._outcomes.get(os.path.normcase(str(parent)))
                    if blocked and blocked['event'] == 'extraction_directory_blocked':
                        evidence = blocked
                        break
            passed = True if evidence else None if interrupted else False
            if not before['complete'] or not after['complete']:
                passed = None
            if passed is not True:
                problems += 1
            self.event('source_reconciled', path=remaining[key]['path'],
                       outcome='new_file_with_reason' if evidence else 'untracked_new_file',
                       passed=passed, after=remaining[key], decision=evidence)
        complete = before['complete'] and after['complete']
        self.check('Учёт исходных файлов', None if interrupted or not complete else problems == 0,
                   {'initial_files': len(initial), 'remaining_files': len(remaining), 'unresolved': problems,
                    'before': before['file'], 'after': after['file'],
                    'scope': 'paths_and_metadata; unchanged_content_of_skipped_files_not_hashed'})

    def _write_log_line(self, message, is_error=False):
        stamp = _now()
        level = 'ERROR' if is_error else 'INFO'
        with (self.directory / 'run.log').open('a', encoding='utf-8') as stream:
            stream.write(f'{stamp} [{level}] {message}\n')
            stream.flush()

    @_report_write_errors
    def log(self, message, is_error=False):
        level = 'ERROR' if is_error else 'INFO'
        self._write_log_line(message, is_error)
        self.event('log', message=str(message), severity=level)
        if self._log_cb:
            self._log_cb(message, is_error)

    @_report_write_errors
    def start_stage(self, name):
        if self._current is not None:
            raise RuntimeError('Предыдущий этап ещё не завершён')
        stage = {'name': name, 'number': len(self._data['stages']) + 1, 'status': 'running', 'started_at': _now(), 'finished_at': None, 'counters': None, 'action_start': len(self._data['actions']), 'check_start': len(self._data['checks'])}
        self._data['stages'].append(stage)
        self._current = stage
        self._write_log_line(f'Начат этап: {name}')
        self.event('stage_started', stage=name, stage_record=stage)
        self._snapshot()

    @_report_write_errors
    def record_move(self, source: Path, destination: Path, expected: dict, keeper: dict | None = None):
        self._data['actions'].append({'type': 'move', 'source': str(Path(source).absolute()), 'destination': str(Path(destination).absolute()), 'expected': dict(expected), 'keeper': dict(keeper) if keeper is not None else None})
        self.event('move_recorded', index=len(self._data['actions']) - 1, action=self._data['actions'][-1])

    @_report_write_errors
    def record_delete(self, path, expected: dict):
        self._data['actions'].append({'type': 'delete', 'path': str(Path(path).absolute()), 'expected': dict(expected), 'limitation': 'Отсутствие файла не подтверждает полноту извлечения архива.'})
        self.event('delete_recorded', index=len(self._data['actions']) - 1, action=self._data['actions'][-1])

    @_report_write_errors
    def check(self, name, passed: bool | None, details=None):
        if passed is not None and not isinstance(passed, bool):
            raise TypeError('passed must be bool or None')
        self._data['checks'].append({'name': name, 'passed': passed, 'details': details, 'timestamp': _now()})
        self.event('check_recorded', check=self._data['checks'][-1])

    def _verify(self, actions):
        checks = []
        cache = {}
        def digest(path):
            if path not in cache:
                self.event('verification_hash_started', path=path)
                try:
                    cache[path] = capture_file(Path(path), self._cancel_event)
                except ReportCancelled:
                    cache[path] = {"cancelled": True, "reason": "Проверка содержимого остановлена пользователем"}
                except (OSError, ValueError) as exc:
                    self.exception(exc, operation='verification_hash', path=path)
                    cache[path] = {'error': str(exc)}
                self.event('verification_hash_finished', path=path, actual=cache[path])
            return cache[path]
        for action in actions:
            first_check = len(checks)
            if action['type'] == 'delete':
                path = action['path']
                checks.append({'name': 'delete_absent', 'passed': not os.path.lexists(path), 'details': {'path': path, 'limitation': action['limitation']}})
                self.event('verification_check', check=checks[-1])
                continue
            source, dest = action['source'], action['destination']
            checks.append({'name': 'move_source_absent', 'passed': not os.path.lexists(source), 'details': {'path': source}})
            actual = digest(dest)
            expected = action['expected']
            checks.append({'name': 'move_destination_digest', 'passed': None if actual.get('cancelled') else 'error' not in actual and actual.get('size') == expected.get('size') and actual.get('sha256') == expected.get('sha256') and expected.get('size') is not None and bool(expected.get('sha256')), 'details': {'path': dest, 'expected': expected, 'actual': actual}})
            keeper = action.get('keeper')
            if keeper is not None:
                keeper_path = keeper.get('path')
                actual_keeper = digest(str(keeper_path)) if keeper_path else {'error': 'Не указан путь сохранённого файла'}
                checks.append({'name': 'keeper_digest', 'passed': None if actual_keeper.get('cancelled') else 'error' not in actual_keeper and bool(keeper.get('sha256')) and actual_keeper.get('sha256') == keeper.get('sha256'), 'details': {'path': keeper_path, 'expected': keeper, 'actual': actual_keeper}})
            for check in checks[first_check:]:
                self.event('verification_check', check=check)
        return checks

    @_report_write_errors
    def end_stage(self, name, counters=None, status='completed'):
        stage = self._current
        if stage is None or stage['name'] != name:
            raise ValueError('Нет соответствующего активного этапа')
        actions = self._data['actions'][stage['action_start']:]
        checks = self._data['checks'][stage['check_start']:] + self._verify(actions)
        issues = [c for c in checks if c['passed'] is not True]
        if status == 'completed' and any(c['passed'] is False for c in checks):
            status = 'verification_failed'
        elif status == 'completed' and issues:
            status = 'completed_with_issues'
        stage.update(status=status, counters=counters, finished_at=_now(), checks=checks, issues=issues)
        stage_doc = dict(stage, actions=actions)
        prefix = f"stage_{stage['number']:02d}"
        self._write_json(self.directory / (prefix + '.json'), stage_doc)
        self._write_text(self.directory / (prefix + '.txt'), self._text(stage_doc))
        self._current = None
        self._write_log_line(f'Завершён этап: {name}; статус: {status}; проверок: {len(checks)}, замечаний: {len(issues)}')
        self.event('stage_finished', stage=name, status=status, stage_record=stage)
        self._snapshot()

    @_report_write_errors
    def finish(self, status='completed'):
        if self._current is not None:
            self.end_stage(self._current['name'], status=status if status != 'completed' else 'completed')
        final_started = _now()
        self._write_log_line('Начат этап: Итоговая проверка')
        verification = self._verify(self._data['actions'])
        final_issues = [c for c in verification if c['passed'] is not True]
        final_status = ('not_applicable' if not self._data['actions'] else
                        'verification_failed' if any(c['passed'] is False for c in verification) else
                        'completed_with_issues' if final_issues else 'completed')
        final_stage = {'name': 'Итоговая проверка', 'number': len(self._data['stages']) + 1,
                       'scope': 'all_recorded_actions', 'action_start': 0, 'status': final_status,
                       'started_at': final_started, 'finished_at': _now(),
                       'counters': {'checked': len(verification), 'issues': len(final_issues)},
                       'checks': verification, 'issues': final_issues}
        if final_status == 'not_applicable':
            final_stage['explanation'] = 'Нет зарегистрированных действий для повторной проверки файлов.'
        self._data['stages'].append(final_stage)
        prefix = f"stage_{final_stage['number']:02d}"
        stage_doc = dict(final_stage, actions=self._data['actions'])
        self._write_json(self.directory / (prefix + '.json'), stage_doc)
        self._write_text(self.directory / (prefix + '.txt'), self._text(stage_doc))
        self._write_log_line(f'Завершён этап: Итоговая проверка; статус: {final_status}; проверок: {len(verification)}, замечаний: {len(final_issues)}')
        self.event('stage_finished', stage=final_stage['name'], status=final_status, stage_record=final_stage)
        checks = self._data['checks'] + verification
        issues = [c for c in checks if c['passed'] is not True]
        failed_stages = [s for s in self._data['stages'] if s['status'] in ('failed', 'verification_failed')]
        if status == 'completed' and self._cancel_event and self._cancel_event.is_set():
            status = 'cancelled'
        if status == 'completed':
            if any(c['passed'] is False for c in checks) or failed_stages:
                status = 'verification_failed'
            elif not checks or issues or any(s['status'] not in ('completed', 'not_applicable') for s in self._data['stages']):
                status = 'completed_with_issues'
        self._data.update(status=status, finished_at=_now(), verification=verification, issues=issues)
        self._write_log_line(f'Завершена операция; статус: {status}; проверок: {len(checks)}, замечаний: {len(issues)}')
        self._snapshot()
        self.event('run_finished', status=status, snapshot_last_seq=self._sequence)
        if self._report_cb:
            self._report_cb(self.report_path)
