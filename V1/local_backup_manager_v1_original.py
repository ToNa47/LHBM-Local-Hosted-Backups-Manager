#!/usr/bin/env python3
"""
Local Backup Manager (single-file version)
============================================
Aplikasi desktop independen (Linux/Mac) untuk memantau perubahan
file/folder secara otomatis dan menyimpan backup + histori versinya
ke folder tujuan (folder lokal lain, external drive, atau folder sync
cloud seperti Dropbox/Google Drive Desktop/Nextcloud, dll).

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
import threading
import time
import tkinter as tk
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import List

# ============================================================
# CONFIG: penyimpanan job ke JSON
# ============================================================

CONFIG_DIR = Path.home() / ".config" / "local-backup-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class BackupJob:
    id: str
    name: str
    source: str              # file/folder yang dipantau
    destination: str         # folder tujuan backup
    interval: int = 30       # detik antar-cek
    retention: int = 10      # jumlah versi lama disimpan per file
    mirror_delete: bool = False
    check_mode: str = "quick"  # "quick" (mtime+size) atau "hash" (sha256)
    excludes: list = field(default_factory=list)
    enabled: bool = False


def new_job_id() -> str:
    return str(uuid.uuid4())[:8]


def load_jobs() -> List[BackupJob]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [BackupJob(**d) for d in raw]
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
# ENGINE: thread worker pemantau + backup
# ============================================================

class JobRunner(threading.Thread):
    def __init__(self, job: BackupJob, log_callback, status_callback):
        super().__init__(daemon=True)
        self.job = job
        self.log = log_callback
        self.status_callback = status_callback
        self._stop_event = threading.Event()
        self._state = {}

    def stop(self):
        self._stop_event.set()

    def run(self):
        self.log(f"[{self.job.name}] Monitoring dimulai (setiap {self.job.interval}s)")
        self.status_callback(self.job.id, "running")
        try:
            while not self._stop_event.is_set():
                try:
                    self._scan_and_backup()
                except FileNotFoundError:
                    self.log(f"[{self.job.name}] Source tidak ditemukan, dicoba lagi nanti")
                except Exception as e:
                    self.log(f"[{self.job.name}] ERROR: {e}")
                self._stop_event.wait(self.job.interval)
        finally:
            self.status_callback(self.job.id, "stopped")
            self.log(f"[{self.job.name}] Monitoring dihentikan")

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

    def _scan_and_backup(self):
        src = Path(self.job.source)
        if not src.exists():
            raise FileNotFoundError(str(src))

        dest_root = Path(self.job.destination)
        latest_dir = dest_root / "latest"
        versions_dir = dest_root / "versions"
        latest_dir.mkdir(parents=True, exist_ok=True)
        versions_dir.mkdir(parents=True, exist_ok=True)

        if src.is_file():
            files_to_check = [(src, src.name)]
        else:
            files_to_check = []
            for root, _dirs, files in os.walk(src):
                for fn in files:
                    full = Path(root) / fn
                    rel = str(full.relative_to(src))
                    if self._is_excluded(rel):
                        continue
                    files_to_check.append((full, rel))

        current_rels = set()
        for full_path, rel in files_to_check:
            current_rels.add(rel)
            try:
                sig = self._file_signature(full_path)
            except (FileNotFoundError, PermissionError):
                continue
            if self._state.get(rel) != sig:
                self._backup_file(full_path, rel, latest_dir, versions_dir)
                self._state[rel] = sig

        if self.job.mirror_delete:
            removed = set(self._state.keys()) - current_rels
            for rel in removed:
                target = latest_dir / rel
                if target.exists():
                    target.unlink()
                    self.log(f"[{self.job.name}] Dihapus (mirror): {rel}")
                del self._state[rel]

    def _backup_file(self, full_path: Path, rel: str, latest_dir: Path, versions_dir: Path):
        dest_latest = latest_dir / rel
        dest_latest.parent.mkdir(parents=True, exist_ok=True)

        if dest_latest.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ver_folder = versions_dir / rel
            ver_folder.mkdir(parents=True, exist_ok=True)
            ver_name = f"{ts}_{dest_latest.name}"
            try:
                shutil.copy2(dest_latest, ver_folder / ver_name)
            except OSError as e:
                self.log(f"[{self.job.name}] Gagal simpan versi lama {rel}: {e}")
            self._prune_versions(ver_folder)

        shutil.copy2(full_path, dest_latest)
        self.log(f"[{self.job.name}] Backup: {rel}")

    def _prune_versions(self, ver_folder: Path):
        if self.job.retention <= 0:
            return
        versions = sorted(ver_folder.iterdir(), key=lambda p: p.stat().st_mtime)
        excess = len(versions) - self.job.retention
        for old in versions[:max(0, excess)]:
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
        root.title("Local Backup Manager")
        root.geometry("980x560")
        root.minsize(760, 420)

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

        columns = ("name", "source", "destination", "interval", "status", "last")
        headers = {
            "name": "Nama", "source": "Source", "destination": "Destination",
            "interval": "Interval(s)", "status": "Status", "last": "Update Terakhir",
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
                              values=(job.name, job.source, job.destination, job.interval, status, last))

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
        if messagebox.askyesno("Konfirmasi", f"Hapus job '{job.name}'? (backup yang sudah ada tidak dihapus)"):
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

        ttk.Label(self.top, text="Source (file/folder yang dipantau):").grid(row=r, column=0, sticky="w", **pad)
        self.source_var = tk.StringVar(value=job.source if job else "")
        ttk.Entry(self.top, textvariable=self.source_var, width=45).grid(row=r, column=1, **pad)
        ttk.Button(self.top, text="Pilih Folder", command=lambda: self._browse(self.source_var, True)).grid(row=r, column=2, **pad)
        r += 1
        ttk.Button(self.top, text="...atau Pilih File", command=lambda: self._browse(self.source_var, False)).grid(row=r, column=1, sticky="w", **pad)
        r += 1

        ttk.Label(self.top, text="Destination folder (backup disimpan di sini):").grid(row=r, column=0, sticky="w", **pad)
        self.dest_var = tk.StringVar(value=job.destination if job else "")
        ttk.Entry(self.top, textvariable=self.dest_var, width=45).grid(row=r, column=1, **pad)
        ttk.Button(self.top, text="Pilih Folder", command=lambda: self._browse(self.dest_var, True)).grid(row=r, column=2, **pad)
        r += 1

        ttk.Label(self.top, text="Interval cek (detik):").grid(row=r, column=0, sticky="w", **pad)
        self.interval_var = tk.StringVar(value=str(job.interval) if job else "30")
        ttk.Entry(self.top, textvariable=self.interval_var, width=10).grid(row=r, column=1, sticky="w", **pad)
        r += 1

        ttk.Label(self.top, text="Jumlah versi lama disimpan per file:").grid(row=r, column=0, sticky="w", **pad)
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
        ttk.Checkbutton(self.top, text="Hapus file di destination jika dihapus di source (mirror delete)",
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
            interval = int(self.interval_var.get())
            retention = int(self.retention_var.get())
            if interval <= 0 or retention < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Interval harus > 0 dan Retention harus angka >= 0")
            return

        excludes = [p.strip() for p in self.exclude_var.get().split(",") if p.strip()]
        job_id = existing_job.id if existing_job else new_job_id()
        self.result = BackupJob(
            id=job_id, name=name, source=source, destination=dest,
            interval=interval, retention=retention,
            mirror_delete=self.mirror_delete_var.get(),
            check_mode=self.mode_var.get(), excludes=excludes,
            enabled=False,
        )
        self.top.destroy()


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
