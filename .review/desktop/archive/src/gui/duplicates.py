"""Отдельная вкладка поиска дубликатов; виджеты обновляет главный поток."""
from __future__ import annotations

import os
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from ..core.duplicates import (ScanCancelled, get_czkawka_path, move_duplicates,
                               scan_duplicates, undo_duplicates)
from ..config import DUPLICATES_DIR


@dataclass(frozen=True)
class DuplicateEvent:
    kind: str
    value: object


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} Б"
        value /= 1024


class DuplicatePanel(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.result = None
        self.keepers = {}
        self._rows = {}
        self._idle_controls = []
        self._page = 0
        self._file_pages = {}
        self._render_id = None
        self._render_generation = 0
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        self._build_inputs()
        self._build_results()
        self.set_busy(False)

    def _button(self, parent, text, command, **kwargs):
        button = ctk.CTkButton(parent, text=text, command=command, **kwargs)
        self._idle_controls.append(button)
        return button

    def _build_inputs(self):
        folders = ctk.CTkFrame(self)
        folders.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        folders.columnconfigure(0, weight=1)
        ctk.CTkLabel(folders, text="Папки для поиска точных копий").grid(row=0, column=0, sticky="w", padx=10)
        self.roots = tk.Listbox(folders, height=3, selectmode="extended", bg="#242424",
                                fg="#eeeeee", selectbackground="#1f6aa5", borderwidth=0,
                                exportselection=False, font=("Segoe UI", 10))
        self.roots.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        buttons = ctk.CTkFrame(folders, fg_color="transparent")
        buttons.grid(row=0, column=1, rowspan=2, padx=8, pady=8)
        for text, command in (("Добавить папку", self.add_folder),
                              ("Взять назначение", self.add_target), ("Убрать выбранные", self.remove_folders)):
            self._button(buttons, text, command, width=155).pack(pady=3)
        paths = ctk.CTkFrame(self)
        paths.grid(row=1, column=0, sticky="ew", padx=6)
        paths.columnconfigure(1, weight=1)
        self.cli_var = tk.StringVar(value=get_czkawka_path() or "")
        target = self.app.tgt_var.get().strip()
        self.storage_var = tk.StringVar(value=str(Path(target) / DUPLICATES_DIR) if target else "")
        for row, label, var, command in ((0, "Czkawka CLI:", self.cli_var, self.browse_cli),
                                         (1, "Хранилище копий:", self.storage_var, self.browse_storage)):
            ctk.CTkLabel(paths, text=label).grid(row=row, column=0, padx=8, pady=5, sticky="w")
            entry = ctk.CTkEntry(paths, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", padx=4, pady=5)
            self._idle_controls.append(entry)
            self._button(paths, "Обзор", command, width=70).grid(row=row, column=2, padx=8, pady=5)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        self.scan_button = self._button(actions, "Найти дубликаты", self.start_scan, width=155)
        self.scan_button.pack(side="left", padx=(0, 8))
        self.stop_button = ctk.CTkButton(actions, text="Стоп", command=self.cancel, width=75,
                                        fg_color="#a93226", hover_color="#7b241c")
        self.stop_button.pack(side="left")
        self.undo_button = self._button(actions, "Отменить перенос", self.start_undo, width=160)
        self.undo_button.pack(side="right")
        self.summary = ctk.CTkLabel(self, text="Выберите папки и запустите поиск. Исходные файлы при поиске не меняются.",
                                    anchor="w", wraplength=740, justify="left")
        self.summary.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 5))

    def _build_results(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=4, column=0, sticky="nsew", padx=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Duplicates.Treeview", background="#242424", foreground="#eeeeee",
                        fieldbackground="#242424", borderwidth=0, rowheight=30,
                        font=("Segoe UI", 10))
        style.configure("Duplicates.Treeview.Heading", background="#343434", foreground="#eeeeee",
                        relief="flat", font=("Segoe UI", 10))
        style.map("Duplicates.Treeview.Heading", background=[("active", "#444444")])
        style.map("Duplicates.Treeview", background=[("selected", "#1f6aa5")],
                  foreground=[("selected", "white")])
        self.tree = ttk.Treeview(frame, columns=("size", "action", "path"),
                                 selectmode="browse", style="Duplicates.Treeview")
        self.tree.heading("#0", text="Группа / файл")
        self.tree.heading("size", text="Размер")
        self.tree.heading("action", text="Действие")
        self.tree.heading("path", text="Полный путь")
        self.tree.column("#0", width=200, minwidth=120)
        self.tree.column("size", width=80, minwidth=70, stretch=False)
        self.tree.column("action", width=100, minwidth=100, stretch=False)
        self.tree.column("path", width=420, minwidth=160)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        pages = ctk.CTkFrame(frame, fg_color="transparent")
        pages.grid(row=2, column=0, sticky="ew")
        self.prev_page_button = self._button(pages, "← Группы", lambda: self.change_page(-1), width=100)
        self.prev_page_button.pack(side="left")
        self.page_label = ctk.CTkLabel(pages, text="Группы: 0")
        self.page_label.pack(side="left", padx=8)
        self.next_page_button = self._button(pages, "Группы →", lambda: self.change_page(1), width=100)
        self.next_page_button.pack(side="left")
        self.tree.bind("<Double-1>", self._file_page_click)
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=5, column=0, sticky="ew", padx=6, pady=6)
        self.keep_button = self._button(controls, "Оставить выбранный", self.keep_selected, width=160)
        self.keep_button.pack(side="left", padx=(0, 5))
        self.skip_button = self._button(controls, "Пропустить группу", self.skip_selected, width=145)
        self.skip_button.pack(side="left", padx=5)
        self._button(controls, "Открыть папку", self.open_selected_folder, width=125).pack(side="left", padx=5)
        self.move_button = self._button(controls, "Переместить копии", self.start_move, width=160)
        self.move_button.pack(side="right")
        self.status = ctk.CTkLabel(self, text="По умолчанию в группе сохраняется первая копия; выбор можно изменить.",
                                   wraplength=740, anchor="w", justify="left")
        self.status.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 3))
        self.progress = ctk.CTkProgressBar(self, mode="indeterminate")
        self.progress.grid(row=7, column=0, sticky="ew", padx=10, pady=(0, 8))
        self.progress.set(0)

    def add_path(self, path):
        path = Path(path).expanduser().absolute()
        if not path.is_dir():
            raise ValueError("Папка для поиска не существует")
        if str(path) not in self.roots.get(0, "end"):
            self.roots.insert("end", str(path))
        if not self.storage_var.get():
            self.storage_var.set(str(path / DUPLICATES_DIR))

    def add_folder(self):
        folder = filedialog.askdirectory(parent=self)
        if folder:
            self.add_path(folder)

    def add_target(self):
        try:
            value = self.app.tgt_var.get().strip()
            if not value:
                raise ValueError("Укажите папку назначения на вкладке «Сортировка»")
            self.add_path(value)
        except ValueError as exc:
            messagebox.showerror("Папки для поиска", str(exc), parent=self)

    def remove_folders(self):
        for index in reversed(self.roots.curselection()):
            self.roots.delete(index)

    def browse_cli(self):
        path = filedialog.askopenfilename(parent=self, title="Выберите czkawka_cli")
        if path:
            self.cli_var.set(path)

    def browse_storage(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            folder = Path(path)
            self.storage_var.set(str(folder if folder.name == DUPLICATES_DIR else folder / DUPLICATES_DIR))

    def storage_path(self):
        value = self.storage_var.get().strip()
        if not value:
            raise ValueError("Укажите папку для хранения копий")
        path = Path(value).expanduser().absolute()
        if path.name != DUPLICATES_DIR:
            path /= DUPLICATES_DIR
            self.storage_var.set(str(path))
        return path

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for control in self._idle_controls:
            control.configure(state=state)
        self.roots.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.move_button.configure(state="normal" if not busy and self.result and self.keepers else "disabled")
        self.keep_button.configure(state="normal" if not busy and self.result else "disabled")
        self.skip_button.configure(state="normal" if not busy and self.result else "disabled")
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0)

    def cancel(self):
        self.app._cancel.set()
        self.stop_button.configure(state="disabled")
        self.status.configure(text="Останавливаю текущую операцию…")

    def _start(self, label, operation, kind):
        self.app._cancel.clear()
        self.app._set_busy(True)
        self.status.configure(text=label, text_color=("gray10", "gray90"))
        def worker():
            try:
                value = operation()
                self.app._queue.put(DuplicateEvent(kind, value))
            except ScanCancelled:
                self.app._queue.put(DuplicateEvent("cancelled", None))
            except Exception as exc:
                self.app._queue.put(DuplicateEvent("error", str(exc)))
            finally:
                self.app._on_worker_done()
        threading.Thread(target=worker, daemon=True).start()

    def _cancel_render(self):
        self._render_generation += 1
        if self._render_id is not None:
            try:
                self.after_cancel(self._render_id)
            except tk.TclError:
                pass
            self._render_id = None

    def destroy(self):
        self._cancel_render()
        super().destroy()

    def clear_results(self):
        self._cancel_render()
        self._page = 0
        self._file_pages.clear()
        self.page_label.configure(text="Группы: 0")
        self.result = None
        self.keepers.clear()
        self._rows.clear()
        self.tree.delete(*self.tree.get_children())

    def start_scan(self):
        if self.app._busy:
            return
        roots = [Path(value) for value in self.roots.get(0, "end")]
        if not roots:
            messagebox.showerror("Поиск дубликатов", "Добавьте хотя бы одну папку", parent=self)
            return
        executable = self.cli_var.get().strip() or None
        quarantine = self.app.quar_var.get().strip()
        excluded = [Path(quarantine).expanduser().resolve()] if quarantine else []
        self.clear_results()
        self.summary.configure(text="Ищу точные совпадения содержимого…")
        self._start("Поиск дубликатов. Можно остановить сканирование.",
                    lambda: scan_duplicates(roots, excluded=excluded, executable=executable,
                                            cancel_event=self.app._cancel, log_cb=self.app.log,
                                            report_cb=self.app._on_report_ready), "scan")

    def selected_file(self):
        selected = self.tree.selection()
        return self._rows.get(selected[0]) if selected else None

    def keep_selected(self):
        if self.app._busy:
            return
        selected = self.selected_file()
        if selected:
            group, path = selected
            self.keepers[group] = path
            self.refresh_group(group)
            self.set_busy(False)

    def skip_selected(self):
        if self.app._busy:
            return
        selected = self.tree.selection()
        if selected:
            item = selected[0]
            if item.startswith("more"):
                return
            group = self._rows[item][0] if item in self._rows else int(item[1:])
            self.keepers.pop(group, None)
            self.refresh_group(group)
            self.set_busy(False)

    def refresh_group(self, group):
        keeper = self.keepers.get(group)
        if self.tree.exists(f"g{group}"):
            self.tree.set(f"g{group}", "path", str(keeper) if keeper else "Группа пропущена")
        for item in self.tree.get_children(f"g{group}"):
            if item not in self._rows:
                continue
            _, path = self._rows[item]
            self.tree.set(item, "action", "Пропустить" if keeper is None else
                          ("Оставить" if path == keeper else "Переместить"))

    def open_selected_folder(self):
        selected = self.selected_file()
        if selected:
            folder = selected[1].parent
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                import subprocess
                import sys
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])

    def start_move(self):
        if self.app._busy or not self.result or not self.keepers:
            return
        try:
            storage = self.storage_path()
        except ValueError as exc:
            messagebox.showerror("Перенос копий", str(exc), parent=self)
            return
        count = sum(len(self.result.groups[index].files) - 1 for index in self.keepers)
        if not messagebox.askyesno("Перенос копий", f"Переместить {count} копий в:\n{storage}\n\n"
                                   "Выбранные оригиналы останутся на местах. Перенос можно отменить.", parent=self):
            return
        result, keepers = self.result, dict(self.keepers)
        self._start("Повторно сравниваю файлы и переношу подтверждённые копии…",
                    lambda: move_duplicates(result, keepers, storage, cancel_event=self.app._cancel,
                                            log_cb=self.app.log, report_cb=self.app._on_report_ready), "move")

    def start_undo(self):
        if self.app._busy:
            return
        try:
            storage = self.storage_path()
        except ValueError as exc:
            messagebox.showerror("Откат копий", str(exc), parent=self)
            return
        if not messagebox.askyesno("Откат копий", "Вернуть файлы последнего переноса из выбранного хранилища?", parent=self):
            return
        self._start("Возвращаю копии на прежние места…",
                    lambda: undo_duplicates(storage, log_cb=self.app.log, cancel_event=self.app._cancel,
                                            report_cb=self.app._on_report_ready), "undo")

    def change_page(self, delta):
        if not self.result:
            return
        new_page = self._page + delta
        if 0 <= new_page < (len(self.result.groups) + 99) // 100:
            self._page = new_page
            self._render_page()

    def _file_page_click(self, event):
        item = self.tree.identify_row(event.y)
        if item.startswith("more"):
            group = int(item[4:])
            self._file_pages[group] = (self._file_pages.get(group, 0) + 1) % ((len(self.result.groups[group].files) + 19) // 20)
            self._render_page()
            return "break"

    def _render_page(self):
        self._cancel_render()
        self._rows.clear()
        self.tree.delete(*self.tree.get_children())
        groups = self.result.groups
        if not groups:
            self.page_label.configure(text="Группы: 0")
            return
        start, end = self._page * 100, min((self._page + 1) * 100, len(groups))
        self.page_label.configure(text=f"Группы {start + 1}–{end} из {len(groups)}")
        generation = self._render_generation
        def rows():
            for group in range(start, end):
                matches = groups[group]
                keeper = self.keepers.get(group)
                parent = self.tree.insert("", "end", iid=f"g{group}", text=f"Группа {group + 1}",
                    values=(format_size(matches.files[0].size), f"Файлов: {len(matches.files)}", str(keeper) if keeper else "Группа пропущена"), open=True)
                yield
                offset = self._file_pages.get(group, 0) * 20
                keeper = self.keepers.get(group)
                for index in range(offset, min(offset + 20, len(matches.files))):
                    entry = matches.files[index]
                    keeper = self.keepers.get(group)
                    action = "Пропустить" if keeper is None else ("Оставить" if entry.path == keeper else "Переместить")
                    item = self.tree.insert(parent, "end", iid=f"f{group}_{index}", text=entry.path.name,
                        values=(format_size(entry.size), action, str(entry.path)))
                    self._rows[item] = (group, entry.path)
                    yield
                if len(matches.files) > 20:
                    self.tree.insert(parent, "end", iid=f"more{group}", text=f"Файлы {offset + 1}–{min(offset + 20, len(matches.files))} из {len(matches.files)}; далее (двойной щелчок)")
                    yield
        iterator = rows()
        def batch():
            if generation != self._render_generation:
                return
            self._render_id = None
            try:
                for _ in range(100):
                    next(iterator)
            except StopIteration:
                return
            self._render_id = self.after(1, batch)
        batch()

    def handle_event(self, event):
        if event.kind == "scan":
            if self.app._cancel.is_set():
                self.handle_event(DuplicateEvent("cancelled", None))
                return
            self.clear_results()
            self.result = event.value
            self.keepers = {group: matches.files[0].path for group, matches in enumerate(self.result.groups)}
            self._render_page()
            self.summary.configure(text=f"Групп: {len(self.result.groups)}. Объём повторных копий: "
                                   f"{format_size(self.result.reclaimable_bytes)}.")
            self.status.configure(text="Выберите сохраняемые копии или пропустите группы. "
                                  "Перенос в хранилище сам по себе место на диске не освобождает.")
        elif event.kind == "move":
            value = event.value
            self.clear_results()
            self.summary.configure(text="Результаты сброшены после переноса. Для новой операции повторите поиск.")
            prefix = "Перенос остановлен. " if self.app._cancel.is_set() else ""
            self.status.configure(text=f"{prefix}Перемещено: {value.moved}; пропущено: {value.skipped}; ошибок: {value.errors}.")
        elif event.kind == "undo":
            self.clear_results()
            restored, missing, errors = event.value
            self.summary.configure(text="После отката повторите поиск для актуальных результатов.")
            prefix = "Откат остановлен; для продолжения повторите отмену переноса. " if self.app._cancel.is_set() else ""
            self.status.configure(text=f"{prefix}Возвращено: {restored}; не найдено: {missing}; ошибок: {errors}.")
        elif event.kind == "cancelled":
            self.clear_results()
            self.summary.configure(text="Операция остановлена. Для актуальных результатов повторите поиск.")
            self.status.configure(text="Уже выполненные переносы можно вернуть кнопкой «Отменить перенос».")
        elif event.kind == "error":
            self.clear_results()
            self.summary.configure(text="Операция завершилась с ошибкой. Подробности — в журнале.")
            self.status.configure(text=str(event.value), text_color="#ef7777")
            self.app.log(f"[ДУБЛИКАТЫ] {event.value}", True)
