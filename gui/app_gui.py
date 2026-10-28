import os
import glob
import threading
import sys
import shutil
import subprocess
from datetime import datetime
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image
import pymupdf as fitz

from core.config import load_config, save_config, APP_VERSION, APP_NAME
from core.pipeline import batch_process_files, get_expected_output_files, check_input_overwrite_conflict
from core.i18n import LANGUAGES, get_text
from core.metadata_config import load_folder_metadata, save_folder_metadata, METADATA_FILENAME
from core.tuning_config import load_tuning_config, get_effective_file_tuning
from gui.tooltip import ToolTip
from gui.file_browser import FilePageTreeBrowser, get_file_max_dpi, get_file_pages_metadata
from gui.inspection_dialog import InspectionProgressDialog
from gui.s2pit_gui import S2PITPageTuningWindow, S2POTPageViewerWindow

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class ConflictDialog(ctk.CTkToplevel):
    """
    Modal dialog displayed when output file collisions are detected.
    """
    def __init__(self, parent, conflict_count, allow_overwrite=True, has_input_conflict=False):
        super().__init__(parent)

        self.allow_overwrite = allow_overwrite
        self.title(parent._tr("conflict_title"))
        self.geometry("540x260")
        self.resizable(False, False)
        self.result = "cancel"

        self.transient(parent)
        self.grab_set()

        self.update_idletasks()
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_w = parent.winfo_width()
        parent_h = parent.winfo_height()
        x = parent_x + (parent_w - 540) // 2
        y = parent_y + (parent_h - 260) // 2
        self.geometry(f"+{x}+{y}")

        ctk.CTkLabel(
            self,
            text=parent._tr("conflict_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#e6a100"
        ).pack(pady=(18, 8))

        if has_input_conflict or not allow_overwrite:
            msg = parent._tr("conflict_input_msg").format(count=conflict_count)
        else:
            msg = parent._tr("conflict_msg").format(count=conflict_count)

        ctk.CTkLabel(self, text=msg, font=ctk.CTkFont(size=12), justify="center", wraplength=500).pack(pady=(0, 18))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=10)

        self.btn_overwrite = ctk.CTkButton(
            btn_frame,
            text=parent._tr("conflict_btn_overwrite"),
            fg_color="#c0392b" if allow_overwrite else "#7f8c8d",
            hover_color="#962d22" if allow_overwrite else "#7f8c8d",
            state="normal" if allow_overwrite else "disabled",
            width=135,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_overwrite
        )
        self.btn_overwrite.pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text=parent._tr("conflict_btn_archive"),
            fg_color="#2980b9",
            hover_color="#1f618d",
            width=145,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_archive
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text=parent._tr("conflict_btn_cancel"),
            fg_color="gray",
            hover_color="#555555",
            width=110,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_cancel
        ).pack(side="left", padx=8)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window()

    def _on_overwrite(self):
        if not self.allow_overwrite:
            return
        self.result = "overwrite"
        self.destroy()

    def _on_archive(self):
        self.result = "archive"
        self.destroy()

    def _on_cancel(self):
        self.result = "cancel"
        self.destroy()


class OutputBrowserWindow(ctk.CTkToplevel):
    """
    Dedicated Output & Results Explorer Window for S2POT.
    Allows browsing both the Documents folder and the Data folder,
    inspecting generated files, result subfolders, and page structures with full metadata,
    and opening/revealing them.
    """
    def __init__(self, parent, docs_dir="", data_dir="", lang="pt_PT"):
        super().__init__(parent)

        self.parent_app = parent
        self.docs_dir = docs_dir
        self.data_dir = data_dir or docs_dir
        self.lang = lang

        self.title(self._tr("results_browser_title"))
        self.geometry("1220x740")
        self.minsize(900, 580)

        self._build_ui()
        self.update_idletasks()
        if self.docs_dir and os.path.exists(self.docs_dir):
            self.after(10, self._initial_load_async)

    def _initial_load_async(self):
        if self.winfo_exists() and self.docs_dir and os.path.exists(self.docs_dir):
            self.browser.set_folder(self.docs_dir, set_as_root=True, progressive=True)

    def _tr(self, key):
        return get_text(key, self.lang)

    def _get_metadata_context(self):
        ctx = {}
        if self.parent_app and hasattr(self.parent_app, "_get_shared_metadata_context"):
            ctx.update(self.parent_app._get_shared_metadata_context())
        ctx["docs_dir"] = self.docs_dir
        ctx["data_dir"] = self.data_dir
        return ctx

    def _build_ui(self):
        # Top Header Bar
        hdr = ctk.CTkFrame(self, height=48, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        hdr.pack(side="top", fill="x")

        self.lbl_hdr_title = ctk.CTkLabel(
            hdr,
            text=f"📂 {self._tr('results_browser_title')}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_hdr_title.pack(side="left", padx=16, pady=10)

        # Language & Help Controls
        h_lang_help = ctk.CTkFrame(hdr, fg_color="transparent")
        h_lang_help.pack(side="right", padx=(10, 16), pady=8)

        help_init = True
        if self.parent_app and hasattr(self.parent_app, "var_help_mode"):
            help_init = self.parent_app.var_help_mode.get()
        self.var_help_mode = ctk.BooleanVar(value=help_init)
        self.chk_help = ctk.CTkCheckBox(
            h_lang_help,
            text=self._tr("help_mode"),
            variable=self.var_help_mode,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_help_toggle
        )
        self.chk_help.pack(side="left", padx=(0, 12))

        self.lbl_lang = ctk.CTkLabel(h_lang_help, text=self._tr("language"), font=ctk.CTkFont(size=12))
        self.lbl_lang.pack(side="left", padx=(0, 4))

        self.cmb_lang = ctk.CTkOptionMenu(
            h_lang_help,
            values=list(LANGUAGES.values()),
            width=135,
            font=ctk.CTkFont(size=12),
            command=self._on_lang_change
        )
        curr_lang_name = LANGUAGES.get(self.lang, "Português (PT)")
        self.cmb_lang.set(curr_lang_name)
        self.cmb_lang.pack(side="left")

        # Tab Segmented Button: Documents vs Data vs Virtual By Document
        self.seg_folder = ctk.CTkSegmentedButton(
            hdr,
            values=[self._tr("tab_documents"), self._tr("tab_data"), self._tr("tab_by_document")],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_tab_change
        )
        self.seg_folder.set(self._tr("tab_documents"))
        self.seg_folder.pack(side="right", padx=(10, 10), pady=10)

        # FilePageTreeBrowser for output with unified columns matching "4. Ficheiros" + DateModified
        self.browser = FilePageTreeBrowser(
            self,
            initial_dir=self.docs_dir,
            lang=self.lang,
            selectmode="extended",
            show_path_bar=True,
            show_filters=False,
            browser_mode="results_docs",
            columns=("Format", "PagesChars", "Size", "ImgRes", "Status", "DateModified"),
            column_widths={
                "Format": 100,
                "PagesChars": 135,
                "Size": 175,
                "ImgRes": 215,
                "Status": 105,
                "DateModified": 145
            },
            metadata_provider=self._get_metadata_context,
            on_page_activated=lambda p, idx: self._open_in_s2pit(),
            on_selection_changed=self._on_browser_selection_change,
            defer_initial_load=True
        )
        self.browser.pack(fill="both", expand=True, padx=14, pady=8)

        # Bottom Action Bar
        bot_bar = ctk.CTkFrame(self, height=48, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        bot_bar.pack(side="bottom", fill="x")

        # Action Buttons
        self.btn_open = ctk.CTkButton(
            bot_bar,
            text=f"📄 {self._tr('btn_open_file')}",
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="gray50",
            hover_color="#1f618d",
            state="disabled",
            command=self._open_selected_file
        )
        self.btn_open.pack(side="left", padx=12, pady=8)

        self.btn_reveal = ctk.CTkButton(
            bot_bar,
            text=f"📁 {self._tr('btn_open_in_explorer')}",
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#34495e",
            hover_color="#2c3e50",
            command=self._reveal_in_explorer
        )
        self.btn_reveal.pack(side="left", padx=6, pady=8)

        self.btn_s2pit = ctk.CTkButton(
            bot_bar,
            text=self._tr("btn_inspect_s2pit"),
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="gray50",
            hover_color="#512e5f",
            state="disabled",
            command=self._open_in_s2pit
        )
        self.btn_s2pit.pack(side="left", padx=6, pady=8)

        self.btn_close = ctk.CTkButton(
            bot_bar,
            text=self._tr("btn_close"),
            width=95,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#7f8c8d",
            hover_color="#95a5a6",
            command=self.destroy
        )
        self.btn_close.pack(side="right", padx=12, pady=8)

    def _resolve_viewable_file(self, fpath):
        if not fpath or not os.path.exists(fpath):
            return None
        visual_exts = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        ctx = self._get_metadata_context()
        in_dir = ctx.get("input_dir", "")
        if in_dir and os.path.exists(in_dir):
            src_name, _ = self.browser._resolve_source_metadata(fpath, ctx)
            if src_name:
                candidate = os.path.join(in_dir, src_name)
                if os.path.exists(candidate) and os.path.splitext(candidate)[1].lower() in visual_exts:
                    return candidate
            # Fallback: match stripped stem against any visual file in input_dir
            raw_stem = os.path.splitext(os.path.basename(fpath))[0]
            base_stem = self.browser._strip_result_suffix(raw_stem).lower()
            try:
                for entry in os.scandir(in_dir):
                    if entry.is_file():
                        e_stem, e_ext = os.path.splitext(entry.name)
                        if e_ext.lower() in visual_exts and e_stem.lower() == base_stem:
                            return entry.path
            except Exception:
                pass
        ext = os.path.splitext(fpath)[1].lower()
        if ext in visual_exts:
            return fpath
        return None

    def _on_browser_selection_change(self, selected_files, selected_pages):
        has_file = bool(selected_files and os.path.exists(selected_files[0]))
        if has_file:
            self.btn_open.configure(state="normal", fg_color="#2980b9", hover_color="#1f618d")
        else:
            self.btn_open.configure(state="disabled", fg_color="gray50")

        fpath, _ = self.browser.get_selected_page()
        viewable = self._resolve_viewable_file(fpath) if fpath else None
        if viewable:
            self.btn_s2pit.configure(state="normal", fg_color="#6c3483", hover_color="#512e5f")
        else:
            self.btn_s2pit.configure(state="disabled", fg_color="gray50")

    def _on_tab_change(self, choice):
        if choice == self._tr("tab_documents"):
            self.browser.browser_mode = "results_docs"
            if self.docs_dir and os.path.exists(self.docs_dir):
                self.browser.set_folder(self.docs_dir, progressive=True)
        elif choice == self._tr("tab_data"):
            self.browser.browser_mode = "results_data"
            if self.data_dir and os.path.exists(self.data_dir):
                self.browser.set_folder(self.data_dir, progressive=True)
        elif choice == self._tr("tab_by_document"):
            self.browser.browser_mode = "results_by_doc"
            target_root = self.docs_dir if (self.docs_dir and os.path.exists(self.docs_dir)) else self.data_dir
            if target_root and os.path.exists(target_root):
                self.browser.set_folder(target_root, progressive=True)

    def refresh_results(self):
        """Refreshes the results browser with the latest directories and metadata from S2POTApp."""
        if not self.winfo_exists():
            return
        if self.parent_app:
            new_docs = self.parent_app.output_entry.get().strip() or self.parent_app.output_dir
            new_data = self.parent_app.data_entry.get().strip() or self.parent_app.data_dir or new_docs
            if new_docs:
                self.docs_dir = new_docs
            if new_data:
                self.data_dir = new_data
        self.browser.refresh(progressive=True)

    def _open_selected_file(self):
        files = self.browser.get_selected_files()
        if files and os.path.exists(files[0]):
            try:
                os.startfile(files[0])
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível abrir o ficheiro:\n{e}", parent=self)

    def _reveal_in_explorer(self):
        files = self.browser.get_selected_files()
        target = files[0] if files and os.path.exists(files[0]) else self.browser.current_dir
        if target and os.path.exists(target):
            try:
                subprocess.Popen(f'explorer /select,"{os.path.abspath(target)}"')
            except Exception:
                os.startfile(os.path.dirname(os.path.abspath(target)))

    def _open_in_s2pit(self):
        fpath, pidx = self.browser.get_selected_page()
        viewable = self._resolve_viewable_file(fpath)
        if viewable:
            S2POTPageViewerWindow(
                self,
                file_path=viewable,
                page_idx=pidx,
                lang=self.lang,
                on_save_callback=self._on_studio_tuning_saved
            )

    def _on_studio_tuning_saved(self):
        if self.parent_app and hasattr(self.parent_app, "_on_tuning_saved"):
            self.parent_app._on_tuning_saved()
        self.refresh_results()

    def _on_lang_change(self, choice):
        for k, v in LANGUAGES.items():
            if v == choice:
                self.lang = k
                break
        if self.parent_app and hasattr(self.parent_app, "_on_language_change"):
            self.parent_app._on_language_change(choice)
        else:
            self.retranslate(self.lang)

    def _on_help_toggle(self):
        val = self.var_help_mode.get()
        if self.parent_app and hasattr(self.parent_app, "var_help_mode"):
            self.parent_app.var_help_mode.set(val)
            self.parent_app._on_help_toggle()

    def retranslate(self, new_lang):
        self.lang = new_lang
        self.title(self._tr("results_browser_title"))
        self.lbl_hdr_title.configure(text=f"📂 {self._tr('results_browser_title')}")
        self.chk_help.configure(text=self._tr("help_mode"))
        self.lbl_lang.configure(text=self._tr("language"))
        curr_lang_name = LANGUAGES.get(self.lang, "Português (PT)")
        self.cmb_lang.set(curr_lang_name)
        self.seg_folder.configure(values=[self._tr("tab_documents"), self._tr("tab_data"), self._tr("tab_by_document")])
        if self.browser.browser_mode == "results_by_doc":
            self.seg_folder.set(self._tr("tab_by_document"))
        elif self.browser.browser_mode == "results_data":
            self.seg_folder.set(self._tr("tab_data"))
        else:
            self.seg_folder.set(self._tr("tab_documents"))
        self.btn_open.configure(text=f"📄 {self._tr('btn_open_file')}")
        self.btn_reveal.configure(text=f"📁 {self._tr('btn_open_in_explorer')}")
        self.btn_s2pit.configure(text=self._tr("btn_inspect_s2pit"))
        self.btn_close.configure(text=self._tr("btn_close"))
        self.browser.set_language(new_lang)


class S2POTApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1440x960")
        self.minsize(1180, 850)

        self.config_data = load_config()
        self.input_dir = self.config_data.get("last_input_dir", "")
        self.output_dir = self.config_data.get("last_output_dir", "")
        self.data_dir = self.config_data.get("last_data_dir", "")
        self.current_language = self.config_data.get("language", "pt_PT")

        self.found_files = []
        self.file_items = {}
        self.item_to_filepath = {}
        self.sort_column = None
        self.sort_reverse = False
        self.table_font_size = self.config_data.get("table_font_size", 10)
        self.abort_requested = False
        self.is_processing = False
        self.current_processing_file = None
        self.loaded_pages = set()
        self.results_window = None
        self.folder_metadata = None
        self.folder_tuning = None
        self.active_studio = None
        self.item_to_info = {}

        self._build_ui()

        if self.input_dir and os.path.exists(self.input_dir):
            self.input_entry.insert(0, self.input_dir)
            self._scan_input_folder()

        if self.output_dir and os.path.exists(self.output_dir):
            self.output_entry.insert(0, self.output_dir)

        if self.data_dir and os.path.exists(self.data_dir):
            self.data_entry.insert(0, self.data_dir)

    def is_help_mode(self):
        return self.var_help_mode.get()

    def _tr(self, key):
        return get_text(key, self.current_language)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=1)

        # ------------------- HEADER -------------------
        header = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray90", "#1a1a1a"))
        header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        header.grid_columnconfigure(1, weight=1)

        self.lbl_title = ctk.CTkLabel(
            header,
            text=f"S2POT — Smart Slim PDF OCR Tool v{APP_VERSION}",
            font=ctk.CTkFont(size=18, weight="bold")
        )
        self.lbl_title.grid(row=0, column=0, padx=20, pady=12, sticky="w")

        h_right = ctk.CTkFrame(header, fg_color="transparent")
        h_right.grid(row=0, column=2, padx=20, pady=12, sticky="e")

        self.var_help_mode = ctk.BooleanVar(value=self.config_data.get("help_mode", True))
        self.chk_help = ctk.CTkCheckBox(
            h_right,
            text=self._tr("help_mode"),
            variable=self.var_help_mode,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_help_toggle
        )
        self.chk_help.pack(side="left", padx=(0, 15))

        self.lbl_lang = ctk.CTkLabel(h_right, text=self._tr("language"), font=ctk.CTkFont(size=12))
        self.lbl_lang.pack(side="left", padx=(0, 5))

        self.cmb_lang = ctk.CTkOptionMenu(
            h_right,
            values=list(LANGUAGES.values()),
            width=140,
            font=ctk.CTkFont(size=12),
            command=self._on_language_change
        )
        curr_lang_name = LANGUAGES.get(self.current_language, "Português (PT)")
        self.cmb_lang.set(curr_lang_name)
        self.cmb_lang.pack(side="left")

        # ------------------- CONTROL PANEL (LEFT: 1 & 3, RIGHT: 2) -------------------
        ctrl_panel = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_panel.grid(row=1, column=0, sticky="ew", padx=15, pady=8)
        ctrl_panel.grid_columnconfigure(0, weight=1)
        ctrl_panel.grid_columnconfigure(1, weight=0)
        ctrl_panel.grid_columnconfigure(2, weight=1)

        # ====== LEFT PANEL (SECTION 1 TOP, SECTION 3 BOTTOM) ======
        left_panel = ctk.CTkFrame(ctrl_panel, fg_color="transparent")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=0)
        left_panel.grid_rowconfigure(0, weight=1)
        left_panel.grid_rowconfigure(1, weight=1)
        left_panel.grid_columnconfigure(0, weight=1)

        # ====== BOX 1: INPUT & FORMATS ======
        self.box1 = ctk.CTkFrame(left_panel, corner_radius=8)
        self.box1.grid(row=0, column=0, sticky="nsew", padx=0, pady=(0, 5))

        self.lbl_box1 = ctk.CTkLabel(self.box1, text=self._tr("box_1_input"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_box1.pack(anchor="w", padx=12, pady=(8, 4))

        f_in = ctk.CTkFrame(self.box1, fg_color="transparent")
        f_in.pack(fill="x", padx=10, pady=2)
        self.input_entry = ctk.CTkEntry(f_in, placeholder_text=self._tr("input_placeholder"), font=ctk.CTkFont(size=12))
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.input_entry.bind("<Return>", self._on_input_entry_change)
        self.input_entry.bind("<FocusOut>", self._on_input_entry_change)
        self.btn_in_browse = ctk.CTkButton(f_in, text=self._tr("browse"), width=80, font=ctk.CTkFont(size=12, weight="bold"), command=self._browse_input)
        self.btn_in_browse.pack(side="right")

        self.var_sync_paths = ctk.BooleanVar(value=self.config_data.get("sync_paths", True))
        self.chk_sync_paths = ctk.CTkCheckBox(
            self.box1,
            text=self._tr("sync_paths"),
            variable=self.var_sync_paths,
            font=ctk.CTkFont(size=12),
            command=self._on_sync_paths_toggle
        )
        self.chk_sync_paths.pack(anchor="w", padx=12, pady=(2, 2))

        self.lbl_badge = ctk.CTkLabel(self.box1, text="Ficheiros encontrados: 0", font=ctk.CTkFont(size=12, weight="bold"), text_color="#3498db")
        self.lbl_badge.pack(anchor="w", padx=12, pady=(2, 2))

        self.btn_tuning_file = ctk.CTkButton(
            self.box1,
            text=self._tr("btn_tuning_file"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            state="disabled",
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_tuning_file
        )
        self.btn_tuning_file.pack(fill="x", padx=10, pady=(2, 6))

        fmt_frame = ctk.CTkFrame(self.box1, fg_color=("gray95", "#242424"), corner_radius=6)
        fmt_frame.pack(fill="x", padx=10, pady=(0, 8))

        self.var_filter_pdf = ctk.BooleanVar(value=self.config_data.get("filter_pdf", True))
        self.var_filter_tiff = ctk.BooleanVar(value=self.config_data.get("filter_tiff", True))
        self.var_filter_jpg = ctk.BooleanVar(value=self.config_data.get("filter_jpg", True))
        self.var_filter_png = ctk.BooleanVar(value=self.config_data.get("filter_png", True))
        self.var_filter_bmp = ctk.BooleanVar(value=self.config_data.get("filter_bmp", True))
        self.var_filter_webp = ctk.BooleanVar(value=self.config_data.get("filter_webp", True))

        f_cb1 = ctk.CTkFrame(fmt_frame, fg_color="transparent")
        f_cb1.pack(fill="x", padx=6, pady=3)
        self.chk_filter_pdf = ctk.CTkCheckBox(f_cb1, text="PDF", font=ctk.CTkFont(size=12), variable=self.var_filter_pdf, command=self._scan_input_folder)
        self.chk_filter_pdf.pack(side="left", padx=4)
        self.chk_filter_tiff = ctk.CTkCheckBox(f_cb1, text="TIFF", font=ctk.CTkFont(size=12), variable=self.var_filter_tiff, command=self._scan_input_folder)
        self.chk_filter_tiff.pack(side="left", padx=4)
        self.chk_filter_jpg = ctk.CTkCheckBox(f_cb1, text="JPG", font=ctk.CTkFont(size=12), variable=self.var_filter_jpg, command=self._scan_input_folder)
        self.chk_filter_jpg.pack(side="left", padx=4)

        f_cb2 = ctk.CTkFrame(fmt_frame, fg_color="transparent")
        f_cb2.pack(fill="x", padx=6, pady=(0, 3))
        self.chk_filter_png = ctk.CTkCheckBox(f_cb2, text="PNG", font=ctk.CTkFont(size=12), variable=self.var_filter_png, command=self._scan_input_folder)
        self.chk_filter_png.pack(side="left", padx=4)
        self.chk_filter_bmp = ctk.CTkCheckBox(f_cb2, text="BMP", font=ctk.CTkFont(size=12), variable=self.var_filter_bmp, command=self._scan_input_folder)
        self.chk_filter_bmp.pack(side="left", padx=4)
        self.chk_filter_webp = ctk.CTkCheckBox(f_cb2, text="WEBP", font=ctk.CTkFont(size=12), variable=self.var_filter_webp, command=self._scan_input_folder)
        self.chk_filter_webp.pack(side="left", padx=4)

        # ====== BOX 3: PROCESS & OCR ENGINE (BELOW BOX 1) ======
        self.box3 = ctk.CTkFrame(left_panel, corner_radius=8)
        self.box3.grid(row=1, column=0, sticky="nsew", padx=0, pady=(5, 0))

        self.lbl_box3 = ctk.CTkLabel(self.box3, text=self._tr("box_3_process"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_box3.pack(anchor="w", padx=12, pady=(8, 4))

        f_dpi = ctk.CTkFrame(self.box3, fg_color="transparent")
        f_dpi.pack(fill="x", padx=10, pady=2)
        self.lbl_dpi = ctk.CTkLabel(f_dpi, text=self._tr("resolution_dpi"), font=ctk.CTkFont(size=12))
        self.lbl_dpi.pack(side="left", padx=(0, 5))
        self.cmb_dpi = ctk.CTkOptionMenu(
            f_dpi,
            values=["300 DPI (Standard)", "150 DPI (Fast)", "600 DPI (Ultra)"],
            width=145,
            font=ctk.CTkFont(size=12),
            command=self._on_dpi_change
        )
        self.cmb_dpi.set("300 DPI (Standard)")
        self.cmb_dpi.pack(side="right")

        f_opt = ctk.CTkFrame(self.box3, fg_color=("gray95", "#242424"), corner_radius=6)
        f_opt.pack(fill="x", padx=10, pady=4)

        self.var_deskew = ctk.BooleanVar(value=self.config_data.get("enable_auto_deskew", True))
        self.chk_deskew = ctk.CTkCheckBox(f_opt, text=self._tr("auto_deskew"), font=ctk.CTkFont(size=12), variable=self.var_deskew)
        self.chk_deskew.pack(anchor="w", padx=8, pady=(4, 2))

        self.var_handwritten = ctk.BooleanVar(value=(self.config_data.get("doc_type", "printed") == "handwritten"))
        self.chk_handwritten = ctk.CTkCheckBox(
            f_opt,
            text=self._tr("doc_type_handwritten"),
            font=ctk.CTkFont(size=12, weight="bold"),
            variable=self.var_handwritten,
            text_color=("#8e44ad", "#a569bd"),
            command=self._on_handwritten_toggle
        )
        self.chk_handwritten.pack(anchor="w", padx=8, pady=(2, 6))

        f_btns = ctk.CTkFrame(self.box3, fg_color="transparent")
        f_btns.pack(fill="x", padx=10, pady=(2, 6))

        self.btn_start = ctk.CTkButton(
            f_btns,
            text=self._tr("btn_process"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._start_batch_process
        )
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.btn_stop = ctk.CTkButton(
            f_btns,
            text=self._tr("btn_abort"),
            fg_color="#c0392b",
            hover_color="#962d22",
            state="disabled",
            width=85,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._abort_batch_process
        )
        self.btn_stop.pack(side="right")

        self.progress = ctk.CTkProgressBar(self.box3)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

        # ====== CLEAR VISIBLE SEPARATOR LINE ======
        separator = ctk.CTkFrame(ctrl_panel, width=2, fg_color=("gray75", "#444444"))
        separator.grid(row=0, column=1, sticky="ns", padx=8, pady=2)

        # ====== BOX 2: OUTPUT (RIGHT SIDE FULL HEIGHT) ======
        self.box2 = ctk.CTkFrame(ctrl_panel, corner_radius=8)
        self.box2.grid(row=0, column=2, sticky="nsew", padx=(5, 0), pady=0)

        self.lbl_box2 = ctk.CTkLabel(self.box2, text=self._tr("box_2_output"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_box2.pack(anchor="w", padx=12, pady=(8, 4))

        # Subsection 1: Documents
        self.lbl_sub_docs = ctk.CTkLabel(self.box2, text=f"• {self._tr('sub_documents')}", font=ctk.CTkFont(size=12, weight="bold"), text_color=("#145a32", "#27ae60"))
        self.lbl_sub_docs.pack(anchor="w", padx=12, pady=(2, 2))

        self.lbl_docs_path = ctk.CTkLabel(self.box2, text=self._tr("docs_folder_label"), font=ctk.CTkFont(size=12))
        self.lbl_docs_path.pack(anchor="w", padx=12, pady=(0, 1))

        f_out = ctk.CTkFrame(self.box2, fg_color="transparent")
        f_out.pack(fill="x", padx=10, pady=2)
        self.output_entry = ctk.CTkEntry(f_out, placeholder_text=self._tr("output_placeholder"), font=ctk.CTkFont(size=12))
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.output_entry.bind("<Return>", self._on_output_entry_change)
        self.output_entry.bind("<FocusOut>", self._on_output_entry_change)
        self.btn_out_browse = ctk.CTkButton(f_out, text=self._tr("browse"), width=80, font=ctk.CTkFont(size=12, weight="bold"), command=self._browse_output)
        self.btn_out_browse.pack(side="right")

        # Organize Documents Checkbox placed directly below documents folder path
        self.var_doc_subfolders = ctk.BooleanVar(value=self.config_data.get("organize_doc_subfolders", False))
        self.chk_doc_subfolders = ctk.CTkCheckBox(self.box2, text=self._tr("organize_doc_subfolders"), font=ctk.CTkFont(size=12), variable=self.var_doc_subfolders, command=self._on_option_toggle)
        self.chk_doc_subfolders.pack(anchor="w", padx=12, pady=(2, 4))

        doc_frame = ctk.CTkFrame(self.box2, fg_color=("gray95", "#242424"), corner_radius=6)
        doc_frame.pack(fill="x", padx=10, pady=(2, 6))

        self.var_pdf = ctk.BooleanVar(value=self.config_data.get("export_pdf", True))
        self.var_docx = ctk.BooleanVar(value=self.config_data.get("export_docx", True))

        self.chk_pdf = ctk.CTkCheckBox(doc_frame, text=self._tr("export_pdf"), font=ctk.CTkFont(size=12), variable=self.var_pdf, command=self._on_option_toggle)
        self.chk_pdf.pack(anchor="w", padx=8, pady=3)

        self.chk_docx = ctk.CTkCheckBox(doc_frame, text=self._tr("export_docx"), font=ctk.CTkFont(size=12), variable=self.var_docx, command=self._on_option_toggle)
        self.chk_docx.pack(anchor="w", padx=8, pady=3)

        # Subsection 2: Data
        self.lbl_sub_data = ctk.CTkLabel(self.box2, text=f"• {self._tr('sub_data')}", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e67e22")
        self.lbl_sub_data.pack(anchor="w", padx=12, pady=(4, 2))

        # Destination Folder for Data Files placed IMMEDIATELY after title, BEFORE checkboxes
        self.lbl_data_path = ctk.CTkLabel(self.box2, text=self._tr("data_folder_label"), font=ctk.CTkFont(size=12))
        self.lbl_data_path.pack(anchor="w", padx=12, pady=(2, 1))

        f_data = ctk.CTkFrame(self.box2, fg_color="transparent")
        f_data.pack(fill="x", padx=10, pady=(0, 2))
        self.data_entry = ctk.CTkEntry(f_data, placeholder_text=self._tr("data_placeholder"), font=ctk.CTkFont(size=12))
        self.data_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.data_entry.bind("<Return>", self._on_data_entry_change)
        self.data_entry.bind("<FocusOut>", self._on_data_entry_change)
        self.btn_data_browse = ctk.CTkButton(f_data, text=self._tr("browse"), width=80, font=ctk.CTkFont(size=12, weight="bold"), command=self._browse_data)
        self.btn_data_browse.pack(side="right")

        # Organize Data Checkbox placed directly below data folder path
        self.var_subfolders = ctk.BooleanVar(value=self.config_data.get("organize_subfolders", False))
        self.chk_subfolders = ctk.CTkCheckBox(self.box2, text=self._tr("organize_subfolders"), font=ctk.CTkFont(size=12), variable=self.var_subfolders, command=self._on_option_toggle)
        self.chk_subfolders.pack(anchor="w", padx=12, pady=(2, 4))

        data_fmt_frame = ctk.CTkFrame(self.box2, fg_color=("gray95", "#242424"), corner_radius=6)
        data_fmt_frame.pack(fill="x", padx=10, pady=2)

        self.var_txt = ctk.BooleanVar(value=self.config_data.get("export_txt", True))
        self.var_hocr = ctk.BooleanVar(value=self.config_data.get("export_hocr", False))
        self.var_json = ctk.BooleanVar(value=self.config_data.get("export_words_json", False))
        self.var_csv = ctk.BooleanVar(value=self.config_data.get("export_words_csv", False))
        self.var_xlsx = ctk.BooleanVar(value=self.config_data.get("export_words_xlsx", False))
        self.var_audit = ctk.BooleanVar(value=self.config_data.get("export_audit_report", False))
        self.var_txt_from_pdf = ctk.BooleanVar(value=self.config_data.get("export_txt_from_pdf", True))
        self.var_docx_from_pdf = ctk.BooleanVar(value=self.config_data.get("export_docx_from_pdf", True))

        f_data_cb1 = ctk.CTkFrame(data_fmt_frame, fg_color="transparent")
        f_data_cb1.pack(fill="x", padx=4, pady=2)
        self.chk_txt = ctk.CTkCheckBox(f_data_cb1, text="TXT", font=ctk.CTkFont(size=12), variable=self.var_txt, command=self._on_option_toggle)
        self.chk_txt.pack(side="left", padx=4)
        self.chk_hocr = ctk.CTkCheckBox(f_data_cb1, text="hOCR", font=ctk.CTkFont(size=12), variable=self.var_hocr, command=self._on_option_toggle)
        self.chk_hocr.pack(side="left", padx=4)
        self.chk_json = ctk.CTkCheckBox(f_data_cb1, text="JSON", font=ctk.CTkFont(size=12), variable=self.var_json, command=self._on_option_toggle)
        self.chk_json.pack(side="left", padx=4)

        f_data_cb2 = ctk.CTkFrame(data_fmt_frame, fg_color="transparent")
        f_data_cb2.pack(fill="x", padx=4, pady=(0, 2))
        self.chk_csv = ctk.CTkCheckBox(f_data_cb2, text="CSV", font=ctk.CTkFont(size=12), variable=self.var_csv, command=self._on_option_toggle)
        self.chk_csv.pack(side="left", padx=4)
        self.chk_xlsx = ctk.CTkCheckBox(f_data_cb2, text="Excel", font=ctk.CTkFont(size=12), variable=self.var_xlsx, command=self._on_option_toggle)
        self.chk_xlsx.pack(side="left", padx=4)
        self.chk_audit = ctk.CTkCheckBox(f_data_cb2, text=self._tr("export_audit_report"), font=ctk.CTkFont(size=12), variable=self.var_audit, command=self._on_option_toggle)
        self.chk_audit.pack(side="left", padx=4)

        f_data_cb3 = ctk.CTkFrame(data_fmt_frame, fg_color="transparent")
        f_data_cb3.pack(fill="x", padx=4, pady=(0, 2))
        self.chk_txt_from_pdf = ctk.CTkCheckBox(f_data_cb3, text=self._tr("export_txt_from_pdf"), font=ctk.CTkFont(size=12), variable=self.var_txt_from_pdf, command=self._on_option_toggle)
        self.chk_txt_from_pdf.pack(side="left", padx=4)
        self.chk_docx_from_pdf = ctk.CTkCheckBox(f_data_cb3, text=self._tr("export_docx_from_pdf"), font=ctk.CTkFont(size=12), variable=self.var_docx_from_pdf, command=self._on_option_toggle)
        self.chk_docx_from_pdf.pack(side="left", padx=4)

        self.btn_explore_results = ctk.CTkButton(
            self.box2,
            text=self._tr("btn_explore_results"),
            fg_color="#6c3483",
            hover_color="#512e5f",
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_results_browser
        )
        self.btn_explore_results.pack(fill="x", padx=10, pady=(2, 8))

        # ------------------- BOX 4: FILE LIST TABLE (WITH EXPANDABLE TREE & CHARS COLUMN) -------------------
        tbl_frame = ctk.CTkFrame(self, corner_radius=8)
        tbl_frame.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 15))
        tbl_frame.grid_columnconfigure(0, weight=1)
        tbl_frame.grid_rowconfigure(1, weight=1)

        f_tbl_hdr = ctk.CTkFrame(tbl_frame, fg_color="transparent")
        f_tbl_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        self.lbl_box4 = ctk.CTkLabel(f_tbl_hdr, text=self._tr("box_table"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_box4.pack(side="left")

        self.btn_inspect = ctk.CTkButton(
            f_tbl_hdr,
            text=self._tr("btn_inspect"),
            width=170,
            height=28,
            fg_color="#6c3483",
            hover_color="#512e5f",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._inspect_files
        )
        self.btn_inspect.pack(side="left", padx=(15, 6))

        self.btn_tune_page = ctk.CTkButton(
            f_tbl_hdr,
            text=self._tr("btn_tune_page"),
            width=150,
            height=28,
            fg_color="gray50",
            hover_color="#512e5f",
            state="disabled",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_tuning_studio
        )
        self.btn_tune_page.pack(side="left", padx=6)

        self.lbl_selected_badge = ctk.CTkLabel(f_tbl_hdr, text="Selecionados: 0 de 0", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e67e22")
        self.lbl_selected_badge.pack(side="right", padx=10)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background="#ffffff",
            foreground="#1a1a1a",
            fieldbackground="#ffffff",
            rowheight=26,
            font=("Segoe UI", self.table_font_size),
            borderwidth=1,
            relief="solid",
            bordercolor="#d0d0d0",
            lightcolor="#d0d0d0",
            darkcolor="#d0d0d0"
        )
        style.configure(
            "Treeview.Heading",
            background="#f2f4f7",
            foreground="#1a1a1a",
            relief="solid",
            borderwidth=1,
            bordercolor="#d0d0d0",
            font=("Segoe UI", 10, "bold")
        )
        style.map("Treeview", background=[("selected", "#2980b9")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#e2e6ea")])

        # New 6-column layout: Filename (#0), Format, Pages/Chars, Size, Img. Res., Status
        cols = ("Format", "PagesChars", "Size", "ImgRes", "Status")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="tree headings", selectmode="extended")

        self.tree.heading("#0", text=self._tr("col_filename"), command=lambda: self._sort_table("#0"))
        self.tree.heading("Format", text=self._tr("col_format"), command=lambda: self._sort_table("Format"))
        self.tree.heading("PagesChars", text=self._tr("col_pages_chars"), command=lambda: self._sort_table("PagesChars"))
        self.tree.heading("Size", text=self._tr("col_size"), command=lambda: self._sort_table("Size"))
        self.tree.heading("ImgRes", text=self._tr("col_img_res"), command=lambda: self._sort_table("ImgRes"))
        self.tree.heading("Status", text=self._tr("col_status"), command=lambda: self._sort_table("Status"))

        self.tree.column("#0", width=290, minwidth=180, anchor="w")
        self.tree.column("Format", width=110, anchor="center")
        self.tree.column("PagesChars", width=175, anchor="center")
        self.tree.column("Size", width=185, anchor="center")
        self.tree.column("ImgRes", width=220, anchor="center")
        self.tree.column("Status", width=135, anchor="center")

        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f8f9fa")
        self.tree.tag_configure("page_even", background="#f0f4f8")
        self.tree.tag_configure("page_odd", background="#e7eff7")
        self.tree.tag_configure("page_row", background="#f0f4f8")
        self.tree.tag_configure("file_warn_conf", background="#fff8db", foreground="#9c6500")
        self.tree.tag_configure("file_low_conf", background="#fde8e8", foreground="#b91c1c")
        self.tree.tag_configure("page_warn_conf", background="#fff3cd", foreground="#856404")
        self.tree.tag_configure("page_low_conf", background="#fadbd8", foreground="#922b21")

        self._setup_tree_gridlines()

        self.tree.bind("<<TreeviewSelect>>", self._on_table_select)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<Double-1>", self._on_tree_item_activate)
        self.tree.bind("<Return>", self._on_tree_item_activate)

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=5)
        vsb.grid(row=1, column=1, sticky="ns", padx=(0, 10), pady=5)
        hsb.grid(row=2, column=0, sticky="ew", padx=(10, 0), pady=(0, 5))

        self._attach_tooltips()
        self._check_conditional_controls()

    def _setup_tree_gridlines(self):
        cols = ("Format", "PagesChars", "Size", "ImgRes", "Status")
        all_cols = ["#0"] + list(cols)[:-1]
        self._tree_grid_dividers = []
        for _ in all_cols:
            div = tk.Frame(self.tree, width=1, bg="#dcdcdc")
            self._tree_grid_dividers.append(div)

        def _update_dividers(event=None):
            try:
                tot = sum(self.tree.column(c, "width") for c in ["#0"] + list(cols))
                off = int(self.tree.xview()[0] * tot)
                cx = 0
                for i, c in enumerate(all_cols):
                    cx += self.tree.column(c, "width")
                    px = cx - off
                    div = self._tree_grid_dividers[i]
                    if 0 <= px <= self.tree.winfo_width():
                        div.place(x=px, y=0, relheight=1.0)
                    else:
                        div.place_forget()
            except Exception:
                pass

        self._update_gridlines_fn = _update_dividers
        self.tree.bind("<Configure>", _update_dividers, add="+")
        self.tree.bind("<ButtonRelease-1>", _update_dividers, add="+")
        self.tree.bind("<B1-Motion>", _update_dividers, add="+")

    def _attach_tooltips(self):
        # Header controls
        ToolTip(self.chk_help, "tt_help_mode", self)
        ToolTip(self.cmb_lang, "tt_lang", self)

        # Section 1 (Box 1: Input & Formats)
        ToolTip(self.input_entry, "tt_input_folder", self)
        ToolTip(self.btn_in_browse, "tt_browse", self)
        ToolTip(self.chk_filter_pdf, "tt_filter_pdf", self)
        ToolTip(self.chk_filter_tiff, "tt_filter_tiff", self)
        ToolTip(self.chk_filter_jpg, "tt_filter_jpg", self)
        ToolTip(self.chk_filter_png, "tt_filter_png", self)
        ToolTip(self.chk_filter_bmp, "tt_filter_bmp", self)
        ToolTip(self.chk_filter_webp, "tt_filter_webp", self)

        # Section 2 (Box 2: Output Documents & Data)
        ToolTip(self.output_entry, "tt_docs_folder", self)
        ToolTip(self.btn_out_browse, "tt_browse", self)
        ToolTip(self.chk_pdf, "tt_export_pdf", self)
        ToolTip(self.chk_docx, "tt_export_docx", self)
        ToolTip(self.chk_doc_subfolders, "tt_organize_doc_subfolders", self)

        ToolTip(self.chk_txt, "tt_fmt_txt", self)
        ToolTip(self.chk_hocr, "tt_fmt_hocr", self)
        ToolTip(self.chk_json, "tt_fmt_words_json", self)
        ToolTip(self.chk_csv, "tt_fmt_words_csv", self)
        ToolTip(self.chk_xlsx, "tt_fmt_words_xlsx", self)
        ToolTip(self.chk_audit, "tt_export_audit_report", self)
        ToolTip(self.chk_txt_from_pdf, "tt_export_txt_from_pdf", self)
        ToolTip(self.chk_docx_from_pdf, "tt_export_docx_from_pdf", self)
        ToolTip(self.data_entry, "tt_data_folder", self)
        ToolTip(self.btn_data_browse, "tt_browse", self)
        ToolTip(self.chk_subfolders, "tt_organize_subfolders", self)
        ToolTip(self.btn_explore_results, "tt_explore_results", self)

        # Section 3 (Box 3: Process & OCR Engine)
        ToolTip(self.cmb_dpi, "tt_resolution_dpi", self)
        ToolTip(self.chk_deskew, "tt_auto_deskew", self)
        ToolTip(self.btn_start, "tt_btn_process", self)
        ToolTip(self.btn_stop, "tt_btn_abort", self)

        # Section 4 (Box 4: Input Files & Table)
        ToolTip(self.btn_inspect, "tt_btn_inspect", self)
        ToolTip(self.btn_tune_page, "tt_btn_tune_selected_page", self)

    def _on_help_toggle(self):
        self.config_data["help_mode"] = self.var_help_mode.get()
        save_config(self.config_data)

    def _on_language_change(self, choice):
        for k, v in LANGUAGES.items():
            if v == choice:
                self.current_language = k
                break
        self.config_data["language"] = self.current_language
        save_config(self.config_data)
        self._retranslate_ui()

    def _get_selected_ui_dpi(self):
        dpi_str = self.cmb_dpi.get() if hasattr(self, "cmb_dpi") else "300"
        if "150" in dpi_str:
            return 150
        elif "600" in dpi_str:
            return 600
        return 300

    def _get_shared_metadata_context(self):
        """Returns the live metadata and tuning state shared with OutputBrowserWindow."""
        in_dir = (self.input_entry.get().strip() or self.input_dir or "").strip('"\'')
        live_file_infos = {}
        for info in self.item_to_info.values():
            if info.get("type") == "file" and info.get("filepath"):
                live_file_infos[info["filepath"]] = info
        input_files_norm = {os.path.normcase(os.path.abspath(f)) for f in self.found_files if f}
        return {
            "input_dir": in_dir,
            "folder_metadata": self.folder_metadata,
            "folder_tuning": self.folder_tuning,
            "live_file_infos": live_file_infos,
            "input_files_norm": input_files_norm,
            "ui_dpi": self._get_selected_ui_dpi()
        }

    def _on_dpi_change(self, choice=None):
        ui_dpi = self._get_selected_ui_dpi()
        for item_id, item_info in self.item_to_info.items():
            fname = item_info.get("filename")
            pidx = item_info.get("page_idx") if item_info.get("type") == "page" else None
            eff = get_effective_file_tuning(self.folder_tuning, fname, page_idx=pidx, default_dpi=ui_dpi)
            item_info["target_dpi"] = eff.get("target_dpi")
            item_info["target_cmode"] = eff.get("color_mode")
        self._refresh_all_row_displays()
        if self.results_window is not None and self.results_window.winfo_exists():
            self.results_window.refresh_results()

    def _on_handwritten_toggle(self):
        doc_type = "handwritten" if self.var_handwritten.get() else "printed"
        self.config_data["doc_type"] = doc_type
        save_config(self.config_data)

    def _format_color_mode(self, cmode):
        if not cmode:
            return ""
        cmode_lower = str(cmode).lower()
        if "gray" in cmode_lower or "cinz" in cmode_lower:
            return self._tr("color_short_gray")
        elif "mono" in cmode_lower or "bw" in cmode_lower or "pb" in cmode_lower or "black" in cmode_lower:
            return self._tr("color_short_bw")
        return self._tr("color_short_color")

    def _get_file_conf_badge_and_tag(self, info, f_idx=0):
        default_tag = "even" if f_idx % 2 == 0 else "odd"
        fconf = info.get("confidence")
        min_pconf = info.get("min_page_confidence")
        low_pages = info.get("low_conf_pages", 0) or 0
        total_chars = info.get("total_chars")
        status_val = info.get("status", "")

        if fconf is None:
            return None, default_tag

        is_critical = (
            fconf < 65.0
            or (min_pconf is not None and min_pconf < 65.0)
            or (status_val == "done" and total_chars is not None and total_chars == 0)
        )
        is_warn = (
            not is_critical
            and (fconf < 80.0 or (min_pconf is not None and min_pconf < 80.0) or low_pages > 0)
        )

        if is_critical:
            return f"🔴 {fconf:.0f}%", "file_low_conf"
        elif is_warn:
            return f"⚠️ {fconf:.0f}%", "file_warn_conf"
        else:
            return f"🟢 {fconf:.0f}%", default_tag

    def _get_page_conf_badge_tag_and_label(self, pinfo, p_idx=0):
        pidx = pinfo.get("page_idx", p_idx)
        default_tag = "page_even" if p_idx % 2 == 0 else "page_odd"
        base_page_txt = self._tr("tree_item_page").format(idx=pidx + 1)

        pconf = pinfo.get("confidence")
        pchars = pinfo.get("chars")
        status_val = pinfo.get("status", "")

        if pconf is None:
            return None, default_tag, f"   ├── 📄 {base_page_txt}"

        if pconf < 65.0 or (status_val == "done" and pchars is not None and pchars == 0):
            return f"🔴 {pconf:.0f}%", "page_low_conf", f"   ├── 🔴 {base_page_txt}"
        elif pconf < 80.0:
            return f"⚠️ {pconf:.0f}%", "page_warn_conf", f"   ├── ⚠️ {base_page_txt}"
        else:
            return f"🟢 {pconf:.0f}%", default_tag, f"   ├── 📄 {base_page_txt}"

    def _format_file_row_values(self, info):
        # Format
        fmt_str = info.get("format", "-")

        # Pages / Chars (+ OCR Confidence badge)
        n_pages = info.get("n_pages", 1)
        total_chars = info.get("total_chars")
        status_val = info.get("status", "")
        conf_badge, _ = self._get_file_conf_badge_and_tag(info)

        if total_chars is not None and (total_chars > 0 or status_val == "done"):
            chars_str = f"{total_chars:,}"
        else:
            chars_str = "-"

        if conf_badge:
            pages_chars_str = f"{n_pages} / {chars_str} ({conf_badge})"
        else:
            pages_chars_str = f"{n_pages} / {chars_str}"

        # Size (Before / After)
        orig_mb = info.get("size_mb", 0.0)
        comp_mb = info.get("comp_size_mb")
        orig_sz_str = f"{max(0.01, orig_mb):.2f} MB" if orig_mb > 0 else "-"
        comp_sz_str = f"{max(0.01, comp_mb):.2f} MB" if (comp_mb is not None and comp_mb > 0) else "-"
        size_str = f"{orig_sz_str} / {comp_sz_str}"

        # Img. Res. (Orig. / Target)
        orig_dpi = info.get("orig_dpi")
        orig_cmode = info.get("orig_cmode")
        target_dpi = info.get("target_dpi")
        target_cmode = info.get("target_cmode")

        if orig_dpi:
            orig_str = f"{orig_dpi} DPI ({self._format_color_mode(orig_cmode)})" if orig_cmode else f"{orig_dpi} DPI"
        else:
            orig_str = "-"

        if target_dpi:
            target_str = f"{target_dpi} DPI ({self._format_color_mode(target_cmode)})" if target_cmode else f"{target_dpi} DPI"
        else:
            target_str = "-"
        img_res_str = f"{orig_str} / {target_str}"

        # Status
        if status_val == "processing":
            curr_p = info.get("curr_page", 1)
            tot_p = info.get("tot_pages", n_pages)
            status_str = f"{self._tr('status_processing')} ({curr_p}/{tot_p})"
        elif status_val == "done":
            elapsed = info.get("elapsed", 0.0)
            status_str = f"{elapsed:.1f}s" if elapsed > 0 else self._tr("status_ready")
        elif status_val == "interrupted":
            status_str = self._tr("status_interrupted")
        elif status_val == "error":
            status_str = "ERRO"
        else:
            status_str = ""

        return (fmt_str, pages_chars_str, size_str, img_res_str, status_str)

    def _format_page_row_values(self, pinfo):
        # Format (dims)
        w, h = pinfo.get("width"), pinfo.get("height")
        dims_str = f"{w} × {h} px" if (w and h) else "-"

        # Pages / Chars (+ OCR Confidence badge) — without repeating "Pág X /"
        pidx = pinfo.get("page_idx", 0)
        pchars = pinfo.get("chars")
        status_val = pinfo.get("status", "")
        conf_badge, _, _ = self._get_page_conf_badge_tag_and_label(pinfo, pidx)

        if pchars is not None and (pchars > 0 or status_val == "done"):
            pchars_str = f"{pchars:,}"
        else:
            pchars_str = "-"
        if conf_badge:
            pages_chars_str = f"{pchars_str} ({conf_badge})"
        else:
            pages_chars_str = f"{pchars_str}"

        # Size
        size_str = "- / -"

        # Img. Res. (Orig. / Target)
        orig_dpi = pinfo.get("orig_dpi")
        orig_cmode = pinfo.get("orig_cmode")
        target_dpi = pinfo.get("target_dpi")
        target_cmode = pinfo.get("target_cmode")

        if orig_dpi:
            orig_str = f"{orig_dpi} DPI ({self._format_color_mode(orig_cmode)})" if orig_cmode else f"{orig_dpi} DPI"
        else:
            orig_str = "-"

        if target_dpi:
            target_str = f"{target_dpi} DPI ({self._format_color_mode(target_cmode)})" if target_cmode else f"{target_dpi} DPI"
        else:
            target_str = "-"
        img_res_str = f"{orig_str} / {target_str}"

        # Status
        if status_val == "done" and pinfo.get("elapsed"):
            status_str = f"{pinfo['elapsed']:.1f}s"
        elif status_val == "done":
            status_str = self._tr("status_ready")
        elif status_val == "processing":
            status_str = self._tr("status_processing")
        else:
            status_str = ""

        return (dims_str, pages_chars_str, size_str, img_res_str, status_str)

    def _refresh_all_row_displays(self):
        """Re-evaluates and updates localized texts and values for all tree rows and badges."""
        sel_count = len(self.tree.selection())
        tot_count = len(self.found_files)
        self.lbl_selected_badge.configure(text=self._tr("selected_badge").format(sel=sel_count, tot=tot_count))
        self.lbl_badge.configure(text=self._tr("found_files_badge").format(count=tot_count))

        for f_idx, parent_id in enumerate(self.tree.get_children("")):
            info = self.item_to_info.get(parent_id)
            if not info or info.get("type") != "file":
                continue
            row_vals = self._format_file_row_values(info)
            _, file_tag = self._get_file_conf_badge_and_tag(info, f_idx)
            self.tree.item(parent_id, values=row_vals, tags=(file_tag,))

            for p_idx, child_id in enumerate(self.tree.get_children(parent_id)):
                cinfo = self.item_to_info.get(child_id)
                if not cinfo or cinfo.get("type") != "page":
                    continue
                _, ptag, page_label = self._get_page_conf_badge_tag_and_label(cinfo, p_idx)
                child_vals = self._format_page_row_values(cinfo)
                self.tree.item(child_id, text=page_label, values=child_vals, tags=(ptag,))

    def _retranslate_ui(self):
        """Dynamically updates all widget text labels when language is changed."""
        self.lbl_title.configure(text=f"S2POT — Smart Slim PDF OCR Tool v{APP_VERSION}")
        self.chk_help.configure(text=self._tr("help_mode"))
        self.lbl_lang.configure(text=self._tr("language"))

        self.lbl_box1.configure(text=self._tr("box_1_input"))
        self.input_entry.configure(placeholder_text=self._tr("input_placeholder"))
        self.btn_in_browse.configure(text=self._tr("browse"))
        self.btn_tuning_file.configure(text=self._tr("btn_tuning_file"))

        self.chk_filter_pdf.configure(text="PDF")
        self.chk_filter_tiff.configure(text="TIFF")
        self.chk_filter_jpg.configure(text="JPG")
        self.chk_filter_png.configure(text="PNG")
        self.chk_filter_bmp.configure(text="BMP")
        self.chk_filter_webp.configure(text="WEBP")

        self.lbl_box2.configure(text=self._tr("box_2_output"))
        self.lbl_sub_docs.configure(text=f"• {self._tr('sub_documents')}")
        self.lbl_docs_path.configure(text=self._tr("docs_folder_label"))
        self.output_entry.configure(placeholder_text=self._tr("output_placeholder"))
        self.btn_out_browse.configure(text=self._tr("browse"))

        self.chk_pdf.configure(text=self._tr("export_pdf"))
        self.chk_docx.configure(text=self._tr("export_docx"))
        self.chk_doc_subfolders.configure(text=self._tr("organize_doc_subfolders"))

        self.lbl_sub_data.configure(text=f"• {self._tr('sub_data')}")
        self.chk_audit.configure(text=self._tr("export_audit_report"))
        self.chk_txt_from_pdf.configure(text=self._tr("export_txt_from_pdf"))
        self.chk_docx_from_pdf.configure(text=self._tr("export_docx_from_pdf"))
        self.lbl_data_path.configure(text=self._tr("data_folder_label"))
        self.data_entry.configure(placeholder_text=self._tr("data_placeholder"))
        self.btn_data_browse.configure(text=self._tr("browse"))
        self.chk_subfolders.configure(text=self._tr("organize_subfolders"))
        self.btn_explore_results.configure(text=self._tr("btn_explore_results"))

        self.lbl_box3.configure(text=self._tr("box_3_process"))
        self.lbl_dpi.configure(text=self._tr("resolution_dpi"))
        self.chk_deskew.configure(text=self._tr("auto_deskew"))
        self.chk_handwritten.configure(text=self._tr("doc_type_handwritten"))
        self.btn_start.configure(text=self._tr("btn_process"))
        self.btn_stop.configure(text=self._tr("btn_abort"))

        self.lbl_box4.configure(text=self._tr("box_table"))
        self.btn_inspect.configure(text=self._tr("btn_inspect"))
        self.btn_tune_page.configure(text=self._tr("btn_tune_page"))

        # Retranslate table headers
        self.tree.heading("#0", text=self._tr("col_filename"))
        self.tree.heading("Format", text=self._tr("col_format"))
        self.tree.heading("PagesChars", text=self._tr("col_pages_chars"))
        self.tree.heading("Size", text=self._tr("col_size"))
        self.tree.heading("ImgRes", text=self._tr("col_img_res"))
        self.tree.heading("Status", text=self._tr("col_status"))

        # Retranslate table rows and selection badge
        self._refresh_all_row_displays()
        self._on_table_select(None)
        self._attach_tooltips()

        if self.results_window is not None and self.results_window.winfo_exists():
            self.results_window.retranslate(self.current_language)


    def _get_current_export_options(self):
        is_doc_sub_active = (self.chk_doc_subfolders.cget("state") != "disabled") and self.var_doc_subfolders.get()
        is_sub_active = (self.chk_subfolders.cget("state") != "disabled") and self.var_subfolders.get()
        return {
            "export_pdf_original": False,
            "export_pdf_compressed": self.var_pdf.get(),
            "export_pdf": self.var_pdf.get(),
            "export_docx": self.var_docx.get(),
            "organize_doc_subfolders": is_doc_sub_active,
            "export_txt": self.var_txt.get(),
            "export_hocr": self.var_hocr.get(),
            "export_words_json": self.var_json.get(),
            "export_words_csv": self.var_csv.get(),
            "export_words_xlsx": self.var_xlsx.get(),
            "export_audit_report": self.var_audit.get(),
            "export_txt_from_pdf": self.var_txt_from_pdf.get(),
            "export_docx_from_pdf": self.var_docx_from_pdf.get(),
            "organize_subfolders": is_sub_active,
            "extract_tables": self.var_docx.get(),
            "extract_illustrations": self.var_docx.get()
        }

    def _validate_no_input_overwrite(self, test_output_dir=None, test_data_dir=None, target_files=None):
        files_to_check = target_files or getattr(self, "found_files", [])
        if not files_to_check:
            return True

        out_path = test_output_dir if test_output_dir is not None else self.output_entry.get().strip()
        data_path = test_data_dir if test_data_dir is not None else (self.data_entry.get().strip() or out_path)

        if not out_path:
            return True

        opts = self._get_current_export_options()
        overwrites = check_input_overwrite_conflict(files_to_check, out_path, data_dir=data_path, export_options=opts)

        if overwrites:
            orig_file, conflict_out = overwrites[0]
            fname = os.path.basename(orig_file)
            conflict_folder = os.path.dirname(conflict_out)
            msg = self._tr("err_input_overwrite_msg").format(filename=fname, path=conflict_folder)
            messagebox.showerror(self._tr("err_input_overwrite_title"), msg)
            return False
        return True

    def _sync_paths_from_input(self):
        if not self.var_sync_paths.get():
            return
        raw_in = self.input_entry.get().strip().strip('"\'')
        if raw_in:
            opts = self._get_current_export_options()
            if self.found_files and check_input_overwrite_conflict(self.found_files, raw_in, data_dir=raw_in, export_options=opts):
                self.var_doc_subfolders.set(True)
                self.var_subfolders.set(True)
                self._on_option_toggle()

            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, raw_in)
            self.output_dir = raw_in
            self.config_data["last_output_dir"] = raw_in

            self.data_entry.delete(0, "end")
            self.data_dir = raw_in
            self.config_data["last_data_dir"] = raw_in

            save_config(self.config_data)

    def _on_sync_paths_toggle(self):
        self._on_option_toggle()
        if self.var_sync_paths.get():
            self._sync_paths_from_input()

    def _on_option_toggle(self):
        self.config_data["export_pdf"] = self.var_pdf.get()
        self.config_data["export_pdf_compressed"] = self.var_pdf.get()
        self.config_data["export_docx"] = self.var_docx.get()
        self.config_data["extract_tables"] = self.var_docx.get()
        self.config_data["extract_illustrations"] = self.var_docx.get()
        self.config_data["organize_doc_subfolders"] = self.var_doc_subfolders.get()
        self.config_data["export_txt"] = self.var_txt.get()
        self.config_data["export_hocr"] = self.var_hocr.get()
        self.config_data["export_words_json"] = self.var_json.get()
        self.config_data["export_words_csv"] = self.var_csv.get()
        self.config_data["export_words_xlsx"] = self.var_xlsx.get()
        self.config_data["export_audit_report"] = self.var_audit.get()
        self.config_data["export_txt_from_pdf"] = self.var_txt_from_pdf.get()
        self.config_data["export_docx_from_pdf"] = self.var_docx_from_pdf.get()
        self.config_data["organize_subfolders"] = self.var_subfolders.get()
        self.config_data["enable_auto_deskew"] = self.var_deskew.get()
        self.config_data["sync_paths"] = self.var_sync_paths.get()
        self.config_data["filter_pdf"] = self.var_filter_pdf.get()
        self.config_data["filter_tiff"] = self.var_filter_tiff.get()
        self.config_data["filter_jpg"] = self.var_filter_jpg.get()
        self.config_data["filter_png"] = self.var_filter_png.get()
        self.config_data["filter_bmp"] = self.var_filter_bmp.get()
        self.config_data["filter_webp"] = self.var_filter_webp.get()
        save_config(self.config_data)
        self._check_conditional_controls()

    def _on_input_entry_change(self, event=None):
        raw = self.input_entry.get().strip().strip('"\'')
        if raw:
            if raw != self.input_entry.get():
                self.input_entry.delete(0, "end")
                self.input_entry.insert(0, raw)
            if os.path.exists(raw):
                self.input_dir = raw
                self.config_data["last_input_dir"] = raw
                save_config(self.config_data)
                self._sync_paths_from_input()
                self._scan_input_folder()

    def _on_output_entry_change(self, event=None):
        raw = self.output_entry.get().strip().strip('"\'')
        if raw:
            if not self._validate_no_input_overwrite(test_output_dir=raw):
                self.output_entry.delete(0, "end")
                self.output_entry.insert(0, self.output_dir or "")
                return
            if raw != self.output_entry.get():
                self.output_entry.delete(0, "end")
                self.output_entry.insert(0, raw)
            self.output_dir = raw
            self.config_data["last_output_dir"] = raw
            save_config(self.config_data)

    def _on_data_entry_change(self, event=None):
        raw = self.data_entry.get().strip().strip('"\'')
        if raw:
            if not self._validate_no_input_overwrite(test_data_dir=raw):
                self.data_entry.delete(0, "end")
                self.data_entry.insert(0, self.data_dir or "")
                return
            if raw != self.data_entry.get():
                self.data_entry.delete(0, "end")
                self.data_entry.insert(0, raw)
            self.data_dir = raw
            self.config_data["last_data_dir"] = raw
            save_config(self.config_data)

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select Input Folder")
        if d:
            self.input_entry.delete(0, "end")
            self.input_entry.insert(0, d)
            self.input_dir = d
            self.config_data["last_input_dir"] = d
            save_config(self.config_data)
            self._sync_paths_from_input()
            self._scan_input_folder()

    def _browse_output(self):
        d = filedialog.askdirectory(title="Select Output Folder")
        if d:
            if not self._validate_no_input_overwrite(test_output_dir=d):
                return
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, d)
            self.output_dir = d
            self.config_data["last_output_dir"] = d
            save_config(self.config_data)

    def _browse_data(self):
        d = filedialog.askdirectory(title="Select Data Folder")
        if d:
            if not self._validate_no_input_overwrite(test_data_dir=d):
                return
            self.data_entry.delete(0, "end")
            self.data_entry.insert(0, d)
            self.data_dir = d
            self.config_data["last_data_dir"] = d
            save_config(self.config_data)

    def _scan_input_folder(self):
        in_path = self.input_entry.get().strip()
        if not in_path or not os.path.exists(in_path):
            self.lbl_badge.configure(text=self._tr("found_files_badge").format(count=0))
            return



        exts = []
        if self.var_filter_pdf.get():
            exts.append("*.pdf")
        if self.var_filter_tiff.get():
            exts.extend(["*.tif", "*.tiff"])
        if self.var_filter_jpg.get():
            exts.extend(["*.jpg", "*.jpeg"])
        if self.var_filter_png.get():
            exts.append("*.png")
        if self.var_filter_bmp.get():
            exts.append("*.bmp")
        if self.var_filter_webp.get():
            exts.append("*.webp")

        files = []
        for pattern in exts:
            files.extend(glob.glob(os.path.join(in_path, pattern)))
            files.extend(glob.glob(os.path.join(in_path, pattern.upper())))

        # Filter out generated output PDFs (*_compressed.pdf, *_original.pdf) if they are inside the input folder
        filtered_files = []
        for f in sorted(list(set(files))):
            bname_lower = os.path.basename(f).lower()
            if bname_lower.endswith("_compressed.pdf") or bname_lower.endswith("_original.pdf"):
                continue
            filtered_files.append(f)

        self.found_files = filtered_files

        # Check for s2pot_tuning.json badge and update button state
        tuning_file = os.path.join(in_path, "s2pot_tuning.json")
        badge_text = self._tr("found_files_badge").format(count=len(self.found_files))
        if os.path.exists(tuning_file):
            badge_text += f"  |  {self._tr('s2pit_badge_active')}"
            self.lbl_badge.configure(text=badge_text, text_color=("#145a32", "#27ae60"))
            self.btn_tuning_file.configure(state="normal", fg_color="#27ae60", hover_color="#1e8449")
        else:
            self.lbl_badge.configure(text=badge_text, text_color="#3498db")
            self.btn_tuning_file.configure(state="disabled", fg_color="gray50")

        # Load metadata and tuning config if present in folder
        self.folder_metadata = load_folder_metadata(in_path)
        meta_files = self.folder_metadata.get("files", {}) if self.folder_metadata else {}
        self.folder_tuning = load_tuning_config(in_path)

        # Auto-populate file table immediately upon folder scan
        self.tree.delete(*self.tree.get_children())
        self.file_items = {}
        self.item_to_filepath = {}
        self.item_to_info = {}
        self.loaded_pages.clear()

        for f_idx, f in enumerate(self.found_files):
            fname = os.path.basename(f)
            ext = os.path.splitext(f)[1].upper().replace(".", "")
            size_mb = os.path.getsize(f) / (1024 * 1024)
            low_ext = os.path.splitext(f)[1].lower()

            f_meta = meta_files.get(fname)
            inspected = f_meta is not None

            if inspected:
                n_pages = f_meta.get("n_pages", 1)
                orig_dpi = f_meta.get("max_dpi", 300)
                orig_cmode = f_meta.get("color_mode")
                tc = f_meta.get("total_chars")
                comp_size_mb = f_meta.get("comp_size_mb")
                f_status = f_meta.get("status", "")
                f_elapsed = f_meta.get("elapsed", 0.0)
                f_conf = f_meta.get("confidence")
                f_min_conf = f_meta.get("min_page_confidence")
                f_low_pages = f_meta.get("low_conf_pages", 0)
            else:
                n_pages = 1
                orig_dpi = None
                orig_cmode = None
                tc = None
                comp_size_mb = None
                f_status = ""
                f_elapsed = 0.0
                f_conf = None
                f_min_conf = None
                f_low_pages = 0
                try:
                    if low_ext == ".pdf":
                        with fitz.open(f) as doc:
                            n_pages = len(doc)
                    elif low_ext in [".tif", ".tiff"]:
                        with Image.open(f) as timg:
                            n_pages = getattr(timg, "n_frames", 1)
                except Exception:
                    pass

            ui_dpi = self._get_selected_ui_dpi()
            eff_tune = get_effective_file_tuning(self.folder_tuning, fname, page_idx=None, default_dpi=ui_dpi)
            target_dpi = (f_meta.get("target_dpi") if inspected else None) or eff_tune.get("target_dpi")
            target_cmode = (f_meta.get("target_cmode") if inspected else None) or eff_tune.get("color_mode")

            finfo = {
                "type": "file",
                "filepath": f,
                "filename": fname,
                "format": ext,
                "n_pages": n_pages,
                "total_chars": tc,
                "confidence": f_conf,
                "min_page_confidence": f_min_conf,
                "low_conf_pages": f_low_pages,
                "size_mb": size_mb,
                "comp_size_mb": comp_size_mb,
                "orig_dpi": orig_dpi,
                "orig_cmode": orig_cmode,
                "target_dpi": target_dpi,
                "target_cmode": target_cmode,
                "status": f_status,
                "elapsed": f_elapsed
            }

            row_vals = self._format_file_row_values(finfo)
            _, file_tag = self._get_file_conf_badge_and_tag(finfo, f_idx)
            item_id = self.tree.insert("", "end", text=f"📄 {fname}", values=row_vals, open=False, tags=(file_tag,))
            self.file_items[f] = item_id
            self.item_to_filepath[item_id] = f
            self.item_to_info[item_id] = finfo

            # If inspected metadata with pages is already available, pre-insert immediately for instant 0ms expansion
            if f_meta and "pages" in f_meta and len(f_meta["pages"]) > 0:
                for p_idx, pinfo in enumerate(f_meta["pages"]):
                    pidx = pinfo.get("page_idx", p_idx)
                    ptune = get_effective_file_tuning(self.folder_tuning, fname, page_idx=pidx, default_dpi=ui_dpi)
                    cinfo = {
                        "type": "page",
                        "filepath": f,
                        "filename": fname,
                        "page_idx": pidx,
                        "width": pinfo.get("width"),
                        "height": pinfo.get("height"),
                        "chars": pinfo.get("chars"),
                        "confidence": pinfo.get("confidence"),
                        "orig_dpi": pinfo.get("dpi") or pinfo.get("orig_dpi"),
                        "orig_cmode": pinfo.get("color_mode") or pinfo.get("orig_cmode"),
                        "target_dpi": pinfo.get("target_dpi") or ptune.get("target_dpi"),
                        "target_cmode": pinfo.get("target_cmode") or ptune.get("color_mode"),
                        "status": pinfo.get("status", ""),
                        "elapsed": pinfo.get("elapsed", 0.0)
                    }
                    _, ptag, page_label = self._get_page_conf_badge_tag_and_label(cinfo, p_idx)
                    child_vals = self._format_page_row_values(cinfo)
                    child_id = self.tree.insert(item_id, "end", text=page_label, values=child_vals, tags=(ptag,))
                    self.item_to_filepath[child_id] = f
                    self.item_to_info[child_id] = cinfo
                self.loaded_pages.add(f)
            elif n_pages > 1 or low_ext in [".pdf", ".tif", ".tiff"]:
                dummy_id = f"{item_id}_dummy"
                self.tree.insert(item_id, "end", iid=dummy_id, text=self._tr("loading_pages"), values=())

        if hasattr(self, "_update_gridlines_fn"):
            self.after(50, self._update_gridlines_fn)

        self._on_table_select(None)
        self._check_conditional_controls()
        if self.results_window is not None and self.results_window.winfo_exists():
            self.results_window.refresh_results()

    def _on_tree_open(self, event):
        """Lazy load page details when user clicks expand (+) on a file node."""
        ui_dpi = self._get_selected_ui_dpi()
        for parent_id in self.tree.get_children(""):
            if self.tree.item(parent_id, "open"):
                fpath = self.item_to_filepath.get(parent_id)
                if not fpath or fpath in self.loaded_pages:
                    continue

                for child in self.tree.get_children(parent_id):
                    if child.endswith("_dummy"):
                        self.tree.delete(child)

                fname = os.path.basename(fpath)
                f_meta = self.folder_metadata.get("files", {}).get(fname) if self.folder_metadata else None

                if f_meta and "pages" in f_meta and len(f_meta["pages"]) > 0:
                    for pinfo in f_meta["pages"]:
                        pidx = pinfo.get("page_idx", 0)
                        ptune = get_effective_file_tuning(self.folder_tuning, fname, page_idx=pidx, default_dpi=ui_dpi)
                        cinfo = {
                            "type": "page",
                            "filepath": fpath,
                            "filename": fname,
                            "page_idx": pidx,
                            "width": pinfo.get("width"),
                            "height": pinfo.get("height"),
                            "chars": pinfo.get("chars"),
                            "confidence": pinfo.get("confidence"),
                            "orig_dpi": pinfo.get("dpi") or pinfo.get("orig_dpi"),
                            "orig_cmode": pinfo.get("color_mode") or pinfo.get("orig_cmode"),
                            "target_dpi": pinfo.get("target_dpi") or ptune.get("target_dpi"),
                            "target_cmode": pinfo.get("target_cmode") or ptune.get("color_mode"),
                            "status": pinfo.get("status", ""),
                            "elapsed": pinfo.get("elapsed", 0.0)
                        }
                        _, ptag, page_label = self._get_page_conf_badge_tag_and_label(cinfo, pidx)
                        child_vals = self._format_page_row_values(cinfo)
                        child_id = self.tree.insert(parent_id, "end", text=page_label, values=child_vals, tags=(ptag,))
                        self.item_to_filepath[child_id] = fpath
                        self.item_to_info[child_id] = cinfo
                else:
                    # NOT inspected yet: simply list pages without heavy extraction!
                    parent_info = self.item_to_info.get(parent_id, {})
                    n_pages = parent_info.get("n_pages", 1)
                    for pidx in range(n_pages):
                        ptune = get_effective_file_tuning(self.folder_tuning, fname, page_idx=pidx, default_dpi=ui_dpi)
                        cinfo = {
                            "type": "page",
                            "filepath": fpath,
                            "filename": fname,
                            "page_idx": pidx,
                            "width": None,
                            "height": None,
                            "chars": None,
                            "orig_dpi": None,
                            "orig_cmode": None,
                            "target_dpi": ptune.get("target_dpi"),
                            "target_cmode": ptune.get("color_mode"),
                            "status": "",
                            "elapsed": 0.0
                        }
                        page_label = f"   ├── 📄 {self._tr('tree_item_page').format(idx=pidx + 1)}"
                        child_vals = self._format_page_row_values(cinfo)
                        ptag = "page_even" if pidx % 2 == 0 else "page_odd"
                        child_id = self.tree.insert(parent_id, "end", text=page_label, values=child_vals, tags=(ptag,))
                        self.item_to_filepath[child_id] = fpath
                        self.item_to_info[child_id] = cinfo

                self.loaded_pages.add(fpath)

    def _open_tuning_studio(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("S2POT", self._tr("no_page_selected_msg"), parent=self)
            return
        item_id = sel[0]
        info = self.item_to_info.get(item_id)
        if not info:
            return
        if info.get("type") == "page":
            self._open_studio_for(info["filepath"], info.get("page_idx", 0))
        elif info.get("type") == "file":
            self._open_studio_for(info["filepath"], 0)

    def _open_studio_for(self, fpath, page_idx=0):
        if not fpath or not os.path.exists(fpath):
            return
        if self.active_studio is not None and self.active_studio.winfo_exists():
            self.active_studio.lift()
            self.active_studio.focus_force()
            return

        self.active_studio = S2POTPageViewerWindow(
            self,
            file_path=fpath,
            page_idx=page_idx,
            lang=self.current_language,
            on_save_callback=self._on_tuning_saved
        )

    def _on_tree_item_activate(self, event=None):
        if event and hasattr(event, "x") and hasattr(event, "y"):
            elt = self.tree.identify_element(event.x, event.y)
            if "indicator" in str(elt).lower():
                return
        sel = self.tree.selection()
        if not sel:
            return
        item_id = sel[0]
        info = self.item_to_info.get(item_id)
        if not info:
            return
        if info.get("type") == "page":
            self._open_studio_for(info["filepath"], info.get("page_idx", 0))
        elif info.get("type") == "file":
            is_open = self.tree.item(item_id, "open")
            self.tree.item(item_id, open=not is_open)

    def _on_tuning_saved(self):
        in_path = self.input_entry.get().strip() or self.input_dir
        if in_path and os.path.exists(in_path):
            ui_dpi = self._get_selected_ui_dpi()
            self.folder_tuning = load_tuning_config(in_path)
            for item_id, item_info in self.item_to_info.items():
                fname = item_info.get("filename")
                pidx = item_info.get("page_idx") if item_info.get("type") == "page" else None
                eff = get_effective_file_tuning(self.folder_tuning, fname, page_idx=pidx, default_dpi=ui_dpi)
                item_info["target_dpi"] = eff.get("target_dpi")
                item_info["target_cmode"] = eff.get("color_mode")
            self._refresh_all_row_displays()

            # Update badge and button state
            tuning_file = os.path.join(in_path, "s2pot_tuning.json")
            badge_text = self._tr("found_files_badge").format(count=len(self.found_files))
            if os.path.exists(tuning_file):
                badge_text += f"  |  {self._tr('s2pit_badge_active')}"
                self.lbl_badge.configure(text=badge_text, text_color=("#145a32", "#27ae60"))
                self.btn_tuning_file.configure(state="normal", fg_color="#27ae60", hover_color="#1e8449")

        if self.results_window is not None and self.results_window.winfo_exists():
            self.results_window.refresh_results()

    def _open_results_browser(self):
        docs_path = self.output_entry.get().strip()
        data_path = self.data_entry.get().strip() or docs_path

        if not docs_path or not os.path.exists(docs_path):
            messagebox.showwarning("Aviso", "A pasta de documentos de saída ainda não foi configurada ou não existe.")
            return

        if self.results_window is not None and self.results_window.winfo_exists():
            self.results_window.destroy()

        self.results_window = OutputBrowserWindow(
            self,
            docs_dir=docs_path,
            data_dir=data_path,
            lang=self.current_language
        )

    def _open_tuning_file(self):
        in_path = self.input_entry.get().strip()
        if in_path:
            tuning_file = os.path.join(in_path, "s2pot_tuning.json")
            if os.path.exists(tuning_file):
                try:
                    os.startfile(tuning_file)
                except Exception as e:
                    messagebox.showerror("S2POT", f"Error opening s2pot_tuning.json: {e}")

    def _get_file_max_dpi(self, fpath):
        """Helper to calculate the highest image resolution (DPI) in a document or image file."""
        ext = os.path.splitext(fpath)[1].lower()
        max_dpi = 0
        try:
            if ext == ".pdf":
                doc = fitz.open(fpath)
                for page in doc:
                    pt_w = page.rect.width
                    pt_h = page.rect.height
                    image_list = page.get_images()
                    if image_list:
                        for img in image_list:
                            xref = img[0]
                            try:
                                base_img = doc.extract_image(xref)
                                w = base_img.get("width", 0)
                                h = base_img.get("height", 0)
                                if pt_w > 0 and pt_h > 0 and w > 0 and h > 0:
                                    dpi_x = (w * 72.0) / pt_w
                                    dpi_y = (h * 72.0) / pt_h
                                    max_dpi = max(max_dpi, dpi_x, dpi_y)
                            except Exception:
                                pass
                    else:
                        pix = page.get_pixmap()
                        dpi_x = (pix.width * 72.0) / pt_w if pt_w > 0 else 300
                        dpi_y = (pix.height * 72.0) / pt_h if pt_h > 0 else 300
                        max_dpi = max(max_dpi, dpi_x, dpi_y)
                doc.close()

            elif ext in [".tif", ".tiff"]:
                tiff = Image.open(fpath)
                n_frames = getattr(tiff, "n_frames", 1)
                for i in range(n_frames):
                    tiff.seek(i)
                    dpi = tiff.info.get("dpi")
                    if dpi and isinstance(dpi, tuple) and len(dpi) >= 2:
                        dpi_val = max(float(dpi[0]), float(dpi[1]))
                        max_dpi = max(max_dpi, dpi_val)
                    elif dpi and isinstance(dpi, (int, float)):
                        max_dpi = max(max_dpi, float(dpi))

            elif ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
                img = Image.open(fpath)
                dpi = img.info.get("dpi")
                if dpi and isinstance(dpi, tuple) and len(dpi) >= 2:
                    dpi_val = max(float(dpi[0]), float(dpi[1]))
                    max_dpi = max(max_dpi, dpi_val)
                elif dpi and isinstance(dpi, (int, float)):
                    max_dpi = max(max_dpi, float(dpi))

        except Exception as e:
            print(f"Error calculating DPI for {fpath}: {e}")

        if max_dpi <= 0:
            max_dpi = 300.0

        return int(round(max_dpi))

    def _inspect_files(self):
        """Inspects selected/scanned input files using non-blocking InspectionProgressDialog and persists to s2pot_metadata.json."""
        selected_ids = self.tree.selection()
        if selected_ids:
            target_files = []
            for iid in selected_ids:
                fpath = self.item_to_filepath.get(iid)
                if fpath and fpath not in target_files:
                    target_files.append(fpath)
        else:
            target_files = list(self.found_files)

        if not target_files:
            messagebox.showwarning("Aviso", "Nenhum ficheiro disponível para inspecionar.", parent=self)
            return

        folder = self.input_dir or os.path.dirname(target_files[0])
        InspectionProgressDialog(
            self,
            file_paths=target_files,
            folder_path=folder,
            lang=self.current_language,
            on_complete=self._on_inspection_finished
        )

    def _on_inspection_finished(self):
        """Refreshes table and metadata cache after inspection completes."""
        in_path = os.path.normpath((self.input_entry.get().strip() or self.input_dir or "").strip('"\''))
        self.folder_metadata = load_folder_metadata(in_path)
        if not self.folder_metadata:
            return

        ui_dpi = self._get_selected_ui_dpi()
        meta_files = self.folder_metadata.get("files", {})
        for fpath, item_id in self.file_items.items():
            fname = os.path.basename(fpath)
            if fname in meta_files:
                f_meta = meta_files[fname]
                finfo = self.item_to_info.get(item_id, {})
                finfo["n_pages"] = f_meta.get("n_pages", 1)
                finfo["orig_dpi"] = f_meta.get("max_dpi", 300)
                finfo["orig_cmode"] = f_meta.get("color_mode")
                if f_meta.get("total_chars") is not None:
                    finfo["total_chars"] = f_meta.get("total_chars", 0)
                if f_meta.get("confidence") is not None:
                    finfo["confidence"] = f_meta.get("confidence")
                if f_meta.get("min_page_confidence") is not None:
                    finfo["min_page_confidence"] = f_meta.get("min_page_confidence")
                if f_meta.get("low_conf_pages") is not None:
                    finfo["low_conf_pages"] = f_meta.get("low_conf_pages", 0)
                self.item_to_info[item_id] = finfo

                # Pre-insert all page rows immediately so expanding the node is 100% instantaneous (0ms)
                for child in self.tree.get_children(item_id):
                    if child in self.item_to_info:
                        del self.item_to_info[child]
                    if child in self.item_to_filepath:
                        del self.item_to_filepath[child]
                    self.tree.delete(child)

                for p_idx, pinfo in enumerate(f_meta.get("pages", [])):
                    pidx = pinfo.get("page_idx", p_idx)
                    ptune = get_effective_file_tuning(self.folder_tuning, fname, page_idx=pidx, default_dpi=ui_dpi)
                    cinfo = {
                        "type": "page",
                        "filepath": fpath,
                        "filename": fname,
                        "page_idx": pidx,
                        "width": pinfo.get("width"),
                        "height": pinfo.get("height"),
                        "chars": pinfo.get("chars"),
                        "confidence": pinfo.get("confidence"),
                        "orig_dpi": pinfo.get("dpi") or pinfo.get("orig_dpi"),
                        "orig_cmode": pinfo.get("color_mode") or pinfo.get("orig_cmode"),
                        "target_dpi": pinfo.get("target_dpi") or ptune.get("target_dpi"),
                        "target_cmode": pinfo.get("target_cmode") or ptune.get("color_mode"),
                        "status": pinfo.get("status", ""),
                        "elapsed": pinfo.get("elapsed", 0.0)
                    }
                    _, ptag, page_label = self._get_page_conf_badge_tag_and_label(cinfo, p_idx)
                    child_vals = self._format_page_row_values(cinfo)
                    child_id = self.tree.insert(item_id, "end", text=page_label, values=child_vals, tags=(ptag,))
                    self.item_to_filepath[child_id] = fpath
                    self.item_to_info[child_id] = cinfo

                self.loaded_pages.add(fpath)

        self._refresh_all_row_displays()
        if hasattr(self, "_update_gridlines_fn"):
            self.after(50, self._update_gridlines_fn)
        if self.results_window is not None and self.results_window.winfo_exists():
            self.results_window.refresh_results()

    def _find_file_item_id(self, filepath=None, filename=None):
        """Reliably finds the tree item_id for a file by exact path, normalized path, or basename."""
        if filepath and filepath in self.file_items:
            return self.file_items[filepath], filepath

        if filepath:
            norm_target = os.path.normcase(os.path.normpath(filepath))
            for k, iid in self.file_items.items():
                if os.path.normcase(os.path.normpath(k)) == norm_target:
                    return iid, k

        target_name = os.path.basename(filepath) if filepath else filename
        if target_name:
            target_lower = target_name.lower()
            target_no_ext = os.path.splitext(target_lower)[0]
            for k, iid in self.file_items.items():
                bname = os.path.basename(k).lower()
                if bname == target_lower or os.path.splitext(bname)[0] == target_no_ext:
                    return iid, k

        return None, filepath

    def _check_conditional_controls(self):
        """Enforces conditional UI rules for Document Subfolders and Data Destination folder."""
        active_doc_targets = sum([
            self.var_pdf.get(),
            self.var_docx.get()
        ])

        if active_doc_targets > 1:
            self.chk_doc_subfolders.configure(state="normal")
        else:
            self.chk_doc_subfolders.configure(state="disabled")
            self.var_doc_subfolders.set(False)

        has_data_exports = any([
            self.var_txt.get(),
            self.var_hocr.get(),
            self.var_json.get(),
            self.var_csv.get(),
            self.var_xlsx.get(),
            self.var_audit.get(),
            self.var_txt_from_pdf.get(),
            self.var_docx_from_pdf.get()
        ])

        if has_data_exports:
            self.data_entry.configure(state="normal")
            self.btn_data_browse.configure(state="normal")
            self.chk_subfolders.configure(state="normal")
        else:
            self.data_entry.configure(state="disabled")
            self.btn_data_browse.configure(state="disabled")
            self.chk_subfolders.configure(state="disabled")
            self.var_subfolders.set(False)

    def _on_table_select(self, event):
        """Updates selection badge and 'Ver Páginas' button state when user selects rows in the table."""
        selected_items = self.tree.selection()
        n_selected = len(selected_items)
        n_total = len(self.found_files)
        self.lbl_selected_badge.configure(text=self._tr("selected_badge").format(sel=n_selected, tot=n_total))

        can_tune = False
        for iid in selected_items:
            info = self.item_to_info.get(iid)
            if info and info.get("type") in ("file", "page") and info.get("filepath") and os.path.exists(info["filepath"]):
                can_tune = True
                break

        if can_tune:
            self.btn_tune_page.configure(state="normal", fg_color="#6c3483", hover_color="#512e5f")
        else:
            self.btn_tune_page.configure(state="disabled", fg_color="gray50")

    def _sort_table(self, col):
        """Sorts the table columns in ascending or descending order."""
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False

        if col == "#0":
            items = [(self.tree.item(k, "text"), k) for k in self.tree.get_children('')]
        else:
            items = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]

        def parse_sort_key(val_tuple):
            val, item_id = val_tuple
            info = self.item_to_info.get(item_id, {})
            if col == "#0":
                return str(val).lower()
            elif col == "Format":
                return str(info.get("format", "")).lower()
            elif col == "PagesChars":
                n_pages = info.get("n_pages", 0) or 0
                tc = info.get("total_chars", 0) or 0
                return (n_pages, tc)
            elif col == "Size":
                return float(info.get("size_mb", 0.0) or 0.0)
            elif col == "ImgRes":
                return int(info.get("orig_dpi", 0) or 0)
            elif col == "Status":
                return str(info.get("status", "")).lower()
            return str(val).lower()

        items.sort(key=parse_sort_key, reverse=self.sort_reverse)

        for index, (val, k) in enumerate(items):
            self.tree.move(k, '', index)

    def _start_batch_process(self):
        in_path = self.input_entry.get().strip()
        out_path = self.output_entry.get().strip()

        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("Error", "Please select a valid Input Folder.")
            return

        if not out_path:
            messagebox.showerror("Error", "Please select a valid Output Folder.")
            return

        if not self.found_files:
            messagebox.showwarning("Warning", "No matching files found in input directory.")
            return

        selected_ids = self.tree.selection()
        if selected_ids:
            target_files = list(dict.fromkeys([self.item_to_filepath[iid] for iid in selected_ids if iid in self.item_to_filepath]))
        else:
            target_files = list(self.found_files)

        if not target_files:
            messagebox.showwarning("Warning", "No files selected for processing.")
            return

        dpi_str = self.cmb_dpi.get()
        dpi_val = 300
        if "150" in dpi_str:
            dpi_val = 150
        elif "600" in dpi_str:
            dpi_val = 600

        is_doc_sub_active = (self.chk_doc_subfolders.cget("state") != "disabled") and self.var_doc_subfolders.get()
        is_sub_active = (self.chk_subfolders.cget("state") != "disabled") and self.var_subfolders.get()

        export_opts = self._get_current_export_options()

        data_dest = self.data_entry.get().strip() or out_path
        input_overwrites = check_input_overwrite_conflict(target_files, out_path, data_dir=data_dest, export_options=export_opts)
        if input_overwrites:
            orig_file, conflict_out = input_overwrites[0]
            fname = os.path.basename(orig_file)
            conflict_folder = os.path.dirname(conflict_out)
            msg = self._tr("err_input_overwrite_msg").format(filename=fname, path=conflict_folder)
            messagebox.showerror(self._tr("err_input_overwrite_title"), msg)
            return

        expected_outputs = get_expected_output_files(target_files, out_path, data_dir=data_dest, export_options=export_opts)
        conflicting_files = [p for p in expected_outputs if os.path.exists(p)]

        if conflicting_files:
            target_norm_set = {os.path.normcase(os.path.abspath(f)) for f in target_files}
            has_input_in_conflicts = any(os.path.normcase(os.path.abspath(c)) in target_norm_set for c in conflicting_files)
            allow_ow = not has_input_in_conflicts

            dlg = ConflictDialog(self, len(conflicting_files), allow_overwrite=allow_ow, has_input_conflict=has_input_in_conflicts)
            if dlg.result == "cancel":
                return
            elif dlg.result == "archive":
                self._archive_conflicting_files(conflicting_files)

        self.is_processing = True
        self.abort_requested = False
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.set(0)

        def run_thread():
            try:
                batch_process_files(
                    target_files,
                    out_path,
                    data_dir=data_dest,
                    export_options=export_opts,
                    enable_auto_deskew=self.var_deskew.get(),
                    target_dpi=dpi_val,
                    doc_type="handwritten" if self.var_handwritten.get() else "printed",
                    progress_callback=lambda cf, tf, fn, cp, tp, m: self.after(0, self._update_progress, cf, tf, fn, cp, tp, m),
                    file_complete_callback=lambda fp, rec: self.after(0, self._on_file_complete, fp, rec),
                    abort_check_fn=lambda: self.abort_requested
                )
                self.after(0, self._on_batch_complete, len(target_files))
            except InterruptedError:
                self.after(0, self._handle_interrupted_status, target_files)
            except Exception as e:
                self.after(0, lambda err=e: messagebox.showerror("Error", f"Error during processing: {err}"))
            finally:
                self.after(0, self._reset_process_ui)

        threading.Thread(target=run_thread, daemon=True).start()

    def _on_batch_complete(self, processed_count):
        self._refresh_all_row_displays()
        if self.results_window is not None and self.results_window.winfo_exists():
            try:
                self.results_window.refresh_results()
            except Exception:
                pass
        self.update_idletasks()
        messagebox.showinfo("Completed", f"Batch processing finished for {processed_count} file(s)!")

    def _archive_conflicting_files(self, conflicting_files):
        """Archives existing conflicting target files into a timestamped 'Archive_YYYYMMDD_HHMMSS' subfolder."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for conf_path in conflicting_files:
            if os.path.exists(conf_path):
                try:
                    parent_dir = os.path.dirname(conf_path)
                    archive_dir = os.path.join(parent_dir, f"Archive_{ts}")
                    os.makedirs(archive_dir, exist_ok=True)
                    fname = os.path.basename(conf_path)
                    target_path = os.path.join(archive_dir, fname)
                    if os.path.exists(target_path):
                        b_name, ext = os.path.splitext(fname)
                        target_path = os.path.join(archive_dir, f"{b_name}_{ts}{ext}")
                    shutil.move(conf_path, target_path)
                except Exception as e:
                    print(f"Error archiving file {conf_path}: {e}")

    def _handle_interrupted_status(self, target_files):
        """Sets status of current and remaining target files to INTERRUPTED when aborted."""
        for fpath in target_files:
            item_id, _ = self._find_file_item_id(filepath=fpath)
            if item_id:
                info = self.item_to_info.get(item_id)
                if info and info.get("status") in ["processing", ""]:
                    info["status"] = "interrupted"
                    self.tree.item(item_id, values=self._format_file_row_values(info))
        self.update_idletasks()
        messagebox.showwarning("Interrupted", "Processing was interrupted by user.")

    def _abort_batch_process(self):
        self.abort_requested = True
        self.btn_stop.configure(state="disabled")

    def _reset_process_ui(self):
        self.is_processing = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.progress.set(1.0)

    def _update_progress(self, curr_file, tot_files, filename, curr_page, tot_pages, msg):
        pct = (curr_file - 1 + (curr_page / float(max(1, tot_pages)))) / float(max(1, tot_files))
        self.progress.set(pct)

        in_dir = (self.input_entry.get().strip() or self.input_dir or "").strip('"\'')
        filepath = os.path.join(in_dir, filename) if in_dir else filename
        item_id, real_fpath = self._find_file_item_id(filepath=filepath, filename=filename)
        self.current_processing_file = real_fpath

        if item_id:
            info = self.item_to_info.get(item_id)
            if info:
                info["status"] = "processing"
                info["curr_page"] = curr_page
                info["tot_pages"] = tot_pages
                info["n_pages"] = tot_pages
                self.tree.item(item_id, values=self._format_file_row_values(info))

            for child_id in self.tree.get_children(item_id):
                cinfo = self.item_to_info.get(child_id)
                if not cinfo or cinfo.get("type") != "page":
                    continue
                pidx = cinfo.get("page_idx", 0)
                if pidx == curr_page - 1:
                    cinfo["status"] = "processing"
                    self.tree.item(child_id, values=self._format_page_row_values(cinfo))
                elif pidx < curr_page - 1 and cinfo.get("status") == "processing":
                    cinfo["status"] = "done"
                    self.tree.item(child_id, values=self._format_page_row_values(cinfo))

        self.update_idletasks()

    def _on_file_complete(self, filepath, record):
        item_id, real_fpath = self._find_file_item_id(filepath=filepath, filename=record.get("filename"))
        if not item_id:
            return

        info = self.item_to_info.get(item_id)
        if info:
            info["status"] = "done"
            info["n_pages"] = record.get("pages", info.get("n_pages", 1))
            info["total_chars"] = record.get("total_chars", 0)
            info["confidence"] = record.get("confidence")
            info["min_page_confidence"] = record.get("min_page_confidence")
            info["low_conf_pages"] = record.get("low_conf_pages", 0)
            info["elapsed"] = record.get("duration_sec", 0.0)
            if record.get("orig_dpi"):
                info["orig_dpi"] = record["orig_dpi"]
            if record.get("orig_cmode"):
                info["orig_cmode"] = record["orig_cmode"]
            if record.get("target_dpi"):
                info["target_dpi"] = record["target_dpi"]
            if record.get("target_cmode"):
                info["target_cmode"] = record["target_cmode"]

            comp_pdf_path = record.get("output_pdf_compressed") or record.get("output_pdf") or record.get("output_primary_path")
            if comp_pdf_path and os.path.exists(comp_pdf_path):
                try:
                    info["comp_size_mb"] = os.path.getsize(comp_pdf_path) / (1024 * 1024)
                except Exception:
                    info["comp_size_mb"] = record.get("pdf_size_mb")
            elif record.get("pdf_size_mb") is not None and record.get("pdf_size_mb") > 0:
                info["comp_size_mb"] = record.get("pdf_size_mb")

            try:
                f_idx = list(self.tree.get_children("")).index(item_id)
            except Exception:
                f_idx = 0
            _, file_tag = self._get_file_conf_badge_and_tag(info, f_idx)
            self.tree.item(item_id, values=self._format_file_row_values(info), tags=(file_tag,))

        # Update page child rows in the tree with processed page metrics
        pages_info = record.get("pages_info", [])
        if pages_info:
            fname = os.path.basename(real_fpath) if real_fpath else record.get("filename", "")
            for child in self.tree.get_children(item_id):
                if child in self.item_to_info:
                    del self.item_to_info[child]
                if child in self.item_to_filepath:
                    del self.item_to_filepath[child]
                self.tree.delete(child)

            meta_pages = []
            for p_idx, pinfo in enumerate(pages_info):
                pidx = pinfo.get("page_idx", p_idx)
                cinfo = {
                    "type": "page",
                    "filepath": real_fpath,
                    "filename": fname,
                    "page_idx": pidx,
                    "width": pinfo.get("width"),
                    "height": pinfo.get("height"),
                    "chars": pinfo.get("chars", 0),
                    "confidence": pinfo.get("confidence"),
                    "orig_dpi": pinfo.get("orig_dpi"),
                    "orig_cmode": pinfo.get("orig_cmode"),
                    "target_dpi": pinfo.get("target_dpi"),
                    "target_cmode": pinfo.get("target_cmode"),
                    "status": "done",
                    "elapsed": pinfo.get("elapsed", 0.0)
                }
                _, ptag, page_label = self._get_page_conf_badge_tag_and_label(cinfo, p_idx)
                child_vals = self._format_page_row_values(cinfo)
                child_id = self.tree.insert(item_id, "end", text=page_label, values=child_vals, tags=(ptag,))
                self.item_to_filepath[child_id] = real_fpath
                self.item_to_info[child_id] = cinfo

                meta_pages.append({
                    "page_idx": pidx,
                    "width": pinfo.get("width"),
                    "height": pinfo.get("height"),
                    "dpi": pinfo.get("orig_dpi"),
                    "color_mode": pinfo.get("orig_cmode"),
                    "target_dpi": pinfo.get("target_dpi"),
                    "target_cmode": pinfo.get("target_cmode"),
                    "chars": pinfo.get("chars", 0),
                    "confidence": pinfo.get("confidence"),
                    "status": "done",
                    "elapsed": pinfo.get("elapsed", 0.0)
                })

            if real_fpath:
                self.loaded_pages.add(real_fpath)

            # Persist updated metadata to folder_metadata and s2pot_metadata.json
            in_dir = os.path.normpath((self.input_entry.get().strip() or self.input_dir or "").strip('"\''))
            if in_dir and os.path.exists(in_dir) and fname:
                if not self.folder_metadata or not isinstance(self.folder_metadata, dict):
                    self.folder_metadata = {
                        "version": "1.0",
                        "folder": in_dir,
                        "updated_at": "",
                        "files": {}
                    }
                files_dict = self.folder_metadata.setdefault("files", {})
                files_dict[fname] = {
                    "filename": fname,
                    "format": info.get("format", "") if info else "",
                    "size_mb": round(info.get("size_mb", 0.0), 2) if info else 0.0,
                    "comp_size_mb": info.get("comp_size_mb") if info else None,
                    "n_pages": info.get("n_pages", len(meta_pages)) if info else len(meta_pages),
                    "max_dpi": info.get("orig_dpi", 300) if info else 300,
                    "color_mode": info.get("orig_cmode", "color") if info else "color",
                    "target_dpi": info.get("target_dpi") if info else None,
                    "target_cmode": info.get("target_cmode") if info else None,
                    "total_chars": info.get("total_chars", 0) if info else 0,
                    "confidence": info.get("confidence") if info else None,
                    "min_page_confidence": info.get("min_page_confidence") if info else None,
                    "low_conf_pages": info.get("low_conf_pages", 0) if info else 0,
                    "status": "done",
                    "elapsed": info.get("elapsed", 0.0) if info else 0.0,
                    "pages": meta_pages
                }
                self.folder_metadata["updated_at"] = datetime.now().isoformat()
                save_folder_metadata(in_dir, self.folder_metadata)

        if self.results_window is not None and self.results_window.winfo_exists():
            try:
                self.results_window.refresh_results()
            except Exception:
                pass

        self.update_idletasks()


def launch_gui():
    app = S2POTApp()
    app.mainloop()


if __name__ == "__main__":
    launch_gui()
