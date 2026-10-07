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
from gui.tooltip import ToolTip

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
            font=ctk.CTkFont(size=17, weight="bold"),
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
            command=self._on_overwrite
        )
        self.btn_overwrite.pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text=parent._tr("conflict_btn_archive"),
            fg_color="#2980b9",
            hover_color="#1f618d",
            width=145,
            command=self._on_archive
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text=parent._tr("conflict_btn_cancel"),
            fg_color="gray",
            hover_color="#555555",
            width=110,
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
            font=ctk.CTkFont(size=20, weight="bold")
        )
        self.lbl_title.grid(row=0, column=0, padx=20, pady=12, sticky="w")

        h_right = ctk.CTkFrame(header, fg_color="transparent")
        h_right.grid(row=0, column=2, padx=20, pady=12, sticky="e")

        self.var_help_mode = ctk.BooleanVar(value=self.config_data.get("help_mode", True))
        self.chk_help = ctk.CTkCheckBox(
            h_right,
            text=self._tr("help_mode"),
            variable=self.var_help_mode,
            font=ctk.CTkFont(weight="bold"),
            command=self._on_help_toggle
        )
        self.chk_help.pack(side="left", padx=(0, 15))

        self.lbl_lang = ctk.CTkLabel(h_right, text=self._tr("language"))
        self.lbl_lang.pack(side="left", padx=(0, 5))

        self.cmb_lang = ctk.CTkOptionMenu(
            h_right,
            values=list(LANGUAGES.values()),
            width=140,
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
        self.input_entry = ctk.CTkEntry(f_in, placeholder_text=self._tr("input_placeholder"))
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.input_entry.bind("<Return>", self._on_input_entry_change)
        self.input_entry.bind("<FocusOut>", self._on_input_entry_change)
        self.btn_in_browse = ctk.CTkButton(f_in, text=self._tr("browse"), width=75, command=self._browse_input)
        self.btn_in_browse.pack(side="right")

        self.var_sync_paths = ctk.BooleanVar(value=self.config_data.get("sync_paths", True))
        self.chk_sync_paths = ctk.CTkCheckBox(
            self.box1,
            text=self._tr("sync_paths"),
            variable=self.var_sync_paths,
            command=self._on_sync_paths_toggle
        )
        self.chk_sync_paths.pack(anchor="w", padx=12, pady=(2, 2))

        self.lbl_badge = ctk.CTkLabel(self.box1, text="Ficheiros encontrados: 0", font=ctk.CTkFont(size=11, weight="bold"), text_color="#3498db")
        self.lbl_badge.pack(anchor="w", padx=12, pady=(2, 2))

        self.btn_open_s2pit = ctk.CTkButton(
            self.box1,
            text=self._tr("btn_open_s2pit"),
            fg_color="#8e44ad",
            hover_color="#732d91",
            height=26,
            command=self._open_s2pit_preview
        )
        self.btn_open_s2pit.pack(fill="x", padx=10, pady=(2, 6))

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
        self.chk_pdf = ctk.CTkCheckBox(f_cb1, text="PDF (0)", variable=self.var_filter_pdf, command=self._scan_input_folder)
        self.chk_pdf.pack(side="left", padx=4)
        self.chk_tiff = ctk.CTkCheckBox(f_cb1, text="TIFF (0)", variable=self.var_filter_tiff, command=self._scan_input_folder)
        self.chk_tiff.pack(side="left", padx=4)
        self.chk_jpg = ctk.CTkCheckBox(f_cb1, text="JPG (0)", variable=self.var_filter_jpg, command=self._scan_input_folder)
        self.chk_jpg.pack(side="left", padx=4)

        f_cb2 = ctk.CTkFrame(fmt_frame, fg_color="transparent")
        f_cb2.pack(fill="x", padx=6, pady=(0, 3))
        self.chk_png = ctk.CTkCheckBox(f_cb2, text="PNG (0)", variable=self.var_filter_png, command=self._scan_input_folder)
        self.chk_png.pack(side="left", padx=4)
        self.chk_bmp = ctk.CTkCheckBox(f_cb2, text="BMP (0)", variable=self.var_filter_bmp, command=self._scan_input_folder)
        self.chk_bmp.pack(side="left", padx=4)
        self.chk_webp = ctk.CTkCheckBox(f_cb2, text="WEBP (0)", variable=self.var_filter_webp, command=self._scan_input_folder)
        self.chk_webp.pack(side="left", padx=4)

        # ====== BOX 3: PROCESS & OCR ENGINE (BELOW BOX 1) ======
        self.box3 = ctk.CTkFrame(left_panel, corner_radius=8)
        self.box3.grid(row=1, column=0, sticky="nsew", padx=0, pady=(5, 0))

        self.lbl_box3 = ctk.CTkLabel(self.box3, text=self._tr("box_3_process"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_box3.pack(anchor="w", padx=12, pady=(8, 4))

        f_dpi = ctk.CTkFrame(self.box3, fg_color="transparent")
        f_dpi.pack(fill="x", padx=10, pady=2)
        self.lbl_dpi = ctk.CTkLabel(f_dpi, text=self._tr("resolution_dpi"))
        self.lbl_dpi.pack(side="left", padx=(0, 5))
        self.cmb_dpi = ctk.CTkOptionMenu(
            f_dpi,
            values=["300 DPI (Standard)", "150 DPI (Fast)", "600 DPI (Ultra)"],
            width=140
        )
        self.cmb_dpi.set("300 DPI (Standard)")
        self.cmb_dpi.pack(side="right")

        f_opt = ctk.CTkFrame(self.box3, fg_color=("gray95", "#242424"), corner_radius=6)
        f_opt.pack(fill="x", padx=10, pady=4)

        self.var_deskew = ctk.BooleanVar(value=self.config_data.get("enable_auto_deskew", True))
        self.chk_deskew = ctk.CTkCheckBox(f_opt, text=self._tr("auto_deskew"), variable=self.var_deskew)
        self.chk_deskew.pack(anchor="w", padx=8, pady=4)

        self.btn_inspect = ctk.CTkButton(
            self.box3,
            text=self._tr("btn_inspect"),
            fg_color="#2980b9",
            hover_color="#1f618d",
            font=ctk.CTkFont(weight="bold"),
            command=self._inspect_files
        )
        self.btn_inspect.pack(fill="x", padx=10, pady=(4, 4))

        f_btns = ctk.CTkFrame(self.box3, fg_color="transparent")
        f_btns.pack(fill="x", padx=10, pady=(2, 6))

        self.btn_start = ctk.CTkButton(
            f_btns,
            text=self._tr("btn_process"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            font=ctk.CTkFont(weight="bold"),
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
        self.lbl_sub_docs = ctk.CTkLabel(self.box2, text=f"• {self._tr('sub_documents')}", font=ctk.CTkFont(size=12, weight="bold"), text_color="#27ae60")
        self.lbl_sub_docs.pack(anchor="w", padx=12, pady=(2, 2))

        self.lbl_docs_path = ctk.CTkLabel(self.box2, text=self._tr("docs_folder_label"), font=ctk.CTkFont(size=11))
        self.lbl_docs_path.pack(anchor="w", padx=12, pady=(0, 1))

        f_out = ctk.CTkFrame(self.box2, fg_color="transparent")
        f_out.pack(fill="x", padx=10, pady=2)
        self.output_entry = ctk.CTkEntry(f_out, placeholder_text=self._tr("output_placeholder"))
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.output_entry.bind("<Return>", self._on_output_entry_change)
        self.output_entry.bind("<FocusOut>", self._on_output_entry_change)
        self.btn_out_browse = ctk.CTkButton(f_out, text=self._tr("browse"), width=75, command=self._browse_output)
        self.btn_out_browse.pack(side="right")

        doc_frame = ctk.CTkFrame(self.box2, fg_color=("gray95", "#242424"), corner_radius=6)
        doc_frame.pack(fill="x", padx=10, pady=(4, 6))

        self.var_pdf_orig = ctk.BooleanVar(value=self.config_data.get("export_pdf_original", True))
        self.var_pdf_comp = ctk.BooleanVar(value=self.config_data.get("export_pdf_compressed", False))
        self.var_docx = ctk.BooleanVar(value=self.config_data.get("export_docx", True))

        self.chk_pdf_orig = ctk.CTkCheckBox(doc_frame, text=self._tr("export_pdf_original"), variable=self.var_pdf_orig, command=self._on_option_toggle)
        self.chk_pdf_orig.pack(anchor="w", padx=8, pady=2)

        self.chk_pdf_comp = ctk.CTkCheckBox(doc_frame, text=self._tr("export_pdf_compressed"), variable=self.var_pdf_comp, command=self._on_option_toggle)
        self.chk_pdf_comp.pack(anchor="w", padx=8, pady=2)

        self.chk_docx = ctk.CTkCheckBox(doc_frame, text=self._tr("export_docx"), variable=self.var_docx, command=self._on_option_toggle)
        self.chk_docx.pack(anchor="w", padx=8, pady=2)

        self.var_tables = ctk.BooleanVar(value=self.config_data.get("extract_tables", True))
        self.var_illustrations = ctk.BooleanVar(value=self.config_data.get("extract_illustrations", True))

        self.chk_tables = ctk.CTkCheckBox(doc_frame, text=self._tr("extract_tables"), variable=self.var_tables, command=self._on_option_toggle)
        self.chk_tables.pack(anchor="w", padx=24, pady=2)

        self.chk_illustrations = ctk.CTkCheckBox(doc_frame, text=self._tr("extract_illustrations"), variable=self.var_illustrations, command=self._on_option_toggle)
        self.chk_illustrations.pack(anchor="w", padx=24, pady=2)

        self.var_doc_subfolders = ctk.BooleanVar(value=self.config_data.get("organize_doc_subfolders", False))
        self.chk_doc_subfolders = ctk.CTkCheckBox(self.box2, text=self._tr("organize_doc_subfolders"), variable=self.var_doc_subfolders, command=self._on_option_toggle)
        self.chk_doc_subfolders.pack(anchor="w", padx=12, pady=(2, 6))

        # Subsection 2: Data
        self.lbl_sub_data = ctk.CTkLabel(self.box2, text=f"• {self._tr('sub_data')}", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e67e22")
        self.lbl_sub_data.pack(anchor="w", padx=12, pady=(4, 2))

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
        self.chk_txt = ctk.CTkCheckBox(f_data_cb1, text="TXT", variable=self.var_txt, command=self._on_option_toggle)
        self.chk_txt.pack(side="left", padx=4)
        self.chk_hocr = ctk.CTkCheckBox(f_data_cb1, text="hOCR", variable=self.var_hocr, command=self._on_option_toggle)
        self.chk_hocr.pack(side="left", padx=4)
        self.chk_json = ctk.CTkCheckBox(f_data_cb1, text="JSON", variable=self.var_json, command=self._on_option_toggle)
        self.chk_json.pack(side="left", padx=4)

        f_data_cb2 = ctk.CTkFrame(data_fmt_frame, fg_color="transparent")
        f_data_cb2.pack(fill="x", padx=4, pady=(0, 2))
        self.chk_csv = ctk.CTkCheckBox(f_data_cb2, text="CSV", variable=self.var_csv, command=self._on_option_toggle)
        self.chk_csv.pack(side="left", padx=4)
        self.chk_xlsx = ctk.CTkCheckBox(f_data_cb2, text="Excel", variable=self.var_xlsx, command=self._on_option_toggle)
        self.chk_xlsx.pack(side="left", padx=4)
        self.chk_audit = ctk.CTkCheckBox(f_data_cb2, text=self._tr("export_audit_report"), variable=self.var_audit, command=self._on_option_toggle)
        self.chk_audit.pack(side="left", padx=4)

        f_data_cb3 = ctk.CTkFrame(data_fmt_frame, fg_color="transparent")
        f_data_cb3.pack(fill="x", padx=4, pady=(0, 2))
        self.chk_txt_from_pdf = ctk.CTkCheckBox(f_data_cb3, text=self._tr("export_txt_from_pdf"), variable=self.var_txt_from_pdf, command=self._on_option_toggle)
        self.chk_txt_from_pdf.pack(side="left", padx=4)
        self.chk_docx_from_pdf = ctk.CTkCheckBox(f_data_cb3, text=self._tr("export_docx_from_pdf"), variable=self.var_docx_from_pdf, command=self._on_option_toggle)
        self.chk_docx_from_pdf.pack(side="left", padx=4)

        self.lbl_data_path = ctk.CTkLabel(self.box2, text=self._tr("data_folder_label"), font=ctk.CTkFont(size=11))
        self.lbl_data_path.pack(anchor="w", padx=12, pady=(4, 1))

        f_data = ctk.CTkFrame(self.box2, fg_color="transparent")
        f_data.pack(fill="x", padx=10, pady=(0, 4))
        self.data_entry = ctk.CTkEntry(f_data, placeholder_text=self._tr("data_placeholder"))
        self.data_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.data_entry.bind("<Return>", self._on_data_entry_change)
        self.data_entry.bind("<FocusOut>", self._on_data_entry_change)
        self.btn_data_browse = ctk.CTkButton(f_data, text=self._tr("browse"), width=75, command=self._browse_data)
        self.btn_data_browse.pack(side="right")

        self.var_subfolders = ctk.BooleanVar(value=self.config_data.get("organize_subfolders", False))
        self.chk_subfolders = ctk.CTkCheckBox(self.box2, text=self._tr("organize_subfolders"), variable=self.var_subfolders, command=self._on_option_toggle)
        self.chk_subfolders.pack(anchor="w", padx=12, pady=(0, 8))

        # ------------------- BOX 4: FILE LIST TABLE (WITH CHARS COLUMN) -------------------
        tbl_frame = ctk.CTkFrame(self, corner_radius=8)
        tbl_frame.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 15))
        tbl_frame.grid_columnconfigure(0, weight=1)
        tbl_frame.grid_rowconfigure(1, weight=1)

        f_tbl_hdr = ctk.CTkFrame(tbl_frame, fg_color="transparent")
        f_tbl_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        self.lbl_box4 = ctk.CTkLabel(f_tbl_hdr, text=self._tr("box_table"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_box4.pack(side="left")

        self.lbl_selected_badge = ctk.CTkLabel(f_tbl_hdr, text="Selecionados: 0 de 0", font=ctk.CTkFont(size=11, weight="bold"), text_color="#e67e22")
        self.lbl_selected_badge.pack(side="right", padx=10)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background="#2b2b2b",
            foreground="white",
            fieldbackground="#2b2b2b",
            rowheight=26,
            font=("Segoe UI", self.table_font_size)
        )
        style.configure("Treeview.Heading", background="#1f1f1f", foreground="white", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#1f618d")])

        # New Column Order: Filename, Format, Size, Pages, ImgRes, Chars, Status, Time
        cols = ("Filename", "Format", "Size", "Pages", "ImgRes", "Chars", "Status", "Time")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", selectmode="extended")

        self.tree.heading("Filename", text=self._tr("col_filename"), command=lambda: self._sort_table("Filename"))
        self.tree.heading("Format", text=self._tr("col_format"), command=lambda: self._sort_table("Format"))
        self.tree.heading("Size", text=self._tr("col_size"), command=lambda: self._sort_table("Size"))
        self.tree.heading("Pages", text=self._tr("col_pages"), command=lambda: self._sort_table("Pages"))
        self.tree.heading("ImgRes", text=self._tr("col_img_res"), command=lambda: self._sort_table("ImgRes"))
        self.tree.heading("Chars", text=self._tr("col_chars"), command=lambda: self._sort_table("Chars"))
        self.tree.heading("Status", text=self._tr("col_status"), command=lambda: self._sort_table("Status"))
        self.tree.heading("Time", text=self._tr("col_time"), command=lambda: self._sort_table("Time"))

        self.tree.column("Filename", width=300, anchor="w")
        self.tree.column("Format", width=130, anchor="center")
        self.tree.column("Size", width=85, anchor="e")
        self.tree.column("Pages", width=65, anchor="center")
        self.tree.column("ImgRes", width=85, anchor="center")
        self.tree.column("Chars", width=90, anchor="e")
        self.tree.column("Status", width=150, anchor="center")
        self.tree.column("Time", width=70, anchor="e")

        self.tree.bind("<<TreeviewSelect>>", self._on_table_select)

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=5)
        vsb.grid(row=1, column=1, sticky="ns", padx=(0, 10), pady=5)

        self._attach_tooltips()
        self._check_conditional_controls()

    def _attach_tooltips(self):
        # Header controls
        ToolTip(self.chk_help, "tt_help_mode", self)
        ToolTip(self.cmb_lang, "tt_lang", self)

        # Section 1 (Box 1: Input & Formats)
        ToolTip(self.input_entry, "tt_input_folder", self)
        ToolTip(self.btn_in_browse, "tt_browse", self)
        ToolTip(self.chk_pdf, "tt_filter_pdf", self)
        ToolTip(self.chk_tiff, "tt_filter_tiff", self)
        ToolTip(self.chk_jpg, "tt_filter_jpg", self)
        ToolTip(self.chk_png, "tt_filter_png", self)
        ToolTip(self.chk_bmp, "tt_filter_bmp", self)
        ToolTip(self.chk_webp, "tt_filter_webp", self)

        # Section 2 (Box 2: Output Documents & Data)
        ToolTip(self.output_entry, "tt_docs_folder", self)
        ToolTip(self.btn_out_browse, "tt_browse", self)
        ToolTip(self.chk_pdf_orig, "tt_export_pdf_original", self)
        ToolTip(self.chk_pdf_comp, "tt_export_pdf_compressed", self)
        ToolTip(self.chk_docx, "tt_export_docx", self)
        ToolTip(self.chk_tables, "tt_extract_tables", self)
        ToolTip(self.chk_illustrations, "tt_extract_illustrations", self)
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

        # Section 3 (Box 3: Process & OCR Engine)
        ToolTip(self.cmb_dpi, "tt_resolution_dpi", self)
        ToolTip(self.chk_deskew, "tt_auto_deskew", self)
        ToolTip(self.btn_inspect, "tt_btn_inspect", self)
        ToolTip(self.btn_start, "tt_btn_process", self)
        ToolTip(self.btn_stop, "tt_btn_abort", self)

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

    def _retranslate_ui(self):
        """Dynamically updates all widget text labels when language is changed."""
        self.lbl_title.configure(text=f"S2POT — Smart Slim PDF OCR Tool v{APP_VERSION}")
        self.chk_help.configure(text=self._tr("help_mode"))
        self.lbl_lang.configure(text=self._tr("language"))

        self.lbl_box1.configure(text=self._tr("box_1_input"))
        self.input_entry.configure(placeholder_text=self._tr("input_placeholder"))
        self.btn_in_browse.configure(text=self._tr("browse"))

        self.lbl_box2.configure(text=self._tr("box_2_output"))
        self.lbl_sub_docs.configure(text=f"• {self._tr('sub_documents')}")
        self.lbl_docs_path.configure(text=self._tr("docs_folder_label"))
        self.output_entry.configure(placeholder_text=self._tr("output_placeholder"))
        self.btn_out_browse.configure(text=self._tr("browse"))

        self.chk_pdf_orig.configure(text=self._tr("export_pdf_original"))
        self.chk_pdf_comp.configure(text=self._tr("export_pdf_compressed"))
        self.chk_docx.configure(text=self._tr("export_docx"))
        self.chk_tables.configure(text=self._tr("extract_tables"))
        self.chk_illustrations.configure(text=self._tr("extract_illustrations"))
        self.chk_doc_subfolders.configure(text=self._tr("organize_doc_subfolders"))

        self.lbl_sub_data.configure(text=f"• {self._tr('sub_data')}")
        self.chk_audit.configure(text=self._tr("export_audit_report"))
        self.chk_txt_from_pdf.configure(text=self._tr("export_txt_from_pdf"))
        self.chk_docx_from_pdf.configure(text=self._tr("export_docx_from_pdf"))
        self.lbl_data_path.configure(text=self._tr("data_folder_label"))
        self.data_entry.configure(placeholder_text=self._tr("data_placeholder"))
        self.btn_data_browse.configure(text=self._tr("browse"))
        self.chk_subfolders.configure(text=self._tr("organize_subfolders"))

        self.lbl_box3.configure(text=self._tr("box_3_process"))
        self.lbl_dpi.configure(text=self._tr("resolution_dpi"))
        self.chk_deskew.configure(text=self._tr("auto_deskew"))
        self.btn_inspect.configure(text=self._tr("btn_inspect"))
        self.btn_start.configure(text=self._tr("btn_process"))
        self.btn_stop.configure(text=self._tr("btn_abort"))

        self.lbl_box4.configure(text=self._tr("box_table"))
        self.tree.heading("Filename", text=self._tr("col_filename"))
        self.tree.heading("Format", text=self._tr("col_format"))
        self.tree.heading("Size", text=self._tr("col_size"))
        self.tree.heading("Pages", text=self._tr("col_pages"))
        self.tree.heading("ImgRes", text=self._tr("col_img_res"))
        self.tree.heading("Chars", text=self._tr("col_chars"))
        self.tree.heading("Status", text=self._tr("col_status"))
        self.tree.heading("Time", text=self._tr("col_time"))

        self.lbl_badge.configure(text=self._tr("found_files_badge").format(count=len(self.found_files)))
        self._on_table_select(None)

    def _get_current_export_options(self):
        is_doc_sub_active = (self.chk_doc_subfolders.cget("state") != "disabled") and self.var_doc_subfolders.get()
        is_sub_active = (self.chk_subfolders.cget("state") != "disabled") and self.var_subfolders.get()
        return {
            "export_pdf_original": self.var_pdf_orig.get(),
            "export_pdf_compressed": self.var_pdf_comp.get(),
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
            "extract_tables": self.var_tables.get() if self.var_docx.get() else False,
            "extract_illustrations": self.var_illustrations.get() if self.var_docx.get() else False
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
        self.config_data["export_pdf_original"] = self.var_pdf_orig.get()
        self.config_data["export_pdf_compressed"] = self.var_pdf_comp.get()
        self.config_data["export_docx"] = self.var_docx.get()
        self.config_data["extract_tables"] = self.var_tables.get()
        self.config_data["extract_illustrations"] = self.var_illustrations.get()
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

        # Calculate exact per-format counts in input folder
        c_pdf = len(glob.glob(os.path.join(in_path, "*.pdf"))) + len(glob.glob(os.path.join(in_path, "*.PDF")))
        c_tiff = len(glob.glob(os.path.join(in_path, "*.tif"))) + len(glob.glob(os.path.join(in_path, "*.tiff"))) + len(glob.glob(os.path.join(in_path, "*.TIF"))) + len(glob.glob(os.path.join(in_path, "*.TIFF")))
        c_jpg = len(glob.glob(os.path.join(in_path, "*.jpg"))) + len(glob.glob(os.path.join(in_path, "*.jpeg"))) + len(glob.glob(os.path.join(in_path, "*.JPG"))) + len(glob.glob(os.path.join(in_path, "*.JPEG")))
        c_png = len(glob.glob(os.path.join(in_path, "*.png"))) + len(glob.glob(os.path.join(in_path, "*.PNG")))
        c_bmp = len(glob.glob(os.path.join(in_path, "*.bmp"))) + len(glob.glob(os.path.join(in_path, "*.BMP")))
        c_webp = len(glob.glob(os.path.join(in_path, "*.webp"))) + len(glob.glob(os.path.join(in_path, "*.WEBP")))

        self.chk_pdf.configure(text=f"{self._tr('filter_pdf')} ({c_pdf})")
        self.chk_tiff.configure(text=f"{self._tr('filter_tiff')} ({c_tiff})")
        self.chk_jpg.configure(text=f"{self._tr('filter_jpg')} ({c_jpg})")
        self.chk_png.configure(text=f"{self._tr('filter_png')} ({c_png})")
        self.chk_bmp.configure(text=f"{self._tr('filter_bmp')} ({c_bmp})")
        self.chk_webp.configure(text=f"{self._tr('filter_webp')} ({c_webp})")

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

        self.found_files = sorted(list(set(files)))

        # Check for s2pot_tuning.json badge
        tuning_file = os.path.join(in_path, "s2pot_tuning.json")
        badge_text = self._tr("found_files_badge").format(count=len(self.found_files))
        if os.path.exists(tuning_file):
            badge_text += f"  |  {self._tr('s2pit_badge_active')}"
            self.lbl_badge.configure(text=badge_text, text_color="#2ecc71")
        else:
            self.lbl_badge.configure(text=badge_text, text_color="#3498db")

    def _open_s2pit_preview(self):
        in_path = self.input_entry.get().strip()
        s2pit_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "s2pit_main.py")
        if os.path.exists(s2pit_script):
            subprocess.Popen([sys.executable, s2pit_script, in_path])

        self.tree.delete(*self.tree.get_children())
        self.file_items = {}
        self.item_to_filepath = {}

        for f in self.found_files:
            fname = os.path.basename(f)
            ext = os.path.splitext(f)[1].upper().replace(".", "")
            size_mb = os.path.getsize(f) / (1024 * 1024)
            fmt_str = ext
            if ext == "PDF":
                try:
                    doc = fitz.open(f)
                    pdf_chars = 0
                    for page in doc:
                        t = page.get_text("text").strip()
                        if t:
                            pdf_chars += len(t)
                    doc.close()
                    if pdf_chars > 0:
                        fmt_str = f"PDF / {pdf_chars:,} chars"
                except Exception:
                    fmt_str = "PDF"

            # New Order: Filename, Format, Size, Pages, ImgRes, Chars, Status, Time
            item_id = self.tree.insert("", "end", values=(fname, fmt_str, f"{size_mb:.2f} MB", "-", "-", "-", "READY", "-"))
            self.file_items[f] = item_id
            self.item_to_filepath[item_id] = f

        self._on_table_select(None)
        self._check_conditional_controls()

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
        """Inspects selected/scanned input files to count pages, image DPI, and updates 'Pages', 'ImgRes', and 'Format' columns."""
        selected_ids = self.tree.selection()
        if selected_ids:
            target_files = [self.item_to_filepath[iid] for iid in selected_ids if iid in self.item_to_filepath]
        else:
            target_files = list(self.found_files)

        if not target_files:
            messagebox.showwarning("Warning", "No files available to inspect.")
            return

        inspected_count = 0
        for fpath in target_files:
            if not os.path.exists(fpath):
                continue

            ext = os.path.splitext(fpath)[1].lower()
            n_pages = 1

            try:
                if ext == ".pdf":
                    doc = fitz.open(fpath)
                    n_pages = len(doc)
                    pdf_chars = 0
                    for page in doc:
                        t = page.get_text("text").strip()
                        if t:
                            pdf_chars += len(t)
                    doc.close()
                    if pdf_chars > 0:
                        fmt_str = f"PDF / {pdf_chars:,} chars"
                    else:
                        fmt_str = "PDF"
                    if fpath in self.file_items:
                        item_id = self.file_items[fpath]
                        self.tree.set(item_id, "Format", fmt_str)
                elif ext in [".tif", ".tiff"]:
                    tiff_img = Image.open(fpath)
                    n_pages = getattr(tiff_img, "n_frames", 1)
            except Exception as e:
                print(f"Inspection error for {fpath}: {e}")

            dpi_val = self._get_file_max_dpi(fpath)
            img_res_str = f"{dpi_val} DPI"

            if fpath in self.file_items:
                item_id = self.file_items[fpath]
                self.tree.set(item_id, "Pages", str(n_pages))
                self.tree.set(item_id, "ImgRes", img_res_str)
                inspected_count += 1

        messagebox.showinfo("Inspection Complete", f"Successfully inspected {inspected_count} file(s)!")

    def _check_conditional_controls(self):
        """Enforces conditional UI rules for Word options, Document Subfolders, and Data Destination folder."""
        if self.var_docx.get():
            self.chk_tables.configure(state="normal")
            self.chk_illustrations.configure(state="normal")
        else:
            self.chk_tables.configure(state="disabled")
            self.chk_illustrations.configure(state="disabled")

        active_doc_targets = sum([
            self.var_pdf_orig.get(),
            self.var_pdf_comp.get(),
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
        """Updates selection badge when user selects rows in the table."""
        selected_items = self.tree.selection()
        n_selected = len(selected_items)
        n_total = len(self.found_files)
        self.lbl_selected_badge.configure(text=self._tr("selected_files_badge").format(selected=n_selected, total=n_total))

    def _sort_table(self, col):
        """Sorts the table columns in ascending or descending order."""
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False

        items = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]

        def parse_sort_key(val_tuple):
            val, item_id = val_tuple
            if col == "Size":
                try:
                    return float(val.replace("MB", "").strip())
                except ValueError:
                    return 0.0
            elif col == "Time":
                try:
                    return float(val.replace("s", "").strip())
                except ValueError:
                    return 0.0
            elif col in ["Pages", "Chars", "ImgRes"]:
                try:
                    return int(val.replace("DPI", "").replace(",", "").strip())
                except ValueError:
                    return 0
            return val.lower()

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
            target_files = [self.item_to_filepath[iid] for iid in selected_ids if iid in self.item_to_filepath]
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

        export_opts = {
            "export_pdf_original": self.var_pdf_orig.get(),
            "export_pdf_compressed": self.var_pdf_comp.get(),
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
            "extract_tables": self.var_tables.get() if self.var_docx.get() else False,
            "extract_illustrations": self.var_illustrations.get() if self.var_docx.get() else False
        }

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
                    progress_callback=self._update_progress,
                    file_complete_callback=self._on_file_complete,
                    abort_check_fn=lambda: self.abort_requested
                )
                self.after(0, lambda: messagebox.showinfo("Completed", f"Batch processing finished for {len(target_files)} file(s)!"))
            except InterruptedError:
                self.after(0, self._handle_interrupted_status, target_files)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Error", f"Error during processing: {e}"))
            finally:
                self.after(0, self._reset_process_ui)

        threading.Thread(target=run_thread, daemon=True).start()

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
        interrupted_text = self._tr("status_interrupted")
        for fpath in target_files:
            if fpath in self.file_items:
                item_id = self.file_items[fpath]
                curr_status = str(self.tree.set(item_id, "Status"))
                if curr_status.startswith("PROCESSING") or curr_status == "READY":
                    self.tree.set(item_id, "Status", interrupted_text)
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
        pct = (curr_file - 1 + (curr_page / float(max(1, tot_pages)))) / float(tot_files)
        self.progress.set(pct)

        filepath = os.path.join(self.input_entry.get().strip(), filename)
        self.current_processing_file = filepath
        if filepath in self.file_items:
            item_id = self.file_items[filepath]
            self.tree.set(item_id, "Status", f"PROCESSING ({curr_page}/{tot_pages})")

    def _on_file_complete(self, filepath, record):
        if filepath in self.file_items:
            item_id = self.file_items[filepath]
            self.tree.set(item_id, "Status", record.get("status", "DONE"))
            self.tree.set(item_id, "Pages", str(record.get("pages", 1)))
            self.tree.set(item_id, "Chars", f"{record.get('total_chars', 0):,}")
            self.tree.set(item_id, "Time", f"{record.get('duration_sec', 0):.1f}s")


def launch_gui():
    app = S2POTApp()
    app.mainloop()


if __name__ == "__main__":
    launch_gui()
