"""Оркестратор: проверка путей, запуск распаковки и сортировки, откат."""
from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path

from ..config import AUDIO_DIR, DOCS_DIR, JUNK_DIR, LOGS_DIR, MEDIA_DIR, OTHER_DIR
from ..utils.file_ops import cleanup_empty_dirs, is_inside
from ..utils.journal import Journal, find_latest_journal, undo_journal
from ..utils.reporting import RunReport
from .context import Context, Options
from .sorter import sort_files
from .unpacker import unpack_archives


def validate_paths(src: str, tgt: str, quar: str):
    """Возвращает ((src, tgt, quar) как Path | None, список ошибок)."""
    if not all(s and s.strip() for s in (src, tgt, quar)):
        return None, ["Укажите все три папки."]

    s, t, q = (Path(x.strip()).expanduser().resolve() for x in (src, tgt, quar))
    errors = []
    if not s.is_dir():
        errors.append("Источник не существует или не является папкой.")
    if is_inside(s, t) and is_inside(t, s) or is_inside(s, q) and is_inside(q, s) \
            or is_inside(t, q) and is_inside(q, t):
        errors.append("Три папки должны быть разными.")
    else:
        if is_inside(t, s):
            errors.append("Назначение не должно лежать внутри источника, иначе уже "
                          "отсортированные файлы будут обработаны повторно.")
        if is_inside(q, s):
            errors.append("Карантин не должен лежать внутри источника.")
    if is_inside(s, t) and s != t:
        errors.append("Источник не должен лежать внутри назначения: каталоги сортировки могут оказаться в источнике.")
    if is_inside(s, q) and s != q:
        errors.append("Источник не должен лежать внутри карантина.")
    return (None, errors) if errors else ((s, t, q), [])


def run_pipeline(src: Path, tgt: Path, quar: Path, options: Options,
                 log_cb, progress_cb, done_cb, cancel_event=None, report_cb=None) -> threading.Thread:
    """Обрабатывает файлы, сохраняет отчёты этапов и проверяет результат."""
    cancel_event = cancel_event or threading.Event()
    src, tgt, quar = (Path(path).expanduser().resolve() for path in (src, tgt, quar))

    def _worker():
        ctx = audit = None
        journal = None
        initial_source = None
        status = "completed"
        try:
            _, path_errors = validate_paths(str(src), str(tgt), str(quar))
            if path_errors:
                raise ValueError(" ".join(path_errors))
            journal = Journal(None)
            if not options.dry_run:
                logs = tgt / LOGS_DIR
                audit = RunReport(logs, "Распаковка и сортировка",
                                  metadata={"source": str(src), "target": str(tgt), "quarantine": str(quar),
                                            "options": asdict(options),
                                            "scope": "SHA-256 перемещённых файлов; список, размеры и CRC извлечённых файлов сверяются с оглавлением до подтверждения распаковки"},
                                  log_cb=log_cb, report_cb=report_cb, cancel_event=cancel_event)
                journal = Journal(logs)
                audit.event("movement_journal", path=str(journal.path))
                audit.event('paths_validated', source=str(src), target=str(tgt), quarantine=str(quar), options=asdict(options))
                initial_source = audit.inventory(src, 'source_before')
                audit.inventory(tgt, 'target_before')
                audit.inventory(quar, 'quarantine_before')
            ctx = Context(src, tgt, quar, options, log_cb, progress_cb,
                          cancel_event, journal, audit=audit)
            if options.dry_run:
                ctx.log("[ПРОБНЫЙ РЕЖИМ] Файлы и настройки не изменяются; отчёты на диск не записываются. "
                        "Содержимое архивов в пробном режиме не просматривается.")
            ctx.log("[СИСТЕМА] Фаза распаковки...")
            if audit:
                audit.start_stage("Распаковка")
            ctx.progress(0)
            u = unpack_archives(ctx)
            if audit:
                audit.check("Этап распаковки завершён без операционных ошибок", u["errors"] == 0,
                            {"counters": u, "cancelled": ctx.cancelled})
                stage_status = "cancelled" if ctx.cancelled else ("completed_with_issues" if any(u[k] for k in ("warning", "quarantined", "skipped", "errors")) else "completed")
                audit.end_stage("Распаковка", u, stage_status)
            s = {"moved": 0, "skipped": 0, "errors": 0, "junk": 0}
            if not ctx.cancelled:
                ctx.log("[СИСТЕМА] Фаза сортировки...")
                if audit:
                    audit.start_stage("Сортировка")
                ctx.progress(0)
                s = sort_files(ctx)
                if audit:
                    audit.check("Этап сортировки завершён без операционных ошибок", s["errors"] == 0,
                                {"counters": s, "cancelled": ctx.cancelled})
                    audit.end_stage("Сортировка", s, "cancelled" if ctx.cancelled else "completed")
            if ctx.cancelled:
                status = "cancelled"
            ctx.log(f"[ИТОГ] Архивы: распаковано {u['ok']}, предупреждений {u['warning']}, "
                    f"в карантине {u['quarantined']}, пропущено {u['skipped']}, ошибок {u['errors']} | "
                    f"Файлы: {'будет перемещено' if options.dry_run else 'перемещено'} {s['moved']}, "
                    f"пропущено {s['skipped']}, служебных {s['junk']}, ошибок {s['errors']}")
            if journal.path:
                ctx.log(f"[ИНФО] Журнал перемещений: {journal.path}")
        except Exception as exc:
            status = "failed"
            log_cb(f"[FATAL ERROR] Обработка не завершена: {exc}", True)
            if audit:
                try:
                    audit.exception(exc, operation='pipeline')
                    audit.check("Обработка завершена", False, {"error": str(exc)})
                    audit.log(f"[ОШИБКА] {exc}", True)
                except OSError:
                    pass
        finally:
            try:
                if ctx:
                    ctx.close()
                elif journal:
                    journal.close()
            except OSError as exc:
                status = "failed"
                log_cb(f"[ОШИБКА] Закрытие журнала: {exc}", True)
            try:
                if audit:
                    final_source = audit.inventory(src, 'source_after')
                    audit.inventory(tgt, 'target_after')
                    audit.inventory(quar, 'quarantine_after')
                    if initial_source:
                        audit.reconcile_source(initial_source, final_source, interrupted=status != 'completed' or cancel_event.is_set())
                    audit.log(f"[ОТЧЁТ] Итоговая проверка и отчёт: {audit.report_path}")
                    audit.finish(status)
            except Exception as exc:
                log_cb(f"[ОШИБКА ОТЧЁТА] Итоговый отчёт не удалось завершить: {exc}", True)
                if audit:
                    try:
                        audit.exception(exc, operation='final_diagnostics')
                        audit.finish('failed')
                    except Exception as report_exc:
                        log_cb(f"[ОШИБКА ОТЧЁТА] Повторное сохранение: {report_exc}; журнал: {audit.directory}", True)
            finally:
                done_cb()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def run_undo(tgt: Path, log_cb, done_cb, cancel_event=None, report_cb=None) -> threading.Thread:
    """Откатывает сортировку и сохраняет независимый отчёт восстановления."""
    cancel_event = cancel_event or threading.Event()

    def _worker():
        audit = None
        status = "completed"
        try:
            audit = RunReport(tgt / LOGS_DIR, "Откат сортировки", metadata={"target": str(tgt)},
                              log_cb=log_cb, report_cb=report_cb, cancel_event=cancel_event)
            audit.inventory(tgt, 'undo_target_before')
            audit.start_stage("Восстановление файлов")
            journal = find_latest_journal(tgt / LOGS_DIR)
            if journal is None:
                audit.log("[ОТКАТ] Журналов для отката не найдено.", True)
                audit.check("Активные перемещения отсутствуют", True)
                audit.end_stage("Восстановление файлов", {"restored": 0, "missing": 0, "errors": 0})
                return
            audit.log(f"[ОТКАТ] Журнал: {journal.name}")
            audit.event("movement_journal", path=str(journal))
            restored, missing, errors = undo_journal(journal, audit.log, cancel_event, audit=audit)
            for name in (MEDIA_DIR, DOCS_DIR, AUDIO_DIR, OTHER_DIR, JUNK_DIR):
                cleanup_empty_dirs(tgt / name, audit=audit)
            status = "cancelled" if cancel_event.is_set() else "completed"
            audit.check("Восстановление завершено без пропусков и ошибок", missing == errors == 0,
                        {"restored": restored, "missing": missing, "errors": errors})
            audit.end_stage("Восстановление файлов", {"restored": restored, "missing": missing, "errors": errors}, status)
            audit.log(f"[ОТКАТ] Возвращено: {restored}; не найдено: {missing}; ошибок: {errors}.")
            if missing or errors or cancel_event.is_set():
                audit.log("[ОТКАТ] Для продолжения устраните причину и повторите откат; "
                          "уже восстановленные файлы повторно не обрабатываются.", True)
        except Exception as exc:
            status = "failed"
            log_cb(f"[FATAL ERROR] Откат не завершён: {exc}", True)
            if audit:
                try:
                    audit.exception(exc, operation='undo_pipeline')
                    audit.check("Откат завершён", False, {"error": str(exc)})
                    audit.log(str(exc), True)
                except OSError:
                    pass
        finally:
            try:
                if audit:
                    audit.inventory(tgt, 'undo_target_after')
                    audit.log(f"[ОТЧЁТ] Проверка восстановления: {audit.report_path}")
                    audit.finish(status)
            except Exception as exc:
                log_cb(f"[ОШИБКА ОТЧЁТА] Итоговый отчёт не удалось завершить: {exc}", True)
                if audit:
                    try:
                        audit.exception(exc, operation='final_undo_diagnostics')
                        audit.finish('failed')
                    except Exception as report_exc:
                        log_cb(f"[ОШИБКА ОТЧЁТА] Повторное сохранение: {report_exc}; журнал: {audit.directory}", True)
            finally:
                done_cb()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread
