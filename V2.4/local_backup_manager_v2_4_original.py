#!/usr/bin/env python3
"""
LHBM - Local Hosted Backups Management (single-file version)
==============================================================
Aplikasi desktop independen (Linux/Mac) untuk memantau perubahan
file/folder secara otomatis (track) dan menyimpan backup sebagai
arsip .zip bertimestamp ke folder tujuan (destination), sehingga
bisa direcall (dipulihkan) ke titik waktu mana pun.

Alur pemakaian (sesuai README):

Cara Track:
    1. Tambahkan file/folder yang mau dipantau (source)
    2. Tentukan folder tujuan backup (destination)
    3. Atur interval cek (per detik/menit, sesuai pilihan) dan durasi
       tracking total (dalam jam, 0 = tanpa batas/sampai di-stop manual)
    4. Selesai - LHBM otomatis membuat .zip baru setiap kali ada
       file yang berubah/ditambah/dihapus

Cara Recall:
    1. Cari folder backup (destination) kamu
    2. Buka folder "zips" di dalamnya
    3. Pilih titik waktu (timestamp) mana yang mau direcall
    4. Ambil (copy) zip tersebut, atau langsung Extract lewat LHBM
    5. Unzip / selesai

Cara jalankan:
    python3 local_backup_manager.py

Tidak butuh library eksternal - hanya Python standard library + Tkinter.
Kalau tkinter belum ada:
    sudo apt install python3-tk      (Ubuntu/Debian)
    sudo dnf install python3-tkinter (Fedora)
    brew install python-tk           (macOS Homebrew)

Config tersimpan otomatis di ~/.config/local-backup-manager/config.json
"""

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import List, Optional

APP_VERSION = "2.1"

# ============================================================
# CONFIG: penyimpanan job ke JSON
# ============================================================

CONFIG_DIR = Path.home() / ".config" / "local-backup-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
ZIPS_DIRNAME = "zips"


@dataclass
class BackupJob:
    id: str
    name: str
    source: str              # file/folder yang dipantau (track)
    destination: str         # folder tujuan backup
    interval_value: int = 30     # angka interval cek (sesuai satuan di bawah)
    interval_unit: str = "detik"  # "detik" atau "menit"
    duration_hours: float = 0.0   # total durasi tracking dalam jam, 0 = tanpa batas
    retention: int = 10      # jumlah zip lama disimpan per job
    mirror_delete: bool = False
    check_mode: str = "quick"  # "quick" (mtime+size) atau "hash" (sha256)
    excludes: list = field(default_factory=list)
    enabled: bool = False

    def interval_seconds(self) -> int:
        multiplier = 60 if self.interval_unit == "menit" else 1
        return max(1, int(self.interval_value) * multiplier)

    def interval_display(self) -> str:
        return f"{self.interval_value} {self.interval_unit}"

    def duration_display(self) -> str:
        return "Tanpa batas" if not self.duration_hours or self.duration_hours <= 0 else f"{self.duration_hours:g} jam"

    def zips_dir(self) -> Path:
        # Folder aman dari nama job (hindari karakter aneh di path)
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in self.name) or self.id
        return Path(self.destination) / ZIPS_DIRNAME / f"{safe_name}_{self.id}"


def new_job_id() -> str:
    return str(uuid.uuid4())[:8]


def _migrate_job_dict(d: dict) -> dict:
    """Konversi config lama (field 'interval' dalam detik) ke skema baru
    interval_value + interval_unit, supaya job lama tidak hilang saat update."""
    d = dict(d)
    if "interval" in d and "interval_value" not in d:
        old_seconds = d.pop("interval")
        try:
            old_seconds = int(old_seconds)
        except (TypeError, ValueError):
            old_seconds = 30
        if old_seconds % 60 == 0 and old_seconds >= 60:
            d["interval_value"] = old_seconds // 60
            d["interval_unit"] = "menit"
        else:
            d["interval_value"] = old_seconds
            d["interval_unit"] = "detik"
    d.pop("interval", None)
    d.setdefault("duration_hours", 0.0)
    return d


def load_jobs() -> List[BackupJob]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [BackupJob(**_migrate_job_dict(d)) for d in raw]
    except (json.JSONDecodeError, TypeError, ValueError):
        backup_path = CONFIG_FILE.with_suffix(".json.bak")
        try:
            CONFIG_FILE.replace(backup_path)
        except OSError:
            pass
        return []


def save_jobs(jobs: List[BackupJob]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = [asdict(j) for j in jobs]
    tmp_file = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp_file.replace(CONFIG_FILE)


# ============================================================
# ENGINE: thread worker pemantau (track) + backup (zip)
# ============================================================

class JobRunner(threading.Thread):
    """
    Memantau `job.source` setiap `job.interval_seconds()` detik. Begitu ada file
    yang berubah/ditambah/dihapus, seluruh source di-zip apa adanya
    (snapshot) ke `<destination>/zips/<job>/<timestamp>.zip`.
    """

    def __init__(self, job: BackupJob, log_callback, status_callback):
        super().__init__(daemon=True)
        self.job = job
        self.log = log_callback
        self.status_callback = status_callback
        self._stop_event = threading.Event()
        self._state = {}
        self._first_scan = True

    def stop(self):
        self._stop_event.set()

    def run(self):
        interval_s = self.job.interval_seconds()
        self.log(f"[{self.job.name}] Tracking dimulai (setiap {self.job.interval_display()}, "
                  f"durasi {self.job.duration_display()})")
        self.status_callback(self.job.id, "running")
        start_time = time.monotonic()
        duration_limit = self.job.duration_hours * 3600 if self.job.duration_hours and self.job.duration_hours > 0 else None
        try:
            while not self._stop_event.is_set():
                if duration_limit is not None and (time.monotonic() - start_time) >= duration_limit:
                    self.log(f"[{self.job.name}] Durasi tracking {self.job.duration_display()} tercapai, berhenti otomatis")
                    break
                try:
                    self._scan_and_backup()
                except FileNotFoundError:
                    self.log(f"[{self.job.name}] Source tidak ditemukan, dicoba lagi nanti")
                except Exception as e:
                    self.log(f"[{self.job.name}] ERROR: {e}")
                self._stop_event.wait(interval_s)
        finally:
            self.status_callback(self.job.id, "stopped")
            self.log(f"[{self.job.name}] Tracking dihentikan")

    def _is_excluded(self, relpath: str) -> bool:
        return any(fnmatch.fnmatch(relpath, p) for p in self.job.excludes)

    def _file_signature(self, path: Path):
        st = os.stat(path)
        if self.job.check_mode == "hash":
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        return (st.st_mtime, st.st_size)

    def _current_files(self, src: Path):
        """Return list of (full_path, relpath) currently under source, minus excludes."""
        if src.is_file():
            return [(src, src.name)]
        files = []
        for root, _dirs, filenames in os.walk(src):
            for fn in filenames:
                full = Path(root) / fn
                rel = str(full.relative_to(src))
                if self._is_excluded(rel):
                    continue
                files.append((full, rel))
        return files

    def _scan_and_backup(self):
        src = Path(self.job.source)
        if not src.exists():
            raise FileNotFoundError(str(src))

        files_to_check = self._current_files(src)

        current_rels = set()
        changed = False
        deleted_rels = []

        for full_path, rel in files_to_check:
            current_rels.add(rel)
            try:
                sig = self._file_signature(full_path)
            except (FileNotFoundError, PermissionError):
                continue
            if self._state.get(rel) != sig:
                changed = True
            self._state[rel] = sig

        removed = set(self._state.keys()) - current_rels
        if removed:
            deleted_rels = sorted(removed)
            for rel in removed:
                del self._state[rel]
            if self.job.mirror_delete:
                changed = True
            # Even without mirror_delete, a deletion is still a change worth
            # tracking, since the file no longer exists at that point in time.
            changed = True

        if self._first_scan:
            # Selalu buat snapshot awal supaya ada titik recall paling lama.
            changed = True
            self._first_scan = False

        if not changed:
            return

        self._make_zip_snapshot(src, files_to_check, deleted_rels)

    def _make_zip_snapshot(self, src: Path, files_to_check, deleted_rels):
        zips_dir = self.job.zips_dir()
        zips_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = zips_dir / f"{ts}.zip"

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for full_path, rel in files_to_check:
                    try:
                        zf.write(full_path, arcname=rel)
                    except (FileNotFoundError, PermissionError) as e:
                        self.log(f"[{self.job.name}] Lewati {rel}: {e}")
        except OSError as e:
            self.log(f"[{self.job.name}] Gagal membuat zip: {e}")
            return

        note = f"Backup baru: {zip_path.name}"
        if deleted_rels:
            note += f" (terdeteksi {len(deleted_rels)} file dihapus)"
        self.log(f"[{self.job.name}] {note}")

        self._prune_zips(zips_dir)

    def _prune_zips(self, zips_dir: Path):
        if self.job.retention <= 0:
            return
        zips = sorted(zips_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
        excess = len(zips) - self.job.retention
        for old in zips[:max(0, excess)]:
            try:
                old.unlink()
            except OSError:
                pass


# ============================================================
# GUI
# ============================================================

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"LHBM - Local Hosted Backups Management v{APP_VERSION}")
        root.geometry("1000x580")
        root.minsize(780, 440)

        self.jobs = load_jobs()
        self.runners = {}

        self._build_ui()
        self._refresh_tree()

    def _build_ui(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=8, pady=6)

        ttk.Button(toolbar, text="+ Tambah Job", command=self.add_job).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Edit Job", command=self.edit_job).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Hapus Job", command=self.remove_job).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="▶ Start", command=self.start_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="■ Stop", command=self.stop_selected).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Start Semua", command=self.start_all).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Stop Semua", command=self.stop_all).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="⟲ Recall Backup...", command=self.open_recall).pack(side="left", padx=2)

        columns = ("name", "source", "destination", "interval", "duration", "status", "last")
        headers = {
            "name": "Nama", "source": "Source", "destination": "Destination",
            "interval": "Interval Cek", "duration": "Durasi Tracking",
            "status": "Status", "last": "Update Terakhir",
        }
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=9)
        for c in columns:
            self.tree.heading(c, text=headers[c])
            width = 220 if c in ("source", "destination") else 110
            self.tree.column(c, width=width, anchor="w")
        self.tree.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(self.root, text="Log Aktivitas:").pack(anchor="w", padx=8)
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.log_text = tk.Text(log_frame, height=14, state="disabled", wrap="word",
                                 bg="#111318", fg="#d7e0ea", insertbackground="#d7e0ea")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")

        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{ts}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, append)

    def status_callback(self, job_id, status):
        self.root.after(0, self._refresh_tree)

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for job in self.jobs:
            running = job.id in self.runners and self.runners[job.id].is_alive()
            status = "Running" if running else "Stopped"
            last = time.strftime("%H:%M:%S") if running else "-"
            self.tree.insert("", "end", iid=job.id,
                              values=(job.name, job.source, job.destination,
                                       job.interval_display(), job.duration_display(), status, last))

    def _selected_job(self):
        sel = self.tree.selection()
        if not sel:
            return None
        job_id = sel[0]
        return next((j for j in self.jobs if j.id == job_id), None)

    def add_job(self):
        dlg = JobDialog(self.root)
        self.root.wait_window(dlg.top)
        if dlg.result:
            self.jobs.append(dlg.result)
            save_jobs(self.jobs)
            self._refresh_tree()

    def edit_job(self):
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Info", "Pilih job dulu di tabel")
            return
        if job.id in self.runners and self.runners[job.id].is_alive():
            messagebox.showwarning("Peringatan", "Stop job ini dulu sebelum diedit")
            return
        dlg = JobDialog(self.root, job)
        self.root.wait_window(dlg.top)
        if dlg.result:
            idx = self.jobs.index(job)
            self.jobs[idx] = dlg.result
            save_jobs(self.jobs)
            self._refresh_tree()

    def remove_job(self):
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Info", "Pilih job dulu di tabel")
            return
        if job.id in self.runners and self.runners[job.id].is_alive():
            messagebox.showwarning("Peringatan", "Stop job ini dulu sebelum dihapus")
            return
        if messagebox.askyesno("Konfirmasi", f"Hapus job '{job.name}'? (zip backup yang sudah ada tidak dihapus)"):
            self.jobs.remove(job)
            save_jobs(self.jobs)
            self._refresh_tree()

    def start_selected(self):
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Info", "Pilih job dulu di tabel")
            return
        self._start_job(job)

    def stop_selected(self):
        job = self._selected_job()
        if job:
            self._stop_job(job)

    def start_all(self):
        for job in self.jobs:
            self._start_job(job)

    def stop_all(self):
        for job in self.jobs:
            self._stop_job(job)

    def _start_job(self, job: BackupJob):
        if job.id in self.runners and self.runners[job.id].is_alive():
            return
        runner = JobRunner(job, self.log, self.status_callback)
        self.runners[job.id] = runner
        runner.start()
        self._refresh_tree()

    def _stop_job(self, job: BackupJob):
        runner = self.runners.get(job.id)
        if runner:
            runner.stop()
        self._refresh_tree()

    def open_recall(self):
        job = self._selected_job()
        RecallDialog(self.root, self.jobs, job, self.log)

    def on_close(self):
        for runner in self.runners.values():
            runner.stop()
        save_jobs(self.jobs)
        self.root.after(250, self.root.destroy)


class JobDialog:
    def __init__(self, parent, job: BackupJob = None):
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title("Tambah Job Backup" if job is None else f"Edit: {job.name}")
        self.top.grab_set()
        self.top.resizable(False, False)

        pad = {"padx": 8, "pady": 4}
        r = 0

        ttk.Label(self.top, text="Nama Job:").grid(row=r, column=0, sticky="w", **pad)
        self.name_var = tk.StringVar(value=job.name if job else "")
        ttk.Entry(self.top, textvariable=self.name_var, width=45).grid(row=r, column=1, columnspan=2, **pad)
        r += 1

        ttk.Label(self.top, text="Source (file/folder yang mau ditrack):").grid(row=r, column=0, sticky="w", **pad)
        self.source_var = tk.StringVar(value=job.source if job else "")
        ttk.Entry(self.top, textvariable=self.source_var, width=45).grid(row=r, column=1, **pad)
        ttk.Button(self.top, text="Pilih Folder", command=lambda: self._browse(self.source_var, True)).grid(row=r, column=2, **pad)
        r += 1
        ttk.Button(self.top, text="...atau Pilih File", command=lambda: self._browse(self.source_var, False)).grid(row=r, column=1, sticky="w", **pad)
        r += 1

        ttk.Label(self.top, text="Destination folder (backup zip disimpan di sini):").grid(row=r, column=0, sticky="w", **pad)
        self.dest_var = tk.StringVar(value=job.destination if job else "")
        ttk.Entry(self.top, textvariable=self.dest_var, width=45).grid(row=r, column=1, **pad)
        ttk.Button(self.top, text="Pilih Folder", command=lambda: self._browse(self.dest_var, True)).grid(row=r, column=2, **pad)
        r += 1

        ttk.Label(self.top, text="Interval cek (seberapa sering discan):").grid(row=r, column=0, sticky="w", **pad)
        interval_row = ttk.Frame(self.top)
        interval_row.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        self.interval_value_var = tk.StringVar(value=str(job.interval_value) if job else "30")
        ttk.Entry(interval_row, textvariable=self.interval_value_var, width=8).pack(side="left")
        self.interval_unit_var = tk.StringVar(value=job.interval_unit if job else "detik")
        ttk.Combobox(interval_row, textvariable=self.interval_unit_var, values=["detik", "menit"],
                     state="readonly", width=8).pack(side="left", padx=(6, 0))
        r += 1

        ttk.Label(self.top, text="Durasi tracking (jam, 0 = tanpa batas):").grid(row=r, column=0, sticky="w", **pad)
        self.duration_var = tk.StringVar(value=(f"{job.duration_hours:g}" if job else "0"))
        ttk.Entry(self.top, textvariable=self.duration_var, width=10).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(self.top, text="job otomatis berhenti sendiri setelah durasi ini tercapai",
                  foreground="#888888").grid(row=r, column=2, sticky="w", **pad)
        r += 1

        ttk.Label(self.top, text="Jumlah zip lama disimpan per job:").grid(row=r, column=0, sticky="w", **pad)
        self.retention_var = tk.StringVar(value=str(job.retention) if job else "10")
        ttk.Entry(self.top, textvariable=self.retention_var, width=10).grid(row=r, column=1, sticky="w", **pad)
        r += 1

        ttk.Label(self.top, text="Mode deteksi perubahan:").grid(row=r, column=0, sticky="w", **pad)
        self.mode_var = tk.StringVar(value=job.check_mode if job else "quick")
        ttk.Combobox(self.top, textvariable=self.mode_var, values=["quick", "hash"],
                     state="readonly", width=10).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(self.top, text="quick = cepat (mtime+size), hash = akurat tapi lebih berat",
                  foreground="#888888").grid(row=r, column=2, sticky="w", **pad)
        r += 1

        self.mirror_delete_var = tk.BooleanVar(value=job.mirror_delete if job else False)
        ttk.Checkbutton(self.top, text="Anggap file yang dihapus di source sebagai perubahan (buat snapshot baru)",
                        variable=self.mirror_delete_var).grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1

        ttk.Label(self.top, text="Exclude pattern (pisah koma, contoh: *.tmp,*.log,.git/*):").grid(row=r, column=0, sticky="w", **pad)
        self.exclude_var = tk.StringVar(value=",".join(job.excludes) if job else "")
        ttk.Entry(self.top, textvariable=self.exclude_var, width=45).grid(row=r, column=1, columnspan=2, **pad)
        r += 1

        btn_frame = ttk.Frame(self.top)
        btn_frame.grid(row=r, column=0, columnspan=3, pady=12)
        ttk.Button(btn_frame, text="Simpan", command=lambda: self._save(job)).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Batal", command=self.top.destroy).pack(side="left", padx=6)

    def _browse(self, var: tk.StringVar, folder: bool):
        path = filedialog.askdirectory() if folder else filedialog.askopenfilename()
        if path:
            var.set(path)

    def _save(self, existing_job):
        name = self.name_var.get().strip()
        source = self.source_var.get().strip()
        dest = self.dest_var.get().strip()
        if not name or not source or not dest:
            messagebox.showerror("Error", "Nama, Source, dan Destination wajib diisi")
            return
        try:
            interval_value = int(self.interval_value_var.get())
            retention = int(self.retention_var.get())
            if interval_value <= 0 or retention < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Interval cek harus angka > 0 dan Retention harus angka >= 0")
            return

        interval_unit = self.interval_unit_var.get()
        if interval_unit not in ("detik", "menit"):
            interval_unit = "detik"

        try:
            duration_hours = float(self.duration_var.get().replace(",", "."))
            if duration_hours < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Durasi tracking (jam) harus angka >= 0 (0 = tanpa batas)")
            return

        excludes = [p.strip() for p in self.exclude_var.get().split(",") if p.strip()]
        job_id = existing_job.id if existing_job else new_job_id()
        self.result = BackupJob(
            id=job_id, name=name, source=source, destination=dest,
            interval_value=interval_value, interval_unit=interval_unit,
            duration_hours=duration_hours, retention=retention,
            mirror_delete=self.mirror_delete_var.get(),
            check_mode=self.mode_var.get(), excludes=excludes,
            enabled=False,
        )
        self.top.destroy()


class RecallDialog:
    """
    Implementasi alur "Cara Recall" dari README:
      1. Locate folder backup (destination)
      2. Buka folder "zips"
      3. Pilih titik waktu yang mau direcall
      4. Ambil zip-nya (copy keluar), atau langsung Extract
      5. Unzip / selesai
    """

    def __init__(self, parent, jobs: List[BackupJob], selected_job: Optional[BackupJob], log_callback):
        self.jobs = jobs
        self.log = log_callback
        self.top = tk.Toplevel(parent)
        self.top.title("Recall Backup")
        self.top.grab_set()
        self.top.geometry("620x420")
        self.top.minsize(560, 380)

        pad = {"padx": 8, "pady": 6}

        top_row = ttk.Frame(self.top)
        top_row.pack(fill="x", **pad)
        ttk.Label(top_row, text="1) Folder backup (destination):").pack(anchor="w")
        path_row = ttk.Frame(top_row)
        path_row.pack(fill="x", pady=(2, 0))
        self.dest_var = tk.StringVar(value=selected_job.destination if selected_job else "")
        ttk.Entry(path_row, textvariable=self.dest_var, width=55).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Locate Folder...", command=self._browse_dest).pack(side="left", padx=4)
        ttk.Button(path_row, text="Buka folder \"zips\"", command=self._scan).pack(side="left")

        ttk.Label(self.top, text="2) Pilih titik waktu (timestamp) yang mau direcall:").pack(anchor="w", **pad)

        list_frame = ttk.Frame(self.top)
        list_frame.pack(fill="both", expand=True, padx=8)
        columns = ("job", "timestamp", "size")
        self.result_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)
        self.result_tree.heading("job", text="Job")
        self.result_tree.heading("timestamp", text="Waktu Backup")
        self.result_tree.heading("size", text="Ukuran")
        self.result_tree.column("job", width=180, anchor="w")
        self.result_tree.column("timestamp", width=200, anchor="w")
        self.result_tree.column("size", width=100, anchor="e")
        scrollbar = ttk.Scrollbar(list_frame, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=scrollbar.set)
        self.result_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        btn_frame = ttk.Frame(self.top)
        btn_frame.pack(fill="x", padx=8, pady=8)
        ttk.Label(btn_frame, text="3) & 4) Ambil zip-nya:").pack(side="left")
        ttk.Button(btn_frame, text="Get Zip Away... (copy)", command=self._copy_zip).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Extract Now...", command=self._extract_zip).pack(side="right", padx=4)

        self._zip_index = {}  # tree iid -> Path
        if self.dest_var.get():
            self._scan()

    def _browse_dest(self):
        path = filedialog.askdirectory()
        if path:
            self.dest_var.set(path)
            self._scan()

    def _scan(self):
        self.result_tree.delete(*self.result_tree.get_children())
        self._zip_index.clear()
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showinfo("Info", "Locate folder backup (destination) dulu")
            return
        zips_root = Path(dest) / ZIPS_DIRNAME
        if not zips_root.exists():
            messagebox.showwarning("Tidak ditemukan", f"Folder \"{ZIPS_DIRNAME}\" tidak ada di:\n{dest}")
            return

        entries = []
        for job_dir in sorted(zips_root.iterdir()):
            if not job_dir.is_dir():
                continue
            for zip_file in job_dir.glob("*.zip"):
                entries.append((job_dir.name, zip_file))

        entries.sort(key=lambda t: t[1].stat().st_mtime, reverse=True)

        for job_name, zip_file in entries:
            ts_raw = zip_file.stem
            try:
                dt = datetime.strptime(ts_raw, "%Y%m%d_%H%M%S")
                ts_display = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts_display = ts_raw
            size_kb = zip_file.stat().st_size / 1024
            size_display = f"{size_kb:,.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:,.1f} MB"
            iid = str(zip_file)
            self.result_tree.insert("", "end", iid=iid, values=(job_name, ts_display, size_display))
            self._zip_index[iid] = zip_file

        if not entries:
            messagebox.showinfo("Info", "Belum ada backup zip di folder ini")

    def _selected_zip(self) -> Optional[Path]:
        sel = self.result_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Pilih dulu titik waktu backup di daftar")
            return None
        return self._zip_index.get(sel[0])

    def _copy_zip(self):
        zip_path = self._selected_zip()
        if not zip_path:
            return
        target = filedialog.asksaveasfilename(
            title="Get Zip Away - simpan ke mana?",
            initialfile=zip_path.name,
            defaultextension=".zip",
            filetypes=[("Zip archive", "*.zip")],
        )
        if not target:
            return
        try:
            shutil.copy2(zip_path, target)
        except OSError as e:
            messagebox.showerror("Error", f"Gagal menyalin zip: {e}")
            return
        self.log(f"[Recall] Zip diambil: {zip_path} -> {target}")
        messagebox.showinfo("Selesai", f"Zip berhasil diambil ke:\n{target}\n\nTinggal unzip file itu kapan saja.")

    def _extract_zip(self):
        zip_path = self._selected_zip()
        if not zip_path:
            return
        target_dir = filedialog.askdirectory(title="Extract Now - unzip ke folder mana?")
        if not target_dir:
            return
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(target_dir)
        except (OSError, zipfile.BadZipFile) as e:
            messagebox.showerror("Error", f"Gagal extract zip: {e}")
            return
        self.log(f"[Recall] Zip di-extract: {zip_path} -> {target_dir}")
        if messagebox.askyesno("Selesai", f"File berhasil di-unzip ke:\n{target_dir}\n\nBuka folder sekarang?"):
            self._open_folder(target_dir)

    @staticmethod
    def _open_folder(path: str):
        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
