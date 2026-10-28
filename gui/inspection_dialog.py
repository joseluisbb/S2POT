import os
import threading
import customtkinter as ctk
from core.i18n import get_text
from core.metadata_config import inspect_files_batch


class InspectionProgressDialog(ctk.CTkToplevel):
    """
    Lightweight, clean modal dialog showing progress during batch file inspection.
    Provides essential user comfort info without lag or excessive updates.
    """
    def __init__(self, parent, file_paths, folder_path, lang="pt_PT", on_complete=None):
        super().__init__(parent)
        self.parent = parent
        self.file_paths = file_paths or []
        self.folder_path = folder_path
        self.lang = lang
        self.on_complete = on_complete
        self.aborted = False

        self.title(get_text("inspect_dialog_title", self.lang) or "Inspeção de Ficheiros")
        self.geometry("520x200")
        self.resizable(False, False)

        # Center on parent window
        self.transient(parent)
        self.update_idletasks()
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            x = px + max(0, (pw - 520) // 2)
            y = py + max(0, (ph - 200) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

        self.grab_set()

        # UI elements
        self._build_ui()

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Start inspection worker thread
        self._start_worker()

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_ui(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=16)

        self.lbl_title = ctk.CTkLabel(
            container,
            text=f"🔍 {self._tr('inspect_dialog_running')}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_title.pack(anchor="w", pady=(0, 10))

        self.progress_bar = ctk.CTkProgressBar(container, height=14)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", pady=(0, 8))

        self.lbl_status = ctk.CTkLabel(
            container,
            text=f"0 / {len(self.file_paths)} ficheiros...",
            font=ctk.CTkFont(size=12),
            text_color=("gray30", "gray70"),
            anchor="w"
        )
        self.lbl_status.pack(fill="x", pady=(0, 14))

        btn_row = ctk.CTkFrame(container, fg_color="transparent")
        btn_row.pack(fill="x")

        self.btn_cancel = ctk.CTkButton(
            btn_row,
            text=self._tr("inspect_dialog_cancel"),
            width=105,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#c0392b",
            hover_color="#962d22",
            command=self._on_cancel
        )
        self.btn_cancel.pack(side="right")

    def _start_worker(self):
        def worker():
            def progress_cb(curr, tot, fname):
                if not self.aborted:
                    self.after(0, self._update_progress, curr, tot, fname)

            def abort_cb():
                return self.aborted

            inspect_files_batch(self.file_paths, self.folder_path, progress_callback=progress_cb, abort_check=abort_cb)
            self.after(0, self._finish)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _update_progress(self, curr, total, fname):
        if not self.winfo_exists():
            return
        pct = int(round((curr / total) * 100)) if total > 0 else 100
        val = (curr / total) if total > 0 else 1.0
        self.progress_bar.set(val)
        txt = self._tr("inspect_dialog_progress").format(curr=curr, total=total, pct=pct, filename=fname)
        self.lbl_status.configure(text=txt)

    def _on_cancel(self):
        self.aborted = True
        self._finish()

    def _finish(self):
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        if self.on_complete:
            try:
                self.on_complete()
            except Exception as e:
                print(f"Error calling on_complete after inspection: {e}")
