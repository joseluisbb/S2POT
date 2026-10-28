import os
import sys
import glob
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image, ImageTk, ImageDraw

from core.config import load_config, save_config, APP_VERSION
from core.i18n import get_text, LANGUAGES
from core.tuning_config import (
    load_tuning_config,
    save_tuning_config,
    get_effective_file_tuning,
    DEFAULT_GLOBAL_TUNING,
    TUNING_FILENAME
)
from core.pipeline import apply_color_mode
from core.enhancer import enhance_contrast_lab
from core.pdf_builder import encode_page_jpeg_bytes
from core.metadata_config import load_folder_metadata, estimate_text_layer_confidence
from core.ocr_text_editor import load_page_ocr_text, save_edited_page_ocr_text, get_document_pages_ocr_summary
from gui.tooltip import ToolTip
from gui.file_browser import FilePageTreeBrowser, get_file_max_dpi, get_file_pages_metadata

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class S2PITPageTuningWindow(ctk.CTkToplevel):
    """
    Dedicated Page Tuning Studio & Lens Window for S2PIT.
    Provides side-by-side original vs tuned comparison, zoom lens inspection,
    and 3-line tuning controls with persistence to s2pot_tuning.json.
    """
    def __init__(self, master, file_path, page_idx=0, lang="pt_PT", on_save_callback=None):
        super().__init__(master)

        resolved_path = self._resolve_original_source_path(master, file_path)
        self.file_path = resolved_path
        self.filename = os.path.basename(resolved_path)
        self.folder_path = os.path.dirname(resolved_path)
        self.page_idx = page_idx
        self.total_pages = 1
        self.lang = lang
        self.current_language = lang
        self.on_save_callback = on_save_callback

        # Config & Help mode
        cfg = load_config() or {}
        self.config_data = cfg
        self.var_help_mode = ctk.BooleanVar(value=cfg.get("help_mode", True))

        self.title(f"{self._tr('page_viewer_title')} — {self.filename} ({self._tr('tree_item_page').format(idx=self.page_idx + 1)})")
        self.geometry("1260x820")
        self.minsize(1020, 680)

        # Splitters & subwindow layout proportions
        self.thumb_width = 250
        self.orig_ratio = 0.5
        self._sash_drag_data = None
        self._thumb_resize_timer = None

        # Thumbnails state & view mode (list vs grid)
        self.thumb_view_mode = "list"
        self.thumb_grid_cols = 4
        self.raw_pil_thumbs = {}
        self.ocr_summary_cache = []
        self.thumb_cards = {}
        self.thumb_images = {}
        self.thumb_ocr_labels = {}
        self.thumb_pil_images = {}
        self.thumb_img_labels = {}
        self._current_thumb_path = None
        self._current_annotated_thumb = None

        # OCR text editor + pinned lenses state
        self.ocr_editor = None
        self.pinned_lenses = {"orig": None, "tuned": None}
        self.lens_focus_regions = {"orig": None, "tuned": None}

        # Load folder tuning configuration
        loaded = load_tuning_config(self.folder_path) or {}
        self.global_tuning = loaded.get("global_tuning", dict(DEFAULT_GLOBAL_TUNING))
        self.file_tunings = loaded.get("file_tunings", {})
        self.page_tunings = loaded.get("page_tunings", {})

        # Tuning variables
        self.var_color_mode = ctk.StringVar(value="color")
        self.var_dpi = ctk.StringVar(value="150 DPI")
        self.var_jpeg_quality = ctk.IntVar(value=60)
        self.var_scope_choice = ctk.StringVar(value="batch") # "batch", "file", "page"

        # Image cache & display state
        self.current_page_orig_dpi = 300
        self.current_orig_bgr = None
        self.current_tuned_bgr = None
        self.orig_display_info = None  # (nw, nh, offset_x, offset_y)
        self.tuned_display_info = None # (nw, nh, offset_x, offset_y)
        self._resize_timer = None

        # Lens popover
        self.lens_popover = None
        self.lbl_lens_img = None
        self.var_lens_zoom = ctk.StringVar(value="Off")

        self._build_studio_ui()
        self._load_and_display_page()

    def _resolve_original_source_path(self, master, file_path):
        """Ensures that if a generated result file (e.g. *_compressed.pdf) is passed, the true original input file is loaded."""
        if not file_path or not os.path.exists(file_path):
            return file_path
        bname = os.path.basename(file_path)
        stem, ext = os.path.splitext(bname)
        stem_low = stem.lower()
        is_result_suffix = stem_low.endswith("_compressed") or stem_low.endswith("_original")
        parent_dir_name = os.path.basename(os.path.dirname(file_path)).lower()
        is_result_subfolder = parent_dir_name in ("pdf-compressed", "pdf-originalimages", "docx")

        if not is_result_suffix and not is_result_subfolder:
            return file_path

        base_stem = stem
        for sfx in ("_compressed", "_original"):
            if base_stem.lower().endswith(sfx) and len(base_stem) > len(sfx):
                base_stem = base_stem[:-len(sfx)]
                break

        candidate_dirs = []
        for obj in (master, getattr(master, "parent_app", None)):
            if obj is not None:
                if hasattr(obj, "input_entry"):
                    try:
                        d = obj.input_entry.get().strip()
                        if d and os.path.exists(d):
                            candidate_dirs.append(d)
                    except Exception:
                        pass
                in_d = getattr(obj, "input_dir", None)
                if in_d and os.path.exists(in_d):
                    candidate_dirs.append(in_d)

        f_dir = os.path.dirname(file_path)
        candidate_dirs.append(f_dir)
        if is_result_subfolder:
            candidate_dirs.append(os.path.dirname(f_dir))

        visual_exts = (".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp", ".webp")
        for cdir in candidate_dirs:
            if not cdir or not os.path.exists(cdir):
                continue
            for vext in (ext,) + visual_exts:
                cand = os.path.join(cdir, base_stem + vext)
                if os.path.exists(cand) and os.path.normcase(os.path.abspath(cand)) != os.path.normcase(os.path.abspath(file_path)):
                    return cand
            try:
                for entry in os.scandir(cdir):
                    if entry.is_file():
                        e_stem, e_ext = os.path.splitext(entry.name)
                        if e_ext.lower() in visual_exts and e_stem.lower() == base_stem.lower():
                            if os.path.normcase(os.path.abspath(entry.path)) != os.path.normcase(os.path.abspath(file_path)):
                                return entry.path
            except Exception:
                pass

        return file_path

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_studio_ui(self):
        # 1. Header Bar
        hdr = ctk.CTkFrame(self, height=48, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        hdr.pack(side="top", fill="x")

        # Document / Page Navigator
        nav_box = ctk.CTkFrame(hdr, fg_color="transparent")
        nav_box.pack(side="left", padx=12, pady=8)

        self.btn_prev_page = ctk.CTkButton(
            nav_box,
            text="◀",
            width=34,
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._prev_page
        )
        self.btn_prev_page.pack(side="left", padx=(0, 4))

        self.lbl_doc_info = ctk.CTkLabel(
            nav_box,
            text=f"📄 {self.filename}  |  Pág {self.page_idx + 1}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_doc_info.pack(side="left", padx=8)

        self.btn_next_page = ctk.CTkButton(
            nav_box,
            text="▶",
            width=34,
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._next_page
        )
        self.btn_next_page.pack(side="left", padx=(4, 0))

        # Language & Help Controls in Header
        lang_help_box = ctk.CTkFrame(hdr, fg_color="transparent")
        lang_help_box.pack(side="right", padx=(10, 14), pady=8)

        self.chk_help = ctk.CTkCheckBox(
            lang_help_box,
            text=self._tr("help_mode"),
            variable=self.var_help_mode,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_help_toggle
        )
        self.chk_help.pack(side="left", padx=(0, 10))

        self.lbl_lang = ctk.CTkLabel(lang_help_box, text=self._tr("language"), font=ctk.CTkFont(size=12))
        self.lbl_lang.pack(side="left", padx=(0, 4))

        self.cmb_lang = ctk.CTkOptionMenu(
            lang_help_box,
            values=list(LANGUAGES.values()),
            width=135,
            font=ctk.CTkFont(size=12),
            command=self._on_language_change
        )
        curr_lang_name = LANGUAGES.get(self.lang, "Português (PT)")
        self.cmb_lang.set(curr_lang_name)
        self.cmb_lang.pack(side="left")

        # Lens Zoom Controls in Header
        lens_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        lens_frame.pack(side="right", padx=10, pady=8)

        ctk.CTkLabel(
            lens_frame,
            text=f"🔍 {self._tr('lens_mode')}:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#d35400", "#f1c40f")
        ).pack(side="left", padx=(0, 6))

        self.cmb_lens = ctk.CTkOptionMenu(
            lens_frame,
            values=["Off", "125%", "150%", "175%", "200%", "300%", "400%"],
            width=100,
            font=ctk.CTkFont(size=12),
            variable=self.var_lens_zoom,
            command=self._on_lens_zoom_change
        )
        self.cmb_lens.pack(side="left")
        ToolTip(self.cmb_lens, "lens_pin_hint", self)

        # OCR Text View / Edit button (for the currently selected page)
        ocr_btn_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        ocr_btn_frame.pack(side="right", padx=(10, 0), pady=8)
        self.btn_edit_ocr = ctk.CTkButton(
            ocr_btn_frame,
            text=self._tr("btn_edit_ocr_text"),
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#6c3483",
            hover_color="#512e5f",
            command=self._open_ocr_text_editor
        )
        self.btn_edit_ocr.pack(side="left")
        ToolTip(self.btn_edit_ocr, "tt_btn_edit_ocr_text", self)

        # 2. Main Central Split View Container with interactive movable sashes
        self.view_container = ctk.CTkFrame(self, fg_color="transparent")
        self.view_container.pack(fill="both", expand=True, padx=10, pady=6)
        self.view_container.bind("<Configure>", self._on_container_resize)

        # === SUBWINDOW 0: THUMBNAILS SIDEBAR ===
        self.v_thumb_box = ctk.CTkFrame(self.view_container, corner_radius=6)

        thumb_top = ctk.CTkFrame(self.v_thumb_box, fg_color="transparent")
        thumb_top.pack(side="top", fill="x", padx=6, pady=(6, 4))

        self.lbl_thumb_title = ctk.CTkLabel(
            thumb_top,
            text=f"📑 {self._tr('sidebar_pages')}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_thumb_title.pack(side="left")

        # Toggle view mode button: Lista vs Matriz
        self.btn_thumb_mode = ctk.CTkButton(
            thumb_top,
            text=f"⊞ {self._tr('thumb_mode_grid')}",
            width=76,
            height=26,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=("#2980b9", "#1f618d"),
            command=self._toggle_thumb_view_mode
        )
        self.btn_thumb_mode.pack(side="right", padx=(4, 0))
        ToolTip(self.btn_thumb_mode, "tt_thumb_mode", self)

        # Grid Columns Input (visible in Grid mode)
        self.box_grid_cols = ctk.CTkFrame(thumb_top, fg_color="transparent")
        self.lbl_grid_cols = ctk.CTkLabel(self.box_grid_cols, text=self._tr("thumb_grid_cols"), font=ctk.CTkFont(size=11))
        self.lbl_grid_cols.pack(side="left", padx=(0, 2))
        self.entry_grid_cols = ctk.CTkEntry(self.box_grid_cols, width=32, height=24, font=ctk.CTkFont(size=11), justify="center")
        self.entry_grid_cols.insert(0, str(self.thumb_grid_cols))
        self.entry_grid_cols.bind("<Return>", self._on_grid_cols_changed)
        self.entry_grid_cols.bind("<FocusOut>", self._on_grid_cols_changed)
        self.entry_grid_cols.pack(side="left")
        ToolTip(self.entry_grid_cols, "tt_thumb_cols", self)

        self.thumb_scroll = ctk.CTkScrollableFrame(self.v_thumb_box, fg_color="transparent")
        self.thumb_scroll.pack(side="top", fill="both", expand=True, padx=4, pady=(0, 4))

        # === SASH 1: SPLITTER BETWEEN THUMBNAILS AND ORIGINAL IMAGE ===
        self.sash_thumb = ctk.CTkFrame(
            self.view_container,
            width=6,
            fg_color=("gray75", "#333333"),
            corner_radius=2,
            cursor="sb_h_double_arrow"
        )
        self._bind_sash(self.sash_thumb, "thumb")

        # === SUBWINDOW 1: ORIGINAL PAGE BOX ===
        self.v_orig_box = ctk.CTkFrame(self.view_container, corner_radius=6)
        self.v_orig_box.grid_rowconfigure(1, weight=1)
        self.v_orig_box.grid_columnconfigure(0, weight=1)

        self.lbl_orig_title = ctk.CTkLabel(
            self.v_orig_box,
            text="📷 ORIGINAL PAGE",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#7f8c8d"
        )
        self.lbl_orig_title.grid(row=0, column=0, pady=(6, 2), sticky="w", padx=8)

        # Encostada ao canto superior esquerdo da subjanela
        self.lbl_orig_img = ctk.CTkLabel(self.v_orig_box, text="Loading image...", font=ctk.CTkFont(size=12), anchor="nw")
        self.lbl_orig_img.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        # Original Metadata Card (3 Lines)
        orig_card = ctk.CTkFrame(self.v_orig_box, fg_color=("gray90", "#242424"), corner_radius=6)
        orig_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))

        self.lbl_orig_meta_cmode = ctk.CTkLabel(orig_card, text="Color Mode: -", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_orig_meta_cmode.pack(anchor="w", padx=8, pady=(4, 1))

        self.lbl_orig_meta_dpi = ctk.CTkLabel(orig_card, text="Original DPI: -", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_orig_meta_dpi.pack(anchor="w", padx=8, pady=1)

        self.lbl_orig_meta_dims = ctk.CTkLabel(orig_card, text="Dimensions: -", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_orig_meta_dims.pack(anchor="w", padx=8, pady=(1, 4))

        # === SASH 2: SPLITTER BETWEEN ORIGINAL AND TUNED PREVIEW ===
        self.sash_previews = ctk.CTkFrame(
            self.view_container,
            width=6,
            fg_color=("gray75", "#333333"),
            corner_radius=2,
            cursor="sb_h_double_arrow"
        )
        self._bind_sash(self.sash_previews, "previews")

        # === SUBWINDOW 2: TUNED RESULT PAGE BOX ===
        self.v_prev_box = ctk.CTkFrame(self.view_container, corner_radius=6)
        self.v_prev_box.grid_rowconfigure(1, weight=1)
        self.v_prev_box.grid_columnconfigure(0, weight=1)

        self.lbl_prev_title = ctk.CTkLabel(
            self.v_prev_box,
            text="⚡ TUNED PREVIEW RESULT",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#145a32", "#27ae60")
        )
        self.lbl_prev_title.grid(row=0, column=0, pady=(6, 2), sticky="w", padx=8)

        # Encostada ao canto superior esquerdo da subjanela
        self.lbl_prev_img = ctk.CTkLabel(self.v_prev_box, text="Calculating preview...", font=ctk.CTkFont(size=12), anchor="nw")
        self.lbl_prev_img.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        # Attach Lens Motion, Leave and Pin (left double-click) handlers to both image labels and subwindows
        for subwin, lbl_widget, ttype in [(self.v_orig_box, self.lbl_orig_img, "orig"), (self.v_prev_box, self.lbl_prev_img, "tuned")]:
            subwin.bind("<Double-Button-1>", lambda e, t=ttype: self._pin_lens_at(e, t))
            lbl_widget.bind("<Motion>", lambda e, t=ttype: self._on_lens_motion(e, t))
            lbl_widget.bind("<Leave>", lambda e: self._hide_lens())
            lbl_widget.bind("<Double-Button-1>", lambda e, t=ttype: self._pin_lens_at(e, t))
            if hasattr(lbl_widget, "_label"):
                lbl_widget._label.bind("<Motion>", lambda e, t=ttype: self._on_lens_motion(e, t))
                lbl_widget._label.bind("<Leave>", lambda e: self._hide_lens())
                lbl_widget._label.bind("<Double-Button-1>", lambda e, t=ttype: self._pin_lens_at(e, t))

        # 3-Line Vertical Tuning Controls Card
        tuning_card = ctk.CTkFrame(self.v_prev_box, fg_color=("gray90", "#242424"), corner_radius=6)
        tuning_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))

        # Line 1: Color Mode Dropdown
        line1 = ctk.CTkFrame(tuning_card, fg_color="transparent")
        line1.pack(fill="x", padx=6, pady=(4, 1))

        ctk.CTkLabel(line1, text=f"{self._tr('color_mode_label')}", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 6))

        self.color_opts_map = {
            self._tr("color_mode_color"): "color",
            self._tr("color_mode_grayscale"): "grayscale",
            self._tr("color_mode_bw"): "monochrome"
        }
        self.cmb_color = ctk.CTkOptionMenu(
            line1,
            values=list(self.color_opts_map.keys()),
            width=180,
            font=ctk.CTkFont(size=12),
            command=self._on_color_dropdown_change
        )
        self.cmb_color.pack(side="left", fill="x", expand=True)

        # Line 2: Target DPI Dropdown
        line2 = ctk.CTkFrame(tuning_card, fg_color="transparent")
        line2.pack(fill="x", padx=6, pady=1)

        ctk.CTkLabel(line2, text="Target DPI:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 6))
        self.cmb_dpi = ctk.CTkOptionMenu(
            line2,
            values=["300 DPI", "200 DPI", "150 DPI", "120 DPI", "96 DPI", "72 DPI"],
            variable=self.var_dpi,
            width=140,
            font=ctk.CTkFont(size=12),
            command=lambda v: self._update_preview()
        )
        self.cmb_dpi.pack(side="left", fill="x", expand=True)

        # Line 3: JPEG Quality Slider
        line3 = ctk.CTkFrame(tuning_card, fg_color="transparent")
        line3.pack(fill="x", padx=6, pady=(1, 4))

        ctk.CTkLabel(line3, text="JPEG Quality:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 4))
        self.lbl_q_val = ctk.CTkLabel(line3, text=f"{self.var_jpeg_quality.get()}%", font=ctk.CTkFont(size=12, weight="bold"), text_color="#3498db")
        self.lbl_q_val.pack(side="left", padx=(0, 6))

        self.sld_q = ctk.CTkSlider(line3, from_=30, to=95, number_of_steps=65, variable=self.var_jpeg_quality, command=self._on_quality_slider_change)
        self.sld_q.pack(side="left", fill="x", expand=True, padx=4)

        # 3. Bottom Action & Persistence Bar
        bot_bar = ctk.CTkFrame(self, fg_color=("gray90", "#1a1a1a"), height=48, corner_radius=0)
        bot_bar.pack(side="bottom", fill="x")

        # Real-time Stats Badge
        self.lbl_stats = ctk.CTkLabel(
            bot_bar,
            text="Est. Size: - KB (Redução: -%)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#145a32", "#27ae60")
        )
        self.lbl_stats.pack(side="left", padx=15, pady=8)

        # Scope Selection Dropdown
        self.scope_opts_map = {
            self._tr("scope_all_batch"): "batch",
            self._tr("scope_this_file"): "file",
            self._tr("scope_this_page"): "page"
        }
        self.cmb_scope = ctk.CTkOptionMenu(
            bot_bar,
            values=list(self.scope_opts_map.keys()),
            width=210,
            font=ctk.CTkFont(size=12),
            command=self._on_scope_dropdown_change
        )
        self.cmb_scope.set(self._tr("scope_all_batch"))
        self.cmb_scope.pack(side="right", padx=10, pady=8)

        # Save Button
        self.btn_save = ctk.CTkButton(
            bot_bar,
            text=self._tr("btn_save_options_file"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save_options_to_file
        )
        self.btn_save.pack(side="right", padx=6, pady=8)

        # Sync controls with active config
        self._sync_tuning_ui_from_state()

    def _get_page_orig_dpi(self):
        """Returns the original DPI of the currently viewed page (using cached metadata or fast single-page inspection)."""
        try:
            meta = load_folder_metadata(self.folder_path)
            if meta and "files" in meta and self.filename in meta["files"]:
                f_meta = meta["files"][self.filename]
                pages = f_meta.get("pages", [])
                for p in pages:
                    if p.get("page_idx") == self.page_idx:
                        dpi_val = p.get("dpi") or p.get("orig_dpi")
                        if dpi_val and dpi_val > 0:
                            return int(round(float(dpi_val)))
                if f_meta.get("max_dpi"):
                    return int(round(float(f_meta["max_dpi"])))
        except Exception:
            pass

        ext = os.path.splitext(self.file_path)[1].lower()
        max_dpi = 0.0
        try:
            if ext == ".pdf":
                with fitz.open(self.file_path) as doc:
                    if 0 <= self.page_idx < len(doc):
                        page = doc[self.page_idx]
                        pt_w, pt_h = page.rect.width, page.rect.height
                        image_list = page.get_images()
                        if image_list:
                            for img in image_list:
                                try:
                                    base_img = doc.extract_image(img[0])
                                    w, h = base_img.get("width", 0), base_img.get("height", 0)
                                    if pt_w > 0 and pt_h > 0 and w > 0 and h > 0:
                                        max_dpi = max(max_dpi, (w * 72.0) / pt_w, (h * 72.0) / pt_h)
                                except Exception:
                                    pass
                        if max_dpi <= 0:
                            max_dpi = 300.0
            elif ext in [".tif", ".tiff"]:
                with Image.open(self.file_path) as tiff:
                    if 0 <= self.page_idx < getattr(tiff, "n_frames", 1):
                        tiff.seek(self.page_idx)
                    dpi = tiff.info.get("dpi")
                    if dpi and isinstance(dpi, tuple) and len(dpi) >= 2:
                        max_dpi = max(float(dpi[0]), float(dpi[1]))
                    elif dpi and isinstance(dpi, (int, float)):
                        max_dpi = float(dpi)
            else:
                with Image.open(self.file_path) as img:
                    dpi = img.info.get("dpi")
                    if dpi and isinstance(dpi, tuple) and len(dpi) >= 2:
                        max_dpi = max(float(dpi[0]), float(dpi[1]))
                    elif dpi and isinstance(dpi, (int, float)):
                        max_dpi = float(dpi)
        except Exception:
            pass

        return int(round(max_dpi)) if max_dpi > 0 else 300

    def _format_dpi_option(self, dpi_val):
        d = int(dpi_val)
        if d == int(self.current_page_orig_dpi):
            return f"{d} DPI (Original)"
        return f"{d} DPI"

    def _update_dpi_dropdown_options(self):
        orig_d = int(self.current_page_orig_dpi) if self.current_page_orig_dpi > 0 else 300
        base_dpis = [300, 200, 150, 120, 96, 72]
        all_dpis = sorted(set(base_dpis + [orig_d]), reverse=True)
        opts = [self._format_dpi_option(d) for d in all_dpis]
        self.cmb_dpi.configure(values=opts)

        try:
            curr_d = int(self.var_dpi.get().split()[0])
        except Exception:
            curr_d = 150
        self.var_dpi.set(self._format_dpi_option(curr_d))

    def _sync_tuning_ui_from_state(self):
        eff = get_effective_file_tuning(
            {"global_tuning": self.global_tuning, "file_tunings": self.file_tunings, "page_tunings": self.page_tunings},
            self.filename,
            self.page_idx
        )

        cmode = eff.get("color_mode", "color")
        if cmode == "grayscale":
            self.cmb_color.set(self._tr("color_mode_grayscale"))
        elif cmode == "monochrome":
            self.cmb_color.set(self._tr("color_mode_bw"))
        else:
            self.cmb_color.set(self._tr("color_mode_color"))

        self.var_color_mode.set(cmode)
        self.var_dpi.set(self._format_dpi_option(eff.get("target_dpi", 150)))
        self.var_jpeg_quality.set(eff.get("jpeg_quality", 60))
        self.lbl_q_val.configure(text=f"{eff.get('jpeg_quality', 60)}%")

        page_str = str(self.page_idx)
        if self.filename in self.page_tunings and page_str in self.page_tunings[self.filename]:
            self.cmb_scope.set(self._tr("scope_this_page"))
            self.var_scope_choice.set("page")
        elif self.filename in self.file_tunings:
            self.cmb_scope.set(self._tr("scope_this_file"))
            self.var_scope_choice.set("file")
        else:
            self.cmb_scope.set(self._tr("scope_all_batch"))
            self.var_scope_choice.set("batch")

    def _load_and_display_page(self):
        self.current_page_orig_dpi = self._get_page_orig_dpi()
        self._update_dpi_dropdown_options()
        self._sync_tuning_ui_from_state()

        ext = os.path.splitext(self.file_path)[1].lower()
        bgr = None
        self.total_pages = 1

        try:
            if ext == ".pdf":
                doc = fitz.open(self.file_path)
                self.total_pages = len(doc)
                if self.page_idx < len(doc):
                    page = doc[self.page_idx]
                    render_dpi = min(max(self.current_page_orig_dpi, 150), 300)
                    pix = page.get_pixmap(dpi=render_dpi)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                doc.close()
            elif ext in [".tif", ".tiff"]:
                tiff = Image.open(self.file_path)
                self.total_pages = getattr(tiff, "n_frames", 1)
                if self.page_idx < self.total_pages:
                    tiff.seek(self.page_idx)
                    img = tiff.convert("RGB")
                    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            else:
                bgr = cv2.imread(self.file_path)
                self.total_pages = 1
        except Exception as e:
            print(f"Error loading page {self.page_idx} of {self.file_path}: {e}")

        if bgr is None:
            # Fallback blank
            bgr = np.ones((600, 450, 3), dtype=np.uint8) * 240

        self.current_orig_bgr = bgr
        self.title(f"{self._tr('page_viewer_title')} — {self.filename} ({self._tr('tree_item_page').format(idx=self.page_idx + 1)})")
        self.lbl_doc_info.configure(text=f"📄 {self.filename}  |  Pág {self.page_idx + 1} de {self.total_pages}")
        self.btn_prev_page.configure(state="normal" if self.page_idx > 0 else "disabled")
        self.btn_next_page.configure(state="normal" if self.page_idx < (self.total_pages - 1) else "disabled")

        self._render_thumbnails_if_needed()
        self._highlight_active_thumb()
        self._update_preview()

    def _go_to_page(self, idx):
        if 0 <= idx < self.total_pages and idx != self.page_idx:
            self.page_idx = idx
            self._sync_tuning_ui_from_state()
            self._load_and_display_page()

    def _prev_page(self):
        if self.page_idx > 0:
            self.page_idx -= 1
            self._sync_tuning_ui_from_state()
            self._load_and_display_page()

    def _next_page(self):
        if self.page_idx < self.total_pages - 1:
            self.page_idx += 1
            self._sync_tuning_ui_from_state()
            self._load_and_display_page()

    def _highlight_active_thumb(self):
        for pidx, card in self.thumb_cards.items():
            if pidx == self.page_idx:
                card.configure(border_color="#3498db", border_width=2)
            else:
                card.configure(border_color=("gray75", "#3a3a3a"), border_width=1)
                # Ensure inactive thumbnail doesn't keep a stale lens overlay
                old_lbl = self.thumb_img_labels.get(pidx)
                old_img = self.thumb_images.get(pidx)
                if old_lbl and old_img and old_lbl.winfo_exists():
                    old_lbl.configure(image=old_img)

    def _toggle_thumb_view_mode(self):
        """Toggles thumbnail sidebar view mode between List (with OCR info) and Grid (matrix without OCR info)."""
        self.thumb_view_mode = "grid" if self.thumb_view_mode == "list" else "list"
        self._rebuild_thumbnails_view()

    def _on_grid_cols_changed(self, event=None):
        val = self.entry_grid_cols.get().strip()
        try:
            n = int(val)
            if 1 <= n <= 12:
                if n != self.thumb_grid_cols:
                    self.thumb_grid_cols = n
                    if self.thumb_view_mode == "grid":
                        self._rebuild_thumbnails_view()
                return
        except ValueError:
            pass
        # Reset if invalid
        self.entry_grid_cols.delete(0, "end")
        self.entry_grid_cols.insert(0, str(self.thumb_grid_cols))

    def _render_thumbnails_if_needed(self):
        if self._current_thumb_path == self.file_path and self.raw_pil_thumbs:
            return
        self._current_thumb_path = self.file_path
        self.raw_pil_thumbs.clear()

        # OCR availability / quality per page (metadata -> result PDF -> native text layer)
        try:
            out_dir, data_dir = self._get_results_dirs()
            ocr_summary = get_document_pages_ocr_summary(self.file_path, self.total_pages, out_dir, data_dir)
        except Exception as e:
            print(f"OCR summary error: {e}")
            ocr_summary = [None] * self.total_pages
        self.ocr_summary_cache = ocr_summary

        ext = os.path.splitext(self.file_path)[1].lower()
        doc = None
        tiff = None
        try:
            if ext == ".pdf":
                doc = fitz.open(self.file_path)
            elif ext in [".tif", ".tiff"]:
                tiff = Image.open(self.file_path)
        except Exception:
            pass

        for pidx in range(self.total_pages):
            pil_thumb = None
            try:
                if doc is not None and pidx < len(doc):
                    pix = doc[pidx].get_pixmap(dpi=50)
                    pil_thumb = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                elif tiff is not None:
                    tiff.seek(pidx)
                    pil_thumb = tiff.convert("RGB").copy()
                elif os.path.exists(self.file_path):
                    with Image.open(self.file_path) as im:
                        pil_thumb = im.convert("RGB").copy()
            except Exception:
                pass

            if pil_thumb is None:
                pil_thumb = Image.new("RGB", (120, 160), color=(220, 220, 220))

            self.raw_pil_thumbs[pidx] = pil_thumb

        if doc is not None:
            doc.close()
        if tiff is not None:
            tiff.close()

        self._rebuild_thumbnails_view()

    def _rebuild_thumbnails_view(self):
        """Re-renders the thumbnail sidebar in List mode (with OCR info) or Grid mode (matrix without OCR info)."""
        self._thumb_resize_timer = None
        if not hasattr(self, "thumb_scroll") or not self.thumb_scroll.winfo_exists():
            return

        for w in self.thumb_scroll.winfo_children():
            w.destroy()

        self.thumb_cards.clear()
        self.thumb_images.clear()
        self.thumb_ocr_labels.clear()
        self.thumb_pil_images.clear()
        self.thumb_img_labels.clear()

        # Update button and grid input visibility
        if self.thumb_view_mode == "grid":
            self.btn_thumb_mode.configure(text=f"☰ {self._tr('thumb_mode_list')}")
            self.box_grid_cols.pack(side="right", padx=(2, 6))
        else:
            self.btn_thumb_mode.configure(text=f"⊞ {self._tr('thumb_mode_grid')}")
            self.box_grid_cols.pack_forget()

        avail_w = self.thumb_scroll.winfo_width()
        if avail_w <= 30:
            avail_w = max(100, int(self.thumb_width) - 25)
        usable_w = max(60, avail_w - 24)

        ocr_summary = getattr(self, "ocr_summary_cache", [])

        if self.thumb_view_mode == "grid":
            # === MATRIX / GRID VIEW (No OCR info) ===
            cols = max(1, min(12, int(self.thumb_grid_cols)))
            gap = 6
            cell_w = max(35, int((usable_w - (cols - 1) * gap) / cols))
            target_thumb_w = max(30, cell_w - 6)
            target_thumb_h = int(target_thumb_w * 1.35)

            grid_container = ctk.CTkFrame(self.thumb_scroll, fg_color="transparent")
            grid_container.pack(fill="both", expand=True, padx=2, pady=2)
            for c in range(cols):
                grid_container.grid_columnconfigure(c, weight=1)

            for pidx in range(self.total_pages):
                raw = self.raw_pil_thumbs.get(pidx)
                if raw is None:
                    raw = Image.new("RGB", (target_thumb_w, target_thumb_h), color=(220, 220, 220))

                t_img = raw.copy()
                t_img.thumbnail((target_thumb_w, target_thumb_h), Image.Resampling.LANCZOS)
                self.thumb_pil_images[pidx] = t_img.copy()

                ctk_img = ctk.CTkImage(light_image=t_img, dark_image=t_img, size=(t_img.width, t_img.height))
                self.thumb_images[pidx] = ctk_img

                r = pidx // cols
                c = pidx % cols

                card = ctk.CTkFrame(
                    grid_container,
                    corner_radius=4,
                    border_width=1,
                    border_color=("gray75", "#3a3a3a"),
                    fg_color=("gray95", "#252525"),
                    cursor="hand2"
                )
                card.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")

                lbl_img = ctk.CTkLabel(card, image=ctk_img, text="", cursor="hand2")
                lbl_img.pack(side="top", padx=3, pady=(4, 1))
                self.thumb_img_labels[pidx] = lbl_img

                lbl_txt = ctk.CTkLabel(
                    card,
                    text=f"{pidx + 1}",
                    font=ctk.CTkFont(size=10, weight="bold"),
                    cursor="hand2"
                )
                lbl_txt.pack(side="top", padx=2, pady=(0, 2))

                for w in [card, lbl_img, lbl_txt]:
                    w.bind("<Button-1>", lambda e, p=pidx: self._go_to_page(p))
                    if hasattr(w, "_label"):
                        w._label.bind("<Button-1>", lambda e, p=pidx: self._go_to_page(p))

                self.thumb_cards[pidx] = card
        else:
            # === LIST VIEW (With OCR info and proportional thumbnail size) ===
            # The thumbnail width scales proportionally with available sidebar width
            target_thumb_w = max(70, min(240, int(usable_w * 0.42)))
            target_thumb_h = int(target_thumb_w * 1.35)

            for pidx in range(self.total_pages):
                raw = self.raw_pil_thumbs.get(pidx)
                if raw is None:
                    raw = Image.new("RGB", (target_thumb_w, target_thumb_h), color=(220, 220, 220))

                t_img = raw.copy()
                t_img.thumbnail((target_thumb_w, target_thumb_h), Image.Resampling.LANCZOS)
                self.thumb_pil_images[pidx] = t_img.copy()

                ctk_img = ctk.CTkImage(light_image=t_img, dark_image=t_img, size=(t_img.width, t_img.height))
                self.thumb_images[pidx] = ctk_img

                card = ctk.CTkFrame(
                    self.thumb_scroll,
                    corner_radius=6,
                    border_width=1,
                    border_color=("gray75", "#3a3a3a"),
                    fg_color=("gray95", "#252525"),
                    cursor="hand2"
                )
                card.pack(fill="x", padx=4, pady=3)
                card.grid_columnconfigure(0, weight=0)
                card.grid_columnconfigure(1, weight=1)

                # Column 0: thumbnail + page label
                lbl_img = ctk.CTkLabel(card, image=ctk_img, text="", cursor="hand2")
                lbl_img.grid(row=0, column=0, padx=(5, 4), pady=(5, 1))
                self.thumb_img_labels[pidx] = lbl_img

                lbl_txt = ctk.CTkLabel(
                    card,
                    text=f"{self._tr('tree_item_page').format(idx=pidx + 1)}",
                    font=ctk.CTkFont(size=11, weight="bold"),
                    cursor="hand2"
                )
                lbl_txt.grid(row=1, column=0, padx=(5, 4), pady=(0, 4))

                # Column 1: OCR text availability, characters and quality
                lbl_ocr = ctk.CTkLabel(card, text="", font=ctk.CTkFont(size=11), justify="left", anchor="w", cursor="hand2")
                lbl_ocr.grid(row=0, column=1, rowspan=2, sticky="w", padx=(4, 6), pady=4)
                self.thumb_ocr_labels[pidx] = lbl_ocr

                info = ocr_summary[pidx] if pidx < len(ocr_summary) else None
                self._update_thumb_ocr_label(pidx, info)

                for w in [card, lbl_img, lbl_txt, lbl_ocr]:
                    w.bind("<Button-1>", lambda e, p=pidx: self._go_to_page(p))
                    if hasattr(w, "_label"):
                        w._label.bind("<Button-1>", lambda e, p=pidx: self._go_to_page(p))

                self.thumb_cards[pidx] = card

        self._highlight_active_thumb()
        self._update_thumbnail_lens_overlay()

    def _update_thumbnail_lens_overlay(self):
        """Draws dynamic focus rectangle(s) matching the active lens(es) over the current page thumbnail."""
        if self.page_idx not in self.thumb_pil_images or self.page_idx not in self.thumb_img_labels:
            return
        lbl = self.thumb_img_labels[self.page_idx]
        if lbl is None or not lbl.winfo_exists():
            return

        base_pil = self.thumb_pil_images[self.page_idx]
        tw, th = base_pil.size
        if tw <= 0 or th <= 0:
            return

        has_any = any(self.lens_focus_regions.get(k) is not None for k in ("orig", "tuned"))
        if not has_any:
            ctk_thumb = self.thumb_images.get(self.page_idx)
            if ctk_thumb is not None:
                lbl.configure(image=ctk_thumb)
            return

        annotated = base_pil.copy()
        draw = ImageDraw.Draw(annotated)

        # Draw original lens rectangle (golden yellow #f1c40f) and/or tuned lens rectangle (green #2ecc71)
        colors = {"orig": "#f1c40f", "tuned": "#2ecc71"}
        for target_type in ("orig", "tuned"):
            reg = self.lens_focus_regions.get(target_type)
            if reg is None:
                continue
            nx1, ny1, nx2, ny2 = reg
            rx1 = max(0, min(tw - 1, int(round(nx1 * tw))))
            ry1 = max(0, min(th - 1, int(round(ny1 * th))))
            rx2 = max(0, min(tw - 1, int(round(nx2 * tw))))
            ry2 = max(0, min(th - 1, int(round(ny2 * th))))
            if rx2 <= rx1:
                rx2 = min(tw - 1, rx1 + 3)
            if ry2 <= ry1:
                ry2 = min(th - 1, ry1 + 3)

            col = colors.get(target_type, "#f1c40f")
            draw.rectangle([rx1, ry1, rx2, ry2], outline=col, width=2)

        ctk_annotated = ctk.CTkImage(light_image=annotated, dark_image=annotated, size=(tw, th))
        self._current_annotated_thumb = ctk_annotated
        lbl.configure(image=ctk_annotated)

    def _update_thumb_ocr_label(self, pidx, info):
        """Renders the OCR info column of a thumbnail card (has OCR? chars + quality badge)."""
        lbl = self.thumb_ocr_labels.get(pidx)
        if lbl is None or not lbl.winfo_exists():
            return
        if not info or not info.get("has_ocr"):
            lbl.configure(text=self._tr("thumb_ocr_no"), text_color=("gray45", "gray60"))
            return
        chars = int(info.get("chars") or 0)
        conf = info.get("confidence")
        lines = [self._tr("thumb_ocr_yes"), self._tr("thumb_chars").format(n=f"{chars:,}")]
        color = ("#145a32", "#27ae60")
        if conf is not None:
            conf = float(conf)
            if conf < 65.0:
                lines.append(f"🔴 {conf:.0f}%")
                color = ("#c0392b", "#e74c3c")
            elif conf < 80.0:
                lines.append(f"⚠️ {conf:.0f}%")
                color = ("#b9770e", "#f39c12")
            else:
                lines.append(f"🟢 {conf:.0f}%")
        if info.get("manual_edit"):
            lines.append(self._tr("thumb_ocr_edited"))
        lbl.configure(text="\n".join(lines), text_color=color)

    def _layout_subwindows(self):
        """Places the 3 subwindows and 2 splitters inside view_container using proportional pixel widths."""
        if not hasattr(self, "view_container") or not self.view_container.winfo_exists():
            return
        W = self.view_container.winfo_width()
        H = self.view_container.winfo_height()
        if W <= 50 or H <= 50:
            return

        sash_w = 6
        min_thumb_w = 70
        max_thumb_w = max(min_thumb_w, W - 250)
        thumb_w = max(min_thumb_w, min(int(self.thumb_width), max_thumb_w))

        rem_w = max(120, W - thumb_w - 2 * sash_w)
        min_img_w = 60
        orig_w = int(rem_w * self.orig_ratio)
        orig_w = max(min_img_w, min(orig_w, rem_w - min_img_w))
        prev_w = max(min_img_w, rem_w - orig_w)

        x = 0
        tk.Frame.place(self.v_thumb_box, x=x, y=0, width=thumb_w, height=H)
        x += thumb_w

        tk.Frame.place(self.sash_thumb, x=x, y=0, width=sash_w, height=H)
        x += sash_w

        tk.Frame.place(self.v_orig_box, x=x, y=0, width=orig_w, height=H)
        x += orig_w

        tk.Frame.place(self.sash_previews, x=x, y=0, width=sash_w, height=H)
        x += sash_w

        tk.Frame.place(self.v_prev_box, x=x, y=0, width=prev_w, height=H)

    def _bind_sash(self, sash_widget, sash_name):
        """Binds hover and drag events to a splitter sash."""
        default_fg = sash_widget.cget("fg_color")

        def _on_enter(e):
            sash_widget.configure(fg_color="#3498db")

        def _on_leave(e):
            if not self._sash_drag_data or self._sash_drag_data.get("sash") != sash_name:
                sash_widget.configure(fg_color=default_fg)

        def _on_press(e):
            if sash_name == "thumb":
                self._sash_drag_data = {
                    "sash": "thumb",
                    "start_x": e.x_root,
                    "start_thumb_w": self.thumb_width
                }
            else:
                self._sash_drag_data = {
                    "sash": "previews",
                    "start_x": e.x_root,
                    "start_orig_w": self.v_orig_box.winfo_width(),
                    "total_imgs_w": self.v_orig_box.winfo_width() + self.v_prev_box.winfo_width()
                }

        def _on_motion(e):
            if not self._sash_drag_data or self._sash_drag_data.get("sash") != sash_name:
                return
            dx = e.x_root - self._sash_drag_data["start_x"]
            if sash_name == "thumb":
                new_w = self._sash_drag_data["start_thumb_w"] + dx
                self.thumb_width = max(80, new_w)
                self._layout_subwindows()
                self._refresh_image_displays()
                self._schedule_thumbnails_resize()
            else:
                total_w = self._sash_drag_data["total_imgs_w"]
                if total_w > 0:
                    new_orig_w = self._sash_drag_data["start_orig_w"] + dx
                    self.orig_ratio = max(0.1, min(0.9, float(new_orig_w) / float(total_w)))
                    self._layout_subwindows()
                    self._refresh_image_displays()

        def _on_release(e):
            self._sash_drag_data = None
            sash_widget.configure(fg_color=default_fg)
            self._layout_subwindows()
            self._refresh_image_displays()
            if sash_name == "thumb":
                self._schedule_thumbnails_resize()

        sash_widget.bind("<Enter>", _on_enter)
        sash_widget.bind("<Leave>", _on_leave)
        sash_widget.bind("<ButtonPress-1>", _on_press)
        sash_widget.bind("<B1-Motion>", _on_motion)
        sash_widget.bind("<ButtonRelease-1>", _on_release)

    def _on_container_resize(self, event):
        self._layout_subwindows()
        if self._resize_timer is not None:
            self.after_cancel(self._resize_timer)
        self._resize_timer = self.after(80, self._refresh_image_displays)
        self._schedule_thumbnails_resize()

    def _schedule_thumbnails_resize(self):
        if self._thumb_resize_timer is not None:
            self.after_cancel(self._thumb_resize_timer)
        self._thumb_resize_timer = self.after(120, self._rebuild_thumbnails_view)

    def _refresh_image_displays(self):
        self._resize_timer = None
        if self.current_orig_bgr is not None and self.lbl_orig_img.winfo_exists():
            bw_orig = max(100, self.lbl_orig_img.winfo_width())
            bh_orig = max(100, self.lbl_orig_img.winfo_height())
            self._display_image_on_label(self.current_orig_bgr, self.lbl_orig_img, target_bw=bw_orig, target_bh=bh_orig)

        if self.current_tuned_bgr is not None and self.lbl_prev_img.winfo_exists():
            bw_tuned = max(100, self.lbl_prev_img.winfo_width())
            bh_tuned = max(100, self.lbl_prev_img.winfo_height())
            self._display_image_on_label(self.current_tuned_bgr, self.lbl_prev_img, target_bw=bw_tuned, target_bh=bh_tuned)

    def _on_color_dropdown_change(self, choice_str):
        cmode = self.color_opts_map.get(choice_str, "color")
        self.var_color_mode.set(cmode)
        self._update_preview()

    def _on_quality_slider_change(self, value):
        q = int(round(float(value)))
        self.var_jpeg_quality.set(q)
        self.lbl_q_val.configure(text=f"{q}%")
        self._update_preview()

    def _on_scope_dropdown_change(self, choice_str):
        self.var_scope_choice.set(self.scope_opts_map.get(choice_str, "batch"))
        self._update_preview()

    def _on_lens_zoom_change(self, choice_str):
        if choice_str == "Off":
            self._hide_lens()
            for t in ("orig", "tuned"):
                self._unpin_lens(t)
            self.lens_focus_regions["orig"] = None
            self.lens_focus_regions["tuned"] = None
            self._update_thumbnail_lens_overlay()
        else:
            for lens in self.pinned_lenses.values():
                if lens is not None:
                    lens.refresh()

    def _get_zoom_factor(self):
        zoom_str = self.var_lens_zoom.get()
        if zoom_str == "Off":
            return None
        try:
            return float(zoom_str.replace("%", "").strip()) / 100.0
        except ValueError:
            return 1.5

    def _get_lens_geometry(self, target_type):
        """
        Returns geometry relative to the image's subwindow (v_orig_box / v_prev_box):
        (subwindow, nw, nh, img_x, img_y, sub_w, sub_h)
        """
        subwindow = getattr(self, "v_orig_box" if target_type == "orig" else "v_prev_box", None)
        target_label = self.lbl_orig_img if target_type == "orig" else self.lbl_prev_img
        display_info = getattr(self, "orig_display_info" if target_type == "orig" else "tuned_display_info", None)
        if subwindow is None or target_label is None or not display_info:
            return None

        nw, nh, _, _ = display_info
        sub_w = subwindow.winfo_width()
        sub_h = subwindow.winfo_height()
        if sub_w <= 10 or sub_h <= 10:
            subwindow.update_idletasks()
            sub_w = subwindow.winfo_width()
            sub_h = subwindow.winfo_height()

        lbl_root_x = target_label.winfo_rootx()
        lbl_root_y = target_label.winfo_rooty()
        sub_root_x = subwindow.winfo_rootx()
        sub_root_y = subwindow.winfo_rooty()

        img_x = max(0.0, float(lbl_root_x - sub_root_x))
        img_y = max(0.0, float(lbl_root_y - sub_root_y))

        return subwindow, nw, nh, img_x, img_y, sub_w, sub_h

    def _pin_lens_at(self, event, target_type):
        """
        Left double-click: pins a lens on this image subwindow at the cursor position.
        The top-left corner of the lens matches exactly the clicked real point in the subwindow,
        and expands to the right and down.
        """
        zoom = self._get_zoom_factor()
        if zoom is None:
            return
        geo = self._get_lens_geometry(target_type)
        target_bgr = self.current_orig_bgr if target_type == "orig" else self.current_tuned_bgr
        if geo is None or target_bgr is None:
            return
        subwindow, nw, nh, img_x, img_y, sub_w, sub_h = geo

        click_sub_x = event.x_root - subwindow.winfo_rootx()
        click_sub_y = event.y_root - subwindow.winfo_rooty()

        if not (0 <= click_sub_x < sub_w and 0 <= click_sub_y < sub_h):
            return

        old = self.pinned_lenses.get(target_type)
        lw, lh = (old.lw, old.lh) if old is not None else (PinnedLens.DEFAULT_SIZE, PinnedLens.DEFAULT_SIZE)
        self._unpin_lens(target_type)
        self._hide_lens()
        self.pinned_lenses[target_type] = PinnedLens(self, target_type, click_sub_x, click_sub_y, lw, lh)

    def _unpin_lens(self, target_type):
        lens = self.pinned_lenses.get(target_type)
        if lens is not None:
            lens.destroy()
        self.pinned_lenses[target_type] = None
        self.lens_focus_regions[target_type] = None
        self._update_thumbnail_lens_overlay()

    def _on_lens_motion(self, event, target_type):
        zoom_str = self.var_lens_zoom.get()
        if zoom_str == "Off" or self.pinned_lenses.get(target_type) is not None:
            # Floating lens disabled, or this image already has a pinned lens
            self._hide_lens()
            return

        try:
            zoom_factor = float(zoom_str.replace("%", "").strip()) / 100.0
        except ValueError:
            zoom_factor = 1.5

        target_bgr = self.current_orig_bgr if target_type == "orig" else self.current_tuned_bgr
        target_label = self.lbl_orig_img if target_type == "orig" else self.lbl_prev_img
        display_info = getattr(self, "orig_display_info" if target_type == "orig" else "tuned_display_info", None)

        if target_bgr is None or target_label is None or not display_info:
            self._hide_lens()
            return

        nw, nh, offset_x, offset_y = display_info
        inner_widget = getattr(target_label, "_label", target_label)

        # Track cursor relative to the rendered image inside the label widget
        img_root_x = inner_widget.winfo_rootx()
        img_root_y = inner_widget.winfo_rooty()

        cursor_x_in_img = float(event.x_root - img_root_x)
        cursor_y_in_img = float(event.y_root - img_root_y)

        if cursor_x_in_img < 0 or cursor_x_in_img > nw or cursor_y_in_img < 0 or cursor_y_in_img > nh:
            self._hide_lens()
            return

        norm_x = max(0.0, min(1.0, cursor_x_in_img / float(nw))) if nw > 0 else 0.0
        norm_y = max(0.0, min(1.0, cursor_y_in_img / float(nh))) if nh > 0 else 0.0

        img_h, img_w = target_bgr.shape[:2]
        cx_px = int(norm_x * img_w)
        cy_px = int(norm_y * img_h)

        crop_fraction = 0.25 / zoom_factor
        crop_w_px = max(16, int(crop_fraction * img_w))
        crop_h_px = max(16, int(crop_fraction * img_h))

        # The upper-left corner of the magnified patch corresponds to the point where the mouse is
        x1 = cx_px
        y1 = cy_px
        x2 = min(img_w, x1 + crop_w_px)
        y2 = min(img_h, y1 + crop_h_px)

        if x2 - x1 < crop_w_px:
            x1 = max(0, x2 - crop_w_px)
        if y2 - y1 < crop_h_px:
            y1 = max(0, y2 - crop_h_px)

        patch = target_bgr[y1:y2, x1:x2]
        if patch.size == 0:
            return

        # Update thumbnail overlay with active floating lens region
        if img_w > 0 and img_h > 0:
            norm_x1 = max(0.0, min(1.0, float(x1) / float(img_w)))
            norm_y1 = max(0.0, min(1.0, float(y1) / float(img_h)))
            norm_x2 = max(0.0, min(1.0, float(x2) / float(img_w)))
            norm_y2 = max(0.0, min(1.0, float(y2) / float(img_h)))
            self.lens_focus_regions[target_type] = (norm_x1, norm_y1, norm_x2, norm_y2)
            self._update_thumbnail_lens_overlay()

        lens_size = 220
        zoomed = cv2.resize(patch, (lens_size, lens_size), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(zoomed, cv2.COLOR_BGR2RGB)
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))

        root_x = event.x_root + 12
        root_y = event.y_root + 12

        # Prevent lens popover from escaping the screen boundary
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        if root_x + lens_size > screen_w:
            root_x = max(0, event.x_root - lens_size - 12)
        if root_y + lens_size > screen_h:
            root_y = max(0, event.y_root - lens_size - 12)

        border_col = "#f1c40f" if target_type == "orig" else "#2ecc71"
        if self.lens_popover is None or not self.lens_popover.winfo_exists():
            self.lens_popover = tk.Toplevel(self)
            self.lens_popover.overrideredirect(True)
            self.lens_popover.attributes("-topmost", True)
            self.lbl_lens_img = tk.Label(self.lens_popover, bd=2, relief="solid", bg=border_col)
            self.lbl_lens_img.pack()
            self.lens_popover.update_idletasks()
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.lens_popover.winfo_id()) or self.lens_popover.winfo_id()
                GWL_EXSTYLE = -20
                WS_EX_TRANSPARENT = 0x00000020
                WS_EX_LAYERED = 0x00080000
                LWA_ALPHA = 0x00000002
                curr_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, curr_style | WS_EX_TRANSPARENT | WS_EX_LAYERED)
                ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            except Exception:
                pass
        else:
            try:
                self.lbl_lens_img.configure(bg=border_col)
            except Exception:
                pass

        self.lens_popover.geometry(f"{lens_size}x{lens_size}+{root_x}+{root_y}")
        self.lbl_lens_img.configure(image=pi)
        self.lbl_lens_img.image = pi
        self.lens_popover.lift()

    def _hide_lens(self):
        if self.lens_popover is not None and self.lens_popover.winfo_exists():
            self.lens_popover.destroy()
            self.lens_popover = None
        changed = False
        for t in ("orig", "tuned"):
            if self.pinned_lenses.get(t) is None and self.lens_focus_regions.get(t) is not None:
                self.lens_focus_regions[t] = None
                changed = True
        if changed:
            self._update_thumbnail_lens_overlay()

    def _update_preview(self):
        if self.current_orig_bgr is None:
            return

        bgr_orig = self.current_orig_bgr
        h_orig, w_orig = bgr_orig.shape[:2]

        cmode = self.var_color_mode.get()
        try:
            target_dpi = int(self.var_dpi.get().split()[0])
        except Exception:
            target_dpi = 150
        jpeg_q = self.var_jpeg_quality.get()

        active_dict = {
            "color_mode": cmode,
            "target_dpi": target_dpi,
            "jpeg_quality": jpeg_q,
            "downsample_max_dim": 1500 if target_dpi <= 150 else 2000
        }

        # Apply in memory according to chosen scope
        scope = self.var_scope_choice.get()
        page_str = str(self.page_idx)
        if scope == "page":
            if self.filename not in self.page_tunings:
                self.page_tunings[self.filename] = {}
            self.page_tunings[self.filename][page_str] = active_dict
        elif scope == "file":
            self.file_tunings[self.filename] = active_dict
            if self.filename in self.page_tunings and page_str in self.page_tunings[self.filename]:
                del self.page_tunings[self.filename][page_str]
        else: # "batch"
            self.global_tuning = dict(active_dict)
            if self.filename in self.file_tunings:
                del self.file_tunings[self.filename]
            if self.filename in self.page_tunings and page_str in self.page_tunings[self.filename]:
                del self.page_tunings[self.filename][page_str]

        # Process tuned image preview matching exact pipeline stages (resize -> enhance_contrast_lab -> apply_color_mode -> 1ch/3ch JPEG)
        orig_dpi = self.current_page_orig_dpi or 300
        eff_dpi = min(target_dpi, orig_dpi) if (target_dpi < orig_dpi and orig_dpi > 0) else orig_dpi
        cache_key = (self.file_path, self.page_idx, eff_dpi, w_orig, h_orig)

        if getattr(self, "_enhanced_base_key", None) == cache_key and getattr(self, "_enhanced_base_bgr", None) is not None:
            bgr_enhanced_base = self._enhanced_base_bgr
        else:
            if target_dpi < orig_dpi and orig_dpi > 0:
                scale = float(target_dpi) / float(orig_dpi)
                new_w = max(1, int(w_orig * scale))
                new_h = max(1, int(h_orig * scale))
                bgr_scaled = cv2.resize(bgr_orig, (new_w, new_h), interpolation=cv2.INTER_AREA)
            else:
                bgr_scaled = bgr_orig
            bgr_enhanced_base = enhance_contrast_lab(bgr_scaled, dpi=eff_dpi)
            self._enhanced_base_key = cache_key
            self._enhanced_base_bgr = bgr_enhanced_base

        bgr_tuned = apply_color_mode(bgr_enhanced_base, cmode)

        # Estimate file size reduction and decode JPEG back so preview reflects real 1-channel / 3-channel JPEG compression
        orig_bytes = w_orig * h_orig * 3
        jpeg_stream = encode_page_jpeg_bytes(bgr_tuned, jpeg_quality=jpeg_q, color_mode=cmode)
        if jpeg_stream:
            tuned_bytes = len(jpeg_stream)
            enc_arr = np.frombuffer(jpeg_stream, dtype=np.uint8)
            decoded_bgr = cv2.imdecode(enc_arr, cv2.IMREAD_COLOR)
            if decoded_bgr is not None:
                bgr_tuned = decoded_bgr
        else:
            tuned_bytes = orig_bytes

        self.current_tuned_bgr = bgr_tuned

        orig_kb = orig_bytes / 1024.0
        tuned_kb = tuned_bytes / 1024.0
        red_pct = max(0.0, (1.0 - (tuned_kb / orig_kb)) * 100.0) if orig_kb > 0 else 0.0

        self.lbl_stats.configure(
            text=f"Est. Size: ~{tuned_kb:.1f} KB  |  Redução: ~{red_pct:.1f}% vs Raw",
            text_color=("#145a32", "#27ae60") if red_pct > 50 else ("#b9770e", "#f39c12")
        )

        detect_cmode = "Grayscale" if len(bgr_orig.shape) == 2 or (bgr_orig[:,:,0] == bgr_orig[:,:,1]).all() else "Color RGB"
        self.lbl_orig_meta_cmode.configure(text=f"Color Mode: {detect_cmode}")
        self.lbl_orig_meta_dpi.configure(text=f"Original DPI: {orig_dpi} DPI")
        self.lbl_orig_meta_dims.configure(text=f"Dimensions: {w_orig} × {h_orig} px")

        self._refresh_image_displays()

    def _display_image_on_label(self, bgr_img, label_widget, target_bw=None, target_bh=None):
        h, w = bgr_img.shape[:2]
        bw = target_bw or label_widget.winfo_width() or 350
        bh = target_bh or label_widget.winfo_height() or 400

        bw = max(100, bw)
        bh = max(100, bh)

        scale = min(float(bw) / float(w), float(bh) / float(h))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))

        resized = cv2.resize(bgr_img, (nw, nh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        pil_im = Image.fromarray(rgb)
        ctk_img = ctk.CTkImage(light_image=pil_im, dark_image=pil_im, size=(nw, nh))

        label_widget.configure(image=ctk_img, text="", anchor="nw")
        label_widget.image = ctk_img

        offset_x = 0.0
        offset_y = 0.0
        if label_widget == self.lbl_orig_img:
            self.orig_display_info = (nw, nh, offset_x, offset_y)
            lens = self.pinned_lenses.get("orig")
        else:
            self.tuned_display_info = (nw, nh, offset_x, offset_y)
            lens = self.pinned_lenses.get("tuned")
        if lens is not None:
            # Re-clamp and re-render the pinned lens with the new image / geometry
            self.after_idle(lens.refresh)

    def _save_options_to_file(self):
        success = save_tuning_config(
            self.folder_path,
            global_tuning=self.global_tuning,
            file_tunings=self.file_tunings,
            page_tunings=self.page_tunings
        )
        if success:
            cfg_path = os.path.join(self.folder_path, TUNING_FILENAME)
            messagebox.showinfo(
                "Opções Gravadas",
                f"As opções foram gravadas com sucesso no ficheiro de configuração:\n\n{cfg_path}\n\nSerão automaticamente aplicadas no S2POT!",
                parent=self
            )
            if self.on_save_callback:
                self.on_save_callback()
        else:
            messagebox.showerror("Erro", "Não foi possível gravar o ficheiro de calibração.", parent=self)

    def _get_results_dirs(self):
        """Resolves the output and data directories for the current document."""
        out_dir = None
        data_dir = None

        m = self.master
        for obj in (m, getattr(m, "parent_app", None)):
            if obj is not None:
                if hasattr(obj, "output_entry"):
                    try:
                        d = obj.output_entry.get().strip().strip('"\'')
                        if d and os.path.exists(d):
                            out_dir = d
                    except Exception:
                        pass
                if not out_dir:
                    d = getattr(obj, "output_dir", None) or getattr(obj, "docs_dir", None)
                    if d and os.path.exists(d):
                        out_dir = d

                if hasattr(obj, "data_entry"):
                    try:
                        d = obj.data_entry.get().strip().strip('"\'')
                        if d and os.path.exists(d):
                            data_dir = d
                    except Exception:
                        pass
                if not data_dir:
                    d = getattr(obj, "data_dir", None)
                    if d and os.path.exists(d):
                        data_dir = d

        if not out_dir or not data_dir:
            cfg = load_config()
            if not out_dir:
                d = cfg.get("last_output_dir", "")
                if d and os.path.exists(d):
                    out_dir = d
            if not data_dir:
                d = cfg.get("last_data_dir", "")
                if d and os.path.exists(d):
                    data_dir = d

        if not out_dir:
            out_dir = self.folder_path
        if not data_dir:
            data_dir = out_dir

        return out_dir, data_dir

    def _open_ocr_text_editor(self):
        out_dir, data_dir = self._get_results_dirs()
        if self.ocr_editor is not None and self.ocr_editor.winfo_exists():
            self.ocr_editor.lift()
            self.ocr_editor.focus_force()
            return

        self.ocr_editor = OCRTextEditorWindow(
            self,
            source_path=self.file_path,
            page_idx=self.page_idx,
            total_pages=self.total_pages,
            output_dir=out_dir,
            data_dir=data_dir,
            lang=self.lang,
            on_saved=self._on_ocr_text_saved
        )

    def _on_ocr_text_saved(self, page_idx, res):
        chars = res.get("chars", 0)
        conf = 100.0 if chars > 0 else 0.0
        self._update_thumb_ocr_label(page_idx, {
            "has_ocr": chars > 0,
            "chars": chars,
            "confidence": conf,
            "manual_edit": True
        })

        # Update parent applications and tables
        m = self.master
        parent_app = getattr(m, "parent_app", None) or (m if hasattr(m, "_refresh_all_row_displays") else None)
        if parent_app is not None:
            in_dir = getattr(parent_app, "input_dir", None)
            if hasattr(parent_app, "input_entry"):
                try:
                    d = parent_app.input_entry.get().strip().strip('"\'')
                    if d and os.path.exists(d):
                        in_dir = d
                except Exception:
                    pass
            if in_dir and os.path.exists(in_dir):
                parent_app.folder_metadata = load_folder_metadata(in_dir)

            if hasattr(parent_app, "item_to_info"):
                fname = os.path.basename(self.file_path)
                fmeta = (parent_app.folder_metadata or {}).get("files", {}).get(fname)
                if fmeta:
                    for iid, info in parent_app.item_to_info.items():
                        if info.get("type") == "file" and info.get("filename") == fname:
                            info["total_chars"] = fmeta.get("total_chars")
                            info["confidence"] = fmeta.get("confidence")
                            info["min_page_confidence"] = fmeta.get("min_page_confidence")
                            info["low_conf_pages"] = fmeta.get("low_conf_pages")
                        elif info.get("type") == "page" and info.get("filename") == fname and info.get("page_idx") == page_idx:
                            info["chars"] = chars
                            info["confidence"] = conf
                if hasattr(parent_app, "_refresh_all_row_displays"):
                    parent_app._refresh_all_row_displays()

            if getattr(parent_app, "results_window", None) is not None:
                try:
                    parent_app.results_window.refresh_results()
                except Exception:
                    pass

        if hasattr(m, "refresh_results"):
            try:
                m.refresh_results()
            except Exception:
                pass

        if self.on_save_callback:
            try:
                self.on_save_callback()
            except Exception:
                pass

    def _on_help_toggle(self):
        val = bool(self.var_help_mode.get())
        import core.i18n
        core.i18n.HELP_MODE_ENABLED = val

        # Persist to config
        cfg = load_config() or {}
        cfg["help_mode"] = val
        save_config(cfg)

        # Sync master / application if present
        m = self.master
        for obj in (m, getattr(m, "parent_app", None)):
            if obj is not None:
                if hasattr(obj, "chk_help_var"):
                    obj.chk_help_var.set(val)
                if hasattr(obj, "chk_help") and hasattr(obj.chk_help, "select") and hasattr(obj.chk_help, "deselect"):
                    if val:
                        obj.chk_help.select()
                    else:
                        obj.chk_help.deselect()

    def _on_language_change(self, choice_str):
        code = "pt_PT"
        for c, name in LANGUAGES.items():
            if name == choice_str:
                code = c
                break

        self.lang = code
        self.current_language = code

        # Persist
        cfg = load_config() or {}
        cfg["language"] = code
        save_config(cfg)

        # Notify master / parent app
        m = self.master
        parent_app = getattr(m, "parent_app", None) or m
        if parent_app is not None:
            if hasattr(parent_app, "_on_language_change"):
                try:
                    parent_app._on_language_change(choice_str)
                except Exception:
                    pass
            elif hasattr(parent_app, "change_language"):
                try:
                    parent_app.change_language(choice_str)
                except Exception:
                    pass
            elif hasattr(parent_app, "retranslate"):
                try:
                    parent_app.retranslate(code)
                except Exception:
                    pass

        self.retranslate(code)

    def retranslate(self, new_lang=None):
        if new_lang:
            self.lang = new_lang
            self.current_language = new_lang

        curr_lang_name = LANGUAGES.get(self.lang, "Português (PT)")
        self.cmb_lang.set(curr_lang_name)
        self.lbl_lang.configure(text=self._tr("language"))
        self.chk_help.configure(text=self._tr("help_mode"))

        self.lbl_thumb_title.configure(text=f"📑 {self._tr('sidebar_pages')}")
        self.lbl_grid_cols.configure(text=self._tr("thumb_grid_cols"))
        if self.thumb_view_mode == "grid":
            self.btn_thumb_mode.configure(text=f"☰ {self._tr('thumb_mode_list')}")
        else:
            self.btn_thumb_mode.configure(text=f"⊞ {self._tr('thumb_mode_grid')}")

        self.btn_edit_ocr.configure(text=f"✏️ {self._tr('btn_edit_ocr_text')}")
        self.btn_save.configure(text=self._tr("btn_save_options_file"))

        self.title(f"{self._tr('page_viewer_title')} — {self.filename} ({self._tr('tree_item_page').format(idx=self.page_idx + 1)})")
        self.lbl_doc_info.configure(text=f"📄 {self.filename}  |  Pág {self.page_idx + 1} de {self.total_pages}")

        # Update Color Options Dropdown
        self.color_opts_map = {
            self._tr("color_mode_color"): "color",
            self._tr("color_mode_grayscale"): "grayscale",
            self._tr("color_mode_bw"): "monochrome"
        }
        self.cmb_color.configure(values=list(self.color_opts_map.keys()))
        cmode = self.var_color_mode.get()
        if cmode == "grayscale":
            self.cmb_color.set(self._tr("color_mode_grayscale"))
        elif cmode == "monochrome":
            self.cmb_color.set(self._tr("color_mode_bw"))
        else:
            self.cmb_color.set(self._tr("color_mode_color"))

        # Update Scope Options Dropdown
        self.scope_opts_map = {
            self._tr("scope_all_batch"): "batch",
            self._tr("scope_this_file"): "file",
            self._tr("scope_this_page"): "page"
        }
        self.cmb_scope.configure(values=list(self.scope_opts_map.keys()))
        s_choice = self.var_scope_choice.get()
        if s_choice == "page":
            self.cmb_scope.set(self._tr("scope_this_page"))
        elif s_choice == "file":
            self.cmb_scope.set(self._tr("scope_this_file"))
        else:
            self.cmb_scope.set(self._tr("scope_all_batch"))

        self._rebuild_thumbnails_view()


class PinnedLens:
    """
    Fixed/Pinned Magnification Lens overlay on an image subwindow (v_orig_box / v_prev_box).
    Key behaviors:
      - Can occupy ANY area in the image's subwindow (not confined to the image bounds).
      - The top-left corner of the lens rectangle ALWAYS coincides with the exact real point
        in the subwindow on which it was placed, anchored in place.
      - Resizing expands ALWAYS to the right and down from that anchored top-left point.
      - Body dragging/moving is disabled.
      - The resize handle is located at the bottom-right corner (and right/bottom edges).
      - Real-time magnified patch safely renders with neutral padding when extending outside image.
      - Double-click inside the lens (left or right) unpins / removes it.
    """
    DEFAULT_SIZE = 220
    MIN_SIZE = 60
    CORNER_SIZE = 14
    SOURCE_OFFSET = 0

    def __init__(self, studio, target_type, top_left_x, top_left_y, lw=220, lh=220):
        self.studio = studio
        self.target_type = target_type

        geo = self.studio._get_lens_geometry(self.target_type)
        if not geo:
            return
        subwindow, nw, nh, img_x, img_y, sub_w, sub_h = geo
        self.subwindow = subwindow

        # The top-left corner ALWAYS matches the exact real point in the subwindow
        self.lx = int(max(0, min(top_left_x, sub_w - self.MIN_SIZE)))
        self.ly = int(max(0, min(top_left_y, sub_h - self.MIN_SIZE)))

        max_w = max(self.MIN_SIZE, sub_w - self.lx)
        max_h = max(self.MIN_SIZE, sub_h - self.ly)
        self.lw = int(max(self.MIN_SIZE, min(lw, max_w)))
        self.lh = int(max(self.MIN_SIZE, min(lh, max_h)))

        border_col = "#f1c40f" if self.target_type == "orig" else "#2ecc71"
        self.canvas = tk.Canvas(
            self.subwindow,
            width=self.lw,
            height=self.lh,
            bd=2,
            relief="solid",
            highlightthickness=0,
            bg=border_col
        )
        self.canvas.place(x=self.lx, y=self.ly, width=self.lw, height=self.lh)
        tk.Misc.lift(self.canvas)

        self._mode = None
        self._photo_ref = None

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_button_press)
        self.canvas.bind("<B1-Motion>", self._on_b1_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_button_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Double-Button-3>", self._on_double_click)

        self.refresh()

    def _hit_test(self, x, y):
        cs = self.CORNER_SIZE
        if x <= cs and y <= cs:
            return "nw"
        elif x >= self.lw - cs and y <= cs:
            return "ne"
        elif x <= cs and y >= self.lh - cs:
            return "sw"
        elif x >= self.lw - cs and y >= self.lh - cs:
            return "se"
        return None

    def _on_motion(self, event):
        mode = self._hit_test(event.x, event.y)
        if mode in ("nw", "se"):
            self.canvas.configure(cursor="size_nw_se")
        elif mode in ("ne", "sw"):
            self.canvas.configure(cursor="size_ne_sw")
        else:
            self.canvas.configure(cursor="")

    def _on_button_press(self, event):
        self._mode = self._hit_test(event.x, event.y)
        self._start_x_root = event.x_root
        self._start_y_root = event.y_root
        self._start_lx = self.lx
        self._start_ly = self.ly
        self._start_lw = self.lw
        self._start_lh = self.lh

    def _on_b1_motion(self, event):
        if not self._mode:
            return
        geo = self.studio._get_lens_geometry(self.target_type)
        if not geo:
            return
        subwindow, nw, nh, img_x, img_y, sub_w, sub_h = geo

        dx = event.x_root - self._start_x_root
        dy = event.y_root - self._start_y_root

        # All 4 corners can be moved; whenever top-left (lx, ly) moves, it becomes the new zoom expansion anchor
        if self._mode == "se":
            self.lw = max(self.MIN_SIZE, min(self._start_lw + dx, sub_w - self._start_lx))
            self.lh = max(self.MIN_SIZE, min(self._start_lh + dy, sub_h - self._start_ly))
        elif self._mode == "nw":
            fixed_se_x = self._start_lx + self._start_lw
            fixed_se_y = self._start_ly + self._start_lh
            new_lx = max(0, min(self._start_lx + dx, fixed_se_x - self.MIN_SIZE))
            new_ly = max(0, min(self._start_ly + dy, fixed_se_y - self.MIN_SIZE))
            self.lx = new_lx
            self.ly = new_ly
            self.lw = fixed_se_x - new_lx
            self.lh = fixed_se_y - new_ly
        elif self._mode == "ne":
            fixed_sw_x = self._start_lx
            fixed_sw_y = self._start_ly + self._start_lh
            new_ly = max(0, min(self._start_ly + dy, fixed_sw_y - self.MIN_SIZE))
            new_lw = max(self.MIN_SIZE, min(self._start_lw + dx, sub_w - fixed_sw_x))
            self.lx = fixed_sw_x
            self.ly = new_ly
            self.lw = new_lw
            self.lh = fixed_sw_y - new_ly
        elif self._mode == "sw":
            fixed_ne_x = self._start_lx + self._start_lw
            fixed_ne_y = self._start_ly
            new_lx = max(0, min(self._start_lx + dx, fixed_ne_x - self.MIN_SIZE))
            new_lh = max(self.MIN_SIZE, min(self._start_lh + dy, sub_h - fixed_ne_y))
            self.lx = new_lx
            self.ly = fixed_ne_y
            self.lw = fixed_ne_x - new_lx
            self.lh = new_lh

        self.canvas.place(x=self.lx, y=self.ly, width=self.lw, height=self.lh)
        tk.Misc.lift(self.canvas)
        self.refresh()

    def _on_button_release(self, event):
        self._mode = None

    def _on_double_click(self, event):
        self.studio._unpin_lens(self.target_type)

    def _clamp_bounds(self):
        geo = self.studio._get_lens_geometry(self.target_type)
        if not geo:
            return
        subwindow, nw, nh, img_x, img_y, sub_w, sub_h = geo

        self.lx = int(max(0, min(self.lx, sub_w - self.MIN_SIZE)))
        self.ly = int(max(0, min(self.ly, sub_h - self.MIN_SIZE)))

        max_w = max(self.MIN_SIZE, sub_w - self.lx)
        max_h = max(self.MIN_SIZE, sub_h - self.ly)
        self.lw = max(self.MIN_SIZE, min(self.lw, max_w))
        self.lh = max(self.MIN_SIZE, min(self.lh, max_h))

    def refresh(self):
        if not self.canvas or not self.canvas.winfo_exists():
            return
        geo = self.studio._get_lens_geometry(self.target_type)
        target_bgr = self.studio.current_orig_bgr if self.target_type == "orig" else self.studio.current_tuned_bgr
        if not geo or target_bgr is None:
            return

        subwindow, nw, nh, img_x, img_y, sub_w, sub_h = geo
        self._clamp_bounds()
        self.canvas.place(x=self.lx, y=self.ly, width=self.lw, height=self.lh)
        tk.Misc.lift(self.canvas)

        zoom = self.studio._get_zoom_factor() or 1.5

        # Real point in the image where the top-left corner (lx, ly) falls
        pt_x_in_img = float(self.lx - img_x)
        pt_y_in_img = float(self.ly - img_y)

        img_h, img_w = target_bgr.shape[:2]

        px_start_x = int(round((pt_x_in_img / float(nw)) * img_w)) if nw > 0 else 0
        px_start_y = int(round((pt_y_in_img / float(nh)) * img_h)) if nh > 0 else 0

        crop_w_px = max(4, int(round((self.lw / float(nw)) * img_w / zoom))) if nw > 0 else 100
        crop_h_px = max(4, int(round((self.lh / float(nh)) * img_h / zoom))) if nh > 0 else 100

        x1 = px_start_x
        y1 = px_start_y
        x2 = x1 + crop_w_px
        y2 = y1 + crop_h_px

        # Safe patch with neutral background padding for areas outside image bounds
        patch = np.full((crop_h_px, crop_w_px, 3), (235, 235, 235), dtype=np.uint8)
        ix1 = max(0, min(img_w, x1))
        ix2 = max(0, min(img_w, x2))
        iy1 = max(0, min(img_h, y1))
        iy2 = max(0, min(img_h, y2))

        if ix2 > ix1 and iy2 > iy1:
            px1 = ix1 - x1
            px2 = px1 + (ix2 - ix1)
            py1 = iy1 - y1
            py2 = py1 + (iy2 - iy1)
            patch[py1:py2, px1:px2] = target_bgr[iy1:iy2, ix1:ix2]

        zoomed = cv2.resize(patch, (self.lw, self.lh), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(zoomed, cv2.COLOR_BGR2RGB)
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))
        self._photo_ref = pi

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=pi)

        border_color = "#f1c40f" if self.target_type == "orig" else "#2ecc71"
        self.canvas.create_rectangle(1, 1, self.lw - 1, self.lh - 1, outline=border_color, width=3)

        # 4 selectable/draggable corner handles (NW, NE, SW, SE)
        cs = self.CORNER_SIZE
        self._draw_corner(0, 0, cs, cs, border_color)
        self._draw_corner(self.lw - cs, 0, self.lw - 1, cs, border_color)
        self._draw_corner(0, self.lh - cs, cs, self.lh - 1, border_color)
        self._draw_corner(self.lw - cs, self.lh - cs, self.lw - 1, self.lh - 1, border_color)

        # Update thumbnail overlay with the normalized region within the image bounds
        if img_w > 0 and img_h > 0 and ix2 > ix1 and iy2 > iy1:
            norm_x1 = max(0.0, min(1.0, float(ix1) / float(img_w)))
            norm_y1 = max(0.0, min(1.0, float(iy1) / float(img_h)))
            norm_x2 = max(0.0, min(1.0, float(ix2) / float(img_w)))
            norm_y2 = max(0.0, min(1.0, float(iy2) / float(img_h)))
            self.studio.lens_focus_regions[self.target_type] = (norm_x1, norm_y1, norm_x2, norm_y2)
        else:
            self.studio.lens_focus_regions[self.target_type] = None
        self.studio._update_thumbnail_lens_overlay()

    def _draw_corner(self, x0, y0, x1, y1, color):
        self.canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="#1a1a1a", width=1)

    def destroy(self):
        if self.canvas is not None and self.canvas.winfo_exists():
            self.canvas.destroy()
        self.canvas = None


class OCRTextEditorWindow(ctk.CTkToplevel):
    """
    Dedicated OCR Text Viewer & Editor Window.
    Allows viewing and editing OCR text of the selected page,
    with live character and line metrics, and incorporates the saved edits
    into all generated document and data formats (PDF, DOCX, TXT, hOCR, coordinates, metadata).
    """
    def __init__(self, master, source_path, page_idx, total_pages, output_dir=None, data_dir=None, lang="pt_PT", on_saved=None):
        super().__init__(master)
        self.source_path = source_path
        self.filename = os.path.basename(source_path)
        self.page_idx = page_idx
        self.total_pages = total_pages
        self.output_dir = output_dir
        self.data_dir = data_dir
        self.lang = lang
        self.on_saved = on_saved

        self.title(f"{self._tr('ocr_editor_title')} — {self.filename} (Pág {self.page_idx + 1} de {self.total_pages})")
        self.geometry("780x640")
        self.minsize(580, 420)

        self.ocr_info = load_page_ocr_text(self.source_path, self.page_idx, output_dir=self.output_dir, data_dir=self.data_dir)
        self.original_text = self.ocr_info.get("text", "")
        self.is_modified = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_ui(self):
        # Header Bar
        hdr = ctk.CTkFrame(self, height=54, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        hdr.pack(side="top", fill="x")

        # Document and page info
        left_box = ctk.CTkFrame(hdr, fg_color="transparent")
        left_box.pack(side="left", padx=14, pady=8)

        lbl_title = ctk.CTkLabel(
            left_box,
            text=f"📝 {self.filename}  |  Pág {self.page_idx + 1} de {self.total_pages}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#3498db"
        )
        lbl_title.pack(anchor="w")

        src_name = os.path.basename(self.ocr_info.get("source") or "") or "OCR / Metadata"
        lbl_src = ctk.CTkLabel(
            left_box,
            text=self._tr("ocr_editor_source").format(src=src_name),
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65")
        )
        lbl_src.pack(anchor="w")

        # Quality Badge
        conf = self.ocr_info.get("confidence")
        if conf is not None and self.ocr_info.get("has_ocr"):
            c_val = float(conf)
            if c_val >= 80.0:
                conf_text = f"🟢 {c_val:.0f}%"
                conf_color = ("#145a32", "#27ae60")
            elif c_val >= 65.0:
                conf_text = f"⚠️ {c_val:.0f}%"
                conf_color = ("#b9770e", "#f39c12")
            else:
                conf_text = f"🔴 {c_val:.0f}%"
                conf_color = ("#c0392b", "#e74c3c")
        else:
            conf_text = "Sem OCR"
            conf_color = ("gray45", "gray60")

        self.lbl_conf_badge = ctk.CTkLabel(
            hdr,
            text=conf_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=conf_color
        )
        self.lbl_conf_badge.pack(side="right", padx=16, pady=8)

        # Hint Banner
        hint_frame = ctk.CTkFrame(self, fg_color=("gray85", "#222222"), corner_radius=0)
        hint_frame.pack(fill="x", padx=12, pady=(6, 4))

        ctk.CTkLabel(
            hint_frame,
            text=f"ℹ️ {self._tr('ocr_editor_hint')}",
            font=ctk.CTkFont(size=11),
            text_color=("gray30", "gray75"),
            justify="left"
        ).pack(side="left", padx=10, pady=5)

        # Main Text Editor Area
        editor_container = ctk.CTkFrame(self, fg_color="transparent")
        editor_container.pack(fill="both", expand=True, padx=12, pady=4)

        self.txt_editor = ctk.CTkTextbox(
            editor_container,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="word",
            undo=True
        )
        self.txt_editor.pack(fill="both", expand=True)
        self.txt_editor.insert("1.0", self.original_text)
        self.txt_editor.bind("<<Modified>>", self._on_text_modified)
        self.txt_editor.bind("<KeyRelease>", self._update_stats)

        # Stats Bar below Textbox
        stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        stats_frame.pack(fill="x", padx=14, pady=(2, 4))

        self.lbl_stats = ctk.CTkLabel(
            stats_frame,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65")
        )
        self.lbl_stats.pack(side="left")

        self.lbl_mod_flag = ctk.CTkLabel(
            stats_frame,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#e67e22"
        )
        self.lbl_mod_flag.pack(side="right")

        # Bottom Action Bar
        bot_bar = ctk.CTkFrame(self, height=48, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        bot_bar.pack(side="bottom", fill="x")

        self.btn_revert = ctk.CTkButton(
            bot_bar,
            text=self._tr("ocr_editor_revert"),
            height=30,
            width=130,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#7f8c8d",
            hover_color="#95a5a6",
            command=self._on_revert_clicked
        )
        self.btn_revert.pack(side="left", padx=12, pady=8)

        self.btn_close = ctk.CTkButton(
            bot_bar,
            text=self._tr("btn_close"),
            height=30,
            width=90,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#34495e",
            hover_color="#2c3e50",
            command=self._on_close
        )
        self.btn_close.pack(side="right", padx=12, pady=8)

        self.btn_save = ctk.CTkButton(
            bot_bar,
            text=self._tr("ocr_editor_save"),
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            command=self._on_save_clicked
        )
        self.btn_save.pack(side="right", padx=(0, 8), pady=8)

        self._update_stats()

    def _on_text_modified(self, event=None):
        if self.txt_editor.edit_modified():
            current = self.txt_editor.get("1.0", "end-1c")
            self._set_modified(current != self.original_text)
            self._update_stats()
            self.txt_editor.edit_modified(False)

    def _set_modified(self, modified):
        self.is_modified = modified
        if modified:
            self.lbl_mod_flag.configure(text=self._tr("ocr_editor_modified"))
            self.title(f"● {self._tr('ocr_editor_title')} — {self.filename} (Pág {self.page_idx + 1})")
        else:
            self.lbl_mod_flag.configure(text="")
            self.title(f"{self._tr('ocr_editor_title')} — {self.filename} (Pág {self.page_idx + 1})")

    def _update_stats(self, event=None):
        txt = self.txt_editor.get("1.0", "end-1c")
        chars = sum(len(w) for w in txt.split())
        lines = len([ln for ln in txt.split("\n") if ln.strip()])
        conf = estimate_text_layer_confidence(txt.split(), txt) if chars > 0 else 0.0
        conf_badge = f"🟢 {conf:.0f}%" if conf >= 80.0 else (f"⚠️ {conf:.0f}%" if conf >= 65.0 else f"🔴 {conf:.0f}%")

        stats_txt = self._tr("ocr_editor_stats").format(
            chars=f"{chars:,}",
            lines=lines,
            conf=conf_badge
        )
        self.lbl_stats.configure(text=stats_txt)

    def _on_revert_clicked(self):
        self.txt_editor.delete("1.0", "end")
        self.txt_editor.insert("1.0", self.original_text)
        self._set_modified(False)
        self._update_stats()

    def _on_save_clicked(self):
        new_text = self.txt_editor.get("1.0", "end-1c")
        self.btn_save.configure(state="disabled", text=self._tr("ocr_editor_saving"))
        self.update_idletasks()

        res = save_edited_page_ocr_text(
            self.source_path,
            self.page_idx,
            new_text,
            output_dir=self.output_dir,
            data_dir=self.data_dir
        )

        self.btn_save.configure(state="normal", text=self._tr("ocr_editor_save"))

        updated_files = res.get("updated", [])
        errors = res.get("errors", [])

        if errors:
            err_msg = "\n".join(f"• {p}: {e}" for p, e in errors)
            messagebox.showwarning(
                self._tr("ocr_editor_title"),
                f"{self._tr('ocr_editor_errors')}\n\n{err_msg}",
                parent=self
            )

        if updated_files:
            file_names = "\n".join(f"• {os.path.basename(p)}" for p in updated_files)
            messagebox.showinfo(
                self._tr("ocr_editor_done_title"),
                f"{self._tr('ocr_editor_done_msg').format(n=len(updated_files))}\n\n{file_names}",
                parent=self
            )
            self.original_text = new_text
            self._set_modified(False)
            if self.on_saved:
                self.on_saved(self.page_idx, res)
        elif not errors:
            messagebox.showinfo(
                self._tr("ocr_editor_title"),
                self._tr("ocr_editor_no_results"),
                parent=self
            )

    def _on_close(self):
        if self.is_modified:
            if not messagebox.askyesno(self._tr("ocr_editor_title"), self._tr("ocr_editor_confirm_close"), parent=self):
                return
        self.destroy()


class S2PITApp(ctk.CTk):
    """
    S2PIT — Smart Slim Preview Tool
    Catalog & File System Browser with Expandable Page Details and Dedicated Tuning Studio.
    """
    def __init__(self, initial_input_dir=None):
        super().__init__()

        self.config_data = load_config()
        self.lang = self.config_data.get("language", "pt_PT")

        self.title(f"S2PIT — Smart Slim Preview Tool v{APP_VERSION}")
        self.geometry("1100x750")
        self.minsize(900, 600)

        self.input_dir = initial_input_dir or self.config_data.get("last_input_dir", "")
        self.active_studio = None

        self._build_ui()

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_ui(self):
        # Header Bar
        hdr_frame = ctk.CTkFrame(self, height=48, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        hdr_frame.pack(side="top", fill="x")

        self.lbl_title = ctk.CTkLabel(
            hdr_frame,
            text=f"S2PIT — Smart Slim Preview Tool v{APP_VERSION}",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_title.pack(side="left", padx=15, pady=10)

        # Action Buttons in Header
        act_frame = ctk.CTkFrame(hdr_frame, fg_color="transparent")
        act_frame.pack(side="right", padx=15, pady=8)

        self.btn_inspect = ctk.CTkButton(
            act_frame,
            text=self._tr("btn_inspect"),
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#6c3483",
            hover_color="#512e5f",
            command=self._on_inspect_clicked
        )
        self.btn_inspect.pack(side="left", padx=(0, 10))

        self.btn_tune = ctk.CTkButton(
            act_frame,
            text=self._tr("btn_tune_selected_page"),
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="gray50",
            hover_color="#512e5f",
            state="disabled",
            command=self._open_tuning_studio
        )
        self.btn_tune.pack(side="left", padx=(0, 10))

        # Language dropdown
        ctk.CTkLabel(act_frame, text=self._tr("language"), font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 5))
        self.cmb_lang = ctk.CTkOptionMenu(
            act_frame,
            values=list(LANGUAGES.values()),
            width=120,
            font=ctk.CTkFont(size=12),
            command=self._on_language_change
        )
        self.cmb_lang.set(LANGUAGES.get(self.lang, "Português (PT)"))
        self.cmb_lang.pack(side="left")

        # Main Body: Reusable FilePageTreeBrowser
        self.browser = FilePageTreeBrowser(
            self,
            initial_dir=self.input_dir,
            lang=self.lang,
            selectmode="browse",
            show_path_bar=True,
            show_filters=True,
            columns=("Format", "PagesChars", "Size", "ImgRes", "Status"),
            on_page_activated=self._on_page_activated,
            on_selection_changed=self._on_selection_changed
        )
        self.browser.pack(fill="both", expand=True, padx=12, pady=10)

        # Status Bar
        self.status_bar = ctk.CTkFrame(self, height=32, fg_color=("gray90", "#1a1a1a"), corner_radius=0)
        self.status_bar.pack(side="bottom", fill="x")

        self.lbl_status = ctk.CTkLabel(
            self.status_bar,
            text="Faça duplo clique numa página ou clique em 'Ver Página' para abrir o estúdio de calibração.",
            font=ctk.CTkFont(size=12),
            text_color=("gray30", "gray70")
        )
        self.lbl_status.pack(side="left", padx=12, pady=4)

    def _on_selection_changed(self, selected_files, selected_pages):
        count = len(selected_files) + len(selected_pages)
        if count > 0:
            self.btn_tune.configure(state="normal", fg_color="#6c3483", hover_color="#512e5f")
        else:
            self.btn_tune.configure(state="disabled", fg_color="gray50")
        if selected_pages:
            fpath, pidx = selected_pages[0]
            fname = os.path.basename(fpath)
            self.lbl_status.configure(text=f"Página selecionada: {fname} (Pág {pidx + 1}). Clique em 'Ver Páginas' para calibrar.")
        elif selected_files:
            fname = os.path.basename(selected_files[0])
            self.lbl_status.configure(text=f"Ficheiro selecionado: {fname}. Expanda para ver as páginas ou clique em 'Ver Páginas'.")

    def _on_page_activated(self, fpath, page_idx):
        self._open_studio_for(fpath, page_idx)

    def _open_tuning_studio(self):
        fpath, pidx = self.browser.get_selected_page()
        if not fpath:
            files = self.browser.get_selected_files()
            if files:
                fpath, pidx = files[0], 0
            else:
                messagebox.showwarning("Aviso", "Por favor selecione um ficheiro ou página na tabela primeiro.", parent=self)
                return
        self._open_studio_for(fpath, pidx)

    def _open_studio_for(self, fpath, page_idx):
        if self.active_studio is not None and self.active_studio.winfo_exists():
            self.active_studio.destroy()

        self.active_studio = S2PITPageTuningWindow(
            self,
            file_path=fpath,
            page_idx=page_idx,
            lang=self.lang,
            on_save_callback=self._on_tuning_saved
        )

    def _on_tuning_saved(self):
        self.browser.refresh()

    def _on_inspect_clicked(self):
        selected_files = self.browser.get_selected_files()
        targets = selected_files if selected_files else self.browser.found_files
        if not targets:
            messagebox.showwarning("Aviso", "Nenhum ficheiro para inspecionar.", parent=self)
            return
        self.browser.inspect_files(targets)

    def _on_language_change(self, choice):
        for k, v in LANGUAGES.items():
            if v == choice:
                self.lang = k
                break
        self.config_data["language"] = self.lang
        save_config(self.config_data)
        self.lbl_title.configure(text=f"S2PIT — Smart Slim Preview Tool v{APP_VERSION}")
        self.btn_inspect.configure(text=self._tr("btn_inspect"))
        self.btn_tune.configure(text=self._tr("btn_tune_selected_page"))

# S2POT Page Viewer Alias
S2POTPageViewerWindow = S2PITPageTuningWindow


def launch_s2pit_gui(initial_dir=None):
    app = S2PITApp(initial_input_dir=initial_dir)
    app.mainloop()


if __name__ == "__main__":
    launch_s2pit_gui()
