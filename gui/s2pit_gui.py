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
from PIL import Image, ImageTk

from core.config import load_config, save_config, APP_VERSION
from core.i18n import get_text
from core.tuning_config import (
    load_tuning_config,
    save_tuning_config,
    get_effective_file_tuning,
    DEFAULT_GLOBAL_TUNING,
    TUNING_FILENAME
)
from core.pipeline import apply_color_mode
from gui.tooltip import ToolTip

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class S2PITApp(ctk.CTk):
    """
    S2PIT — Smart Slim Preview Tool v1.0.0
    Standalone interactive preview & tuning tool for PDF/TIFF/Image batch compression.
    """
    def __init__(self, initial_input_dir=None):
        super().__init__()

        self.config_data = load_config()
        self.lang = self.config_data.get("language", "pt_PT")

        self.title(f"S2PIT — Smart Slim Preview Tool v{APP_VERSION}")
        self.geometry("1280x880")
        self.minsize(1100, 750)

        self.input_dir = initial_input_dir or self.config_data.get("last_input_dir", "")
        self.found_files = []
        self.file_items = {}
        self.item_to_filepath = {}
        self.selected_file_path = None
        self.selected_page_idx = 0
        self.page_images_cache = []

        # Current Tuning State (Global, Per-file, and Per-page)
        self.global_tuning = dict(DEFAULT_GLOBAL_TUNING)
        self.file_tunings = {}
        self.page_tunings = {}

        self.var_color_mode = ctk.StringVar(value="color")
        self.var_dpi = ctk.StringVar(value="150 DPI")
        self.var_jpeg_quality = ctk.IntVar(value=60)
        self.var_scope_choice = ctk.StringVar(value="batch") # "batch", "file", "page"

        # Image display cache & lens tracking
        self.current_orig_bgr = None
        self.current_tuned_bgr = None
        self.orig_display_info = None  # (nw, nh, offset_x, offset_y)
        self.tuned_display_info = None # (nw, nh, offset_x, offset_y)

        self.lens_popover = None
        self.lbl_lens_img = None

        self._resize_timer = None

        self._build_ui()

        if self.input_dir and os.path.exists(self.input_dir):
            self.input_entry.delete(0, "end")
            self.input_entry.insert(0, self.input_dir)
            self._scan_input_folder()

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_ui(self):
        # Top Header Bar
        hdr_frame = ctk.CTkFrame(self, height=45, fg_color="#1e1e1e", corner_radius=0)
        hdr_frame.pack(side="top", fill="x")

        self.lbl_title = ctk.CTkLabel(
            hdr_frame,
            text=f"S2PIT — Smart Slim Preview Tool v{APP_VERSION}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_title.pack(side="left", padx=15, pady=8)

        # Lens Zoom Level Dropdown in Header Bar (Off, 125%, 150%, 175%, 200%, 300%, 400%)
        lens_frame = ctk.CTkFrame(hdr_frame, fg_color="transparent")
        lens_frame.pack(side="right", padx=15, pady=6)

        ctk.CTkLabel(
            lens_frame,
            text=f"🔍 {self._tr('lens_mode')}:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f1c40f"
        ).pack(side="left", padx=(0, 6))

        self.cmb_lens = ctk.CTkOptionMenu(
            lens_frame,
            values=["Off", "125%", "150%", "175%", "200%", "300%", "400%"],
            width=95,
            command=lambda v: self._on_lens_zoom_change(v)
        )
        self.cmb_lens.set("Off")
        self.cmb_lens.pack(side="left")

        # Main Split Layout: Left Panel (Files) & Right Panel (Preview & Tuning)
        main_paned = ctk.CTkFrame(self, fg_color="transparent")
        main_paned.pack(fill="both", expand=True, padx=10, pady=10)
        main_paned.grid_columnconfigure(0, weight=3) # Left Column (Files)
        main_paned.grid_columnconfigure(1, weight=7) # Right Column (Preview & Tuning)
        main_paned.grid_rowconfigure(0, weight=1)

        # --- LEFT COLUMN (Box 1 & Box 4) ---
        left_frame = ctk.CTkFrame(main_paned, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        # Box 1: Input Folder
        box1 = ctk.CTkFrame(left_frame)
        box1.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(box1, text=self._tr("box_1_input"), font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(8, 2))

        in_row = ctk.CTkFrame(box1, fg_color="transparent")
        in_row.pack(fill="x", padx=10, pady=5)
        self.input_entry = ctk.CTkEntry(in_row, placeholder_text=self._tr("input_placeholder"))
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.input_entry.bind("<Return>", self._on_input_entry_change)
        self.input_entry.bind("<FocusOut>", self._on_input_entry_change)

        btn_browse = ctk.CTkButton(in_row, text=self._tr("browse"), width=80, command=self._browse_input)
        btn_browse.pack(side="right")

        # Format Checkboxes Row
        fmt_row = ctk.CTkFrame(box1, fg_color="transparent")
        fmt_row.pack(fill="x", padx=10, pady=(0, 8))
        self.chk_pdf = ctk.CTkCheckBox(fmt_row, text="PDF", command=self._scan_input_folder)
        self.chk_pdf.select()
        self.chk_pdf.pack(side="left", padx=5)
        self.chk_tiff = ctk.CTkCheckBox(fmt_row, text="TIFF", command=self._scan_input_folder)
        self.chk_tiff.select()
        self.chk_tiff.pack(side="left", padx=5)
        self.chk_jpg = ctk.CTkCheckBox(fmt_row, text="JPG", command=self._scan_input_folder)
        self.chk_jpg.select()
        self.chk_jpg.pack(side="left", padx=5)

        # Box 4: Files Table
        box4 = ctk.CTkFrame(left_frame)
        box4.pack(fill="both", expand=True)

        tbl_hdr = ctk.CTkFrame(box4, fg_color="transparent")
        tbl_hdr.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(tbl_hdr, text=self._tr("box_table"), font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")

        self.lbl_badge = ctk.CTkLabel(tbl_hdr, text="", font=ctk.CTkFont(size=11, weight="bold"), text_color="#3498db")
        self.lbl_badge.pack(side="right")

        tbl_frame = ctk.CTkFrame(box4, fg_color="transparent")
        tbl_frame.pack(fill="both", expand=True, padx=10, pady=5)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", rowheight=24, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#1f1f1f", foreground="white", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1f618d")])

        cols = ("Filename", "Format", "Size", "Pages", "ImgRes")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", selectmode="browse")

        self.tree.heading("Filename", text=self._tr("col_filename"))
        self.tree.heading("Format", text=self._tr("col_format"))
        self.tree.heading("Size", text=self._tr("col_size"))
        self.tree.heading("Pages", text=self._tr("col_pages"))
        self.tree.heading("ImgRes", text=self._tr("col_img_res"))

        self.tree.column("Filename", width=160, anchor="w")
        self.tree.column("Format", width=60, anchor="center")
        self.tree.column("Size", width=65, anchor="e")
        self.tree.column("Pages", width=45, anchor="center")
        self.tree.column("ImgRes", width=60, anchor="center")

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)

        # --- RIGHT COLUMN (Interactive Preview & Tuning Studio) ---
        right_frame = ctk.CTkFrame(main_paned)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right_frame.grid_rowconfigure(1, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)

        # 1. Top Thumbnails Bar
        self.thumb_bar = ctk.CTkScrollableFrame(right_frame, orientation="horizontal", height=65, fg_color="#1a1a1a")
        self.thumb_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))

        # 2. Central Split View Container (Original vs Result - FORCED STRICT 50% / 50% UNIFORM WEIGHT)
        self.view_container = ctk.CTkFrame(right_frame, fg_color="transparent")
        self.view_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        self.view_container.grid_columnconfigure(0, weight=1, uniform="preview_cols")
        self.view_container.grid_columnconfigure(1, weight=1, uniform="preview_cols")
        self.view_container.grid_rowconfigure(0, weight=1)
        self.view_container.bind("<Configure>", self._on_container_resize)

        # === LEFT COLUMN: ORIGINAL PAGE BOX ===
        v_orig_box = ctk.CTkFrame(self.view_container)
        v_orig_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        v_orig_box.grid_rowconfigure(1, weight=1)
        v_orig_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(v_orig_box, text="📷 ORIGINAL PAGE", font=ctk.CTkFont(size=12, weight="bold"), text_color="#7f8c8d").grid(row=0, column=0, pady=(6, 2))

        self.lbl_orig_img = ctk.CTkLabel(v_orig_box, text="No file selected")
        self.lbl_orig_img.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # Original Metadata Card (3 Lines - Height Aligned with Right Tuning Card)
        orig_card = ctk.CTkFrame(v_orig_box, fg_color=("gray90", "#242424"), corner_radius=6)
        orig_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))

        self.lbl_orig_meta_cmode = ctk.CTkLabel(orig_card, text="Color Mode: -", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_orig_meta_cmode.pack(anchor="w", padx=8, pady=(4, 1))

        self.lbl_orig_meta_dpi = ctk.CTkLabel(orig_card, text="Original DPI: -", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_orig_meta_dpi.pack(anchor="w", padx=8, pady=1)

        self.lbl_orig_meta_dims = ctk.CTkLabel(orig_card, text="Dimensions: -", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_orig_meta_dims.pack(anchor="w", padx=8, pady=(1, 4))

        # === RIGHT COLUMN: TUNED RESULT PAGE BOX ===
        v_prev_box = ctk.CTkFrame(self.view_container)
        v_prev_box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        v_prev_box.grid_rowconfigure(1, weight=1)
        v_prev_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(v_prev_box, text="⚡ TUNED PREVIEW RESULT", font=ctk.CTkFont(size=12, weight="bold"), text_color="#2ecc71").grid(row=0, column=0, pady=(6, 2))

        self.lbl_prev_img = ctk.CTkLabel(v_prev_box, text="No file selected")
        self.lbl_prev_img.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # Attach Lens Motion and Leave Event Handlers directly to Label Widgets
        for lbl_widget, ttype in [(self.lbl_orig_img, "orig"), (self.lbl_prev_img, "tuned")]:
            lbl_widget.bind("<Motion>", lambda e, t=ttype: self._on_lens_motion(e, t))
            lbl_widget.bind("<Leave>", lambda e: self._hide_lens())
            if hasattr(lbl_widget, "_label"):
                lbl_widget._label.bind("<Motion>", lambda e, t=ttype: self._on_lens_motion(e, t))
                lbl_widget._label.bind("<Leave>", lambda e: self._hide_lens())

        # 3-Line Vertical Tuning Controls Card (Under Tuned Result Page)
        tuning_card = ctk.CTkFrame(v_prev_box, fg_color=("gray90", "#242424"), corner_radius=6)
        tuning_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))

        # Line 1: Color Mode Dropdown
        line1 = ctk.CTkFrame(tuning_card, fg_color="transparent")
        line1.pack(fill="x", padx=6, pady=(4, 1))

        ctk.CTkLabel(line1, text=f"{self._tr('color_mode_label')}", font=ctk.CTkFont(size=11, weight="bold")).pack(side="left", padx=(0, 6))

        self.color_opts_map = {
            self._tr("color_mode_color"): "color",
            self._tr("color_mode_grayscale"): "grayscale",
            self._tr("color_mode_bw"): "monochrome"
        }
        self.cmb_color = ctk.CTkOptionMenu(
            line1,
            values=list(self.color_opts_map.keys()),
            width=180,
            command=lambda v: self._on_color_dropdown_change(v)
        )
        self.cmb_color.pack(side="left", fill="x", expand=True)

        # Line 2: Target DPI Dropdown
        line2 = ctk.CTkFrame(tuning_card, fg_color="transparent")
        line2.pack(fill="x", padx=6, pady=1)

        ctk.CTkLabel(line2, text="Target DPI:", font=ctk.CTkFont(size=11, weight="bold")).pack(side="left", padx=(0, 6))
        self.cmb_dpi = ctk.CTkOptionMenu(
            line2,
            values=["300 DPI", "200 DPI", "150 DPI", "120 DPI"],
            variable=self.var_dpi,
            width=140,
            command=lambda v: self._update_preview()
        )
        self.cmb_dpi.pack(side="left", fill="x", expand=True)

        # Line 3: JPEG Quality Slider
        line3 = ctk.CTkFrame(tuning_card, fg_color="transparent")
        line3.pack(fill="x", padx=6, pady=(1, 4))

        ctk.CTkLabel(line3, text="JPEG Quality:", font=ctk.CTkFont(size=11, weight="bold")).pack(side="left", padx=(0, 4))
        self.lbl_q_val = ctk.CTkLabel(line3, text=f"{self.var_jpeg_quality.get()}%", font=ctk.CTkFont(size=11, weight="bold"), text_color="#3498db")
        self.lbl_q_val.pack(side="left", padx=(0, 6))

        self.sld_q = ctk.CTkSlider(line3, from_=30, to=95, number_of_steps=65, variable=self.var_jpeg_quality, command=self._on_quality_slider_change)
        self.sld_q.pack(side="left", fill="x", expand=True, padx=4)

        # Real-time Stats Badge
        self.lbl_size_stats = ctk.CTkLabel(
            right_frame,
            text="Original: - MB  |  Tuned Preview: - KB  (-0%)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#2ecc71"
        )
        self.lbl_size_stats.grid(row=2, column=0, sticky="ew", padx=10, pady=4)

        # Bottom Scope & Save Options Action Bar
        act_bar = ctk.CTkFrame(right_frame, fg_color="transparent")
        act_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 8))

        ctk.CTkLabel(act_bar, text="Aplicar:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(5, 6))

        self.scope_opts_map = {
            self._tr("scope_all_batch"): "batch",
            self._tr("scope_this_file"): "file",
            self._tr("scope_this_page"): "page"
        }
        self.cmb_scope = ctk.CTkOptionMenu(
            act_bar,
            values=list(self.scope_opts_map.keys()),
            width=220,
            command=lambda v: self._on_scope_dropdown_change(v)
        )
        self.cmb_scope.pack(side="left", padx=5)

        self.btn_save = ctk.CTkButton(
            act_bar,
            text=self._tr("btn_save_options_file"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            font=ctk.CTkFont(weight="bold"),
            command=self._save_rules_to_folder
        )
        self.btn_save.pack(side="right", padx=5)

    def _on_container_resize(self, event):
        """Dynamic resize handler: resizes displayed images when the main window expands."""
        if self._resize_timer is not None:
            self.after_cancel(self._resize_timer)
        self._resize_timer = self.after(100, self._refresh_image_displays)

    def _refresh_image_displays(self):
        self._resize_timer = None
        if self.current_orig_bgr is not None and self.current_tuned_bgr is not None:
            bw_orig = self.lbl_orig_img.winfo_width()
            bh_orig = self.lbl_orig_img.winfo_height()
            bw_tuned = self.lbl_prev_img.winfo_width()
            bh_tuned = self.lbl_prev_img.winfo_height()

            bw = min(bw_orig, bw_tuned) if (bw_orig > 50 and bw_tuned > 50) else max(bw_orig, bw_tuned, 350)
            bh = min(bh_orig, bh_tuned) if (bh_orig > 50 and bh_tuned > 50) else max(bh_orig, bh_tuned, 400)

            self._display_image_on_label(self.current_orig_bgr, self.lbl_orig_img, target_bw=bw, target_bh=bh)
            self._display_image_on_label(self.current_tuned_bgr, self.lbl_prev_img, target_bw=bw, target_bh=bh)

    def _on_color_dropdown_change(self, choice_str):
        cmode = self.color_opts_map.get(choice_str, "color")
        self.var_color_mode.set(cmode)
        self._update_preview()

    def _on_scope_dropdown_change(self, choice_str):
        scope = self.scope_opts_map.get(choice_str, "batch")
        self.var_scope_choice.set(scope)
        self._update_preview()

    def _on_lens_zoom_change(self, val):
        if val == "Off":
            self._hide_lens()

    def _hide_lens(self):
        if self.lens_popover is not None:
            try:
                self.lens_popover.destroy()
            except Exception:
                pass
            self.lens_popover = None
            self.lbl_lens_img = None

    def _on_lens_motion(self, event, target_type):
        zoom_str = self.cmb_lens.get()
        if zoom_str == "Off" or not zoom_str:
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

        cursor_x_in_label = float(event.x)
        cursor_y_in_label = float(event.y)

        cursor_x_in_img = cursor_x_in_label - offset_x
        cursor_y_in_img = cursor_y_in_label - offset_y

        if cursor_x_in_img < 0 or cursor_x_in_img > nw or cursor_y_in_img < 0 or cursor_y_in_img > nh:
            self._hide_lens()
            return

        norm_x = max(0.0, min(1.0, cursor_x_in_img / float(nw))) if nw > 0 else 0.5
        norm_y = max(0.0, min(1.0, cursor_y_in_img / float(nh))) if nh > 0 else 0.5

        img_h, img_w = target_bgr.shape[:2]
        cx_px = int(norm_x * img_w)
        cy_px = int(norm_y * img_h)

        crop_fraction = 0.25 / zoom_factor
        crop_w_px = max(16, int(crop_fraction * img_w))
        crop_h_px = max(16, int(crop_fraction * img_w))

        x1 = max(0, cx_px - crop_w_px // 2)
        y1 = max(0, cy_px - crop_h_px // 2)
        x2 = min(img_w, x1 + crop_w_px)
        y2 = min(img_h, y1 + crop_h_px)

        crop = target_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return

        disp_w, disp_h = 280, 280
        zoomed = cv2.resize(crop, (disp_w, disp_h), interpolation=cv2.INTER_CUBIC)

        cv2.rectangle(zoomed, (0, 0), (disp_w - 1, disp_h - 1), (0, 255, 240), 3)
        mid_x, mid_y = disp_w // 2, disp_h // 2
        cv2.drawMarker(zoomed, (mid_x, mid_y), (0, 255, 240), markerType=cv2.MARKER_CROSS, markerSize=16, thickness=2)

        rgb = cv2.cvtColor(zoomed, cv2.COLOR_BGR2RGB)
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))

        inner_widget = getattr(target_label, "_label", target_label)
        root_x = inner_widget.winfo_rootx() + event.x + 25
        root_y = inner_widget.winfo_rooty() + event.y + 25

        if self.lens_popover is None or not self.lens_popover.winfo_exists():
            self.lens_popover = tk.Toplevel(self)
            self.lens_popover.overrideredirect(True)
            self.lens_popover.attributes("-topmost", True)
            self.lbl_lens_img = tk.Label(self.lens_popover, bg="black")
            self.lbl_lens_img.pack()

        self.lens_popover.geometry(f"{disp_w}x{disp_h}+{root_x}+{root_y}")
        self.lbl_lens_img.configure(image=pi)
        self.lbl_lens_img.image = pi

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select Input Folder")
        if d:
            self.input_entry.delete(0, "end")
            self.input_entry.insert(0, d)
            self.input_dir = d
            self.config_data["last_input_dir"] = d
            save_config(self.config_data)
            self._scan_input_folder()

    def _on_input_entry_change(self, event=None):
        raw = self.input_entry.get().strip().strip('"\'')
        if raw and os.path.exists(raw):
            self.input_dir = raw
            self.config_data["last_input_dir"] = raw
            save_config(self.config_data)
            self._scan_input_folder()

    def _scan_input_folder(self):
        in_path = self.input_entry.get().strip()
        if not in_path or not os.path.exists(in_path):
            self.lbl_badge.configure(text=self._tr("found_files_badge").format(count=0))
            return

        loaded = load_tuning_config(in_path)
        if loaded:
            self.global_tuning = loaded.get("global_tuning", dict(DEFAULT_GLOBAL_TUNING))
            self.file_tunings = loaded.get("file_tunings", {})
            self.page_tunings = loaded.get("page_tunings", {})
        else:
            self.global_tuning = dict(DEFAULT_GLOBAL_TUNING)
            self.file_tunings = {}
            self.page_tunings = {}

        self._sync_tuning_ui_from_state()

        exts = []
        if self.chk_pdf.get(): exts.append("*.pdf")
        if self.chk_tiff.get(): exts.extend(["*.tif", "*.tiff"])
        if self.chk_jpg.get(): exts.extend(["*.jpg", "*.jpeg"])

        files = []
        for pattern in exts:
            files.extend(glob.glob(os.path.join(in_path, pattern)))
            files.extend(glob.glob(os.path.join(in_path, pattern.upper())))

        self.found_files = sorted(list(set(files)))
        self.lbl_badge.configure(text=self._tr("found_files_badge").format(count=len(self.found_files)))

        self.tree.delete(*self.tree.get_children())
        self.file_items = {}
        self.item_to_filepath = {}

        for f in self.found_files:
            fname = os.path.basename(f)
            ext = os.path.splitext(f)[1].upper().replace(".", "")
            size_mb = os.path.getsize(f) / (1024 * 1024)

            item_id = self.tree.insert("", "end", values=(fname, ext, f"{size_mb:.2f} MB", "-", "-"))
            self.file_items[f] = item_id
            self.item_to_filepath[item_id] = f

        if self.found_files:
            first_id = self.tree.get_children()[0]
            self.tree.selection_set(first_id)
            self._on_file_select(None)

    def _sync_tuning_ui_from_state(self):
        filename = os.path.basename(self.selected_file_path) if self.selected_file_path else None
        page_idx = self.selected_page_idx

        eff = get_effective_file_tuning(
            {"global_tuning": self.global_tuning, "file_tunings": self.file_tunings, "page_tunings": self.page_tunings},
            filename,
            page_idx
        )

        cmode = eff.get("color_mode", "color")
        if cmode == "grayscale":
            self.cmb_color.set(self._tr("color_mode_grayscale"))
        elif cmode == "monochrome":
            self.cmb_color.set(self._tr("color_mode_bw"))
        else:
            self.cmb_color.set(self._tr("color_mode_color"))

        self.var_color_mode.set(cmode)
        self.var_dpi.set(f"{eff.get('target_dpi', 300)} DPI")
        self.var_jpeg_quality.set(eff.get("jpeg_quality", 70))
        self.lbl_q_val.configure(text=f"{eff.get('jpeg_quality', 70)}%")

        page_str = str(page_idx)
        if filename and filename in self.page_tunings and page_str in self.page_tunings[filename]:
            self.cmb_scope.set(self._tr("scope_this_page"))
            self.var_scope_choice.set("page")
        elif filename and filename in self.file_tunings:
            self.cmb_scope.set(self._tr("scope_this_file"))
            self.var_scope_choice.set("file")
        else:
            self.cmb_scope.set(self._tr("scope_all_batch"))
            self.var_scope_choice.set("batch")

    def _get_file_max_dpi(self, fpath):
        ext = os.path.splitext(fpath)[1].lower()
        max_dpi = 0
        try:
            if ext == ".pdf":
                doc = fitz.open(fpath)
                for page in doc:
                    pt_w, pt_h = page.rect.width, page.rect.height
                    image_list = page.get_images()
                    if image_list:
                        for img in image_list:
                            try:
                                base_img = doc.extract_image(img[0])
                                w, h = base_img.get("width", 0), base_img.get("height", 0)
                                if pt_w > 0 and pt_h > 0 and w > 0 and h > 0:
                                    max_dpi = max(max_dpi, (w * 72.0) / pt_w, (h * 72.0) / pt_h)
                            except Exception: pass
                    else:
                        pix = page.get_pixmap()
                        if pt_w > 0 and pt_h > 0:
                            max_dpi = max(max_dpi, (pix.width * 72.0) / pt_w, (pix.height * 72.0) / pt_h)
                doc.close()

            elif ext in [".tif", ".tiff"]:
                tiff = Image.open(fpath)
                dpi = tiff.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    max_dpi = max(float(dpi[0]), float(dpi[1]))
            elif ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
                img = Image.open(fpath)
                dpi = img.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    max_dpi = max(float(dpi[0]), float(dpi[1]))
        except Exception:
            pass

        return int(round(max_dpi)) if max_dpi > 0 else 300

    def _on_file_select(self, event):
        selected_ids = self.tree.selection()
        if not selected_ids:
            return

        iid = selected_ids[0]
        fpath = self.item_to_filepath.get(iid)
        if not fpath or not os.path.exists(fpath):
            return

        self.selected_file_path = fpath
        self.selected_page_idx = 0
        self._load_file_pages(fpath)
        self._sync_tuning_ui_from_state()
        self._update_preview()

    def _load_file_pages(self, fpath):
        for widget in self.thumb_bar.winfo_children():
            widget.destroy()

        self.page_images_cache = []
        ext = os.path.splitext(fpath)[1].lower()

        try:
            if ext == ".pdf":
                doc = fitz.open(fpath)
                for idx, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                    self.page_images_cache.append(bgr)
                doc.close()
            elif ext in [".tif", ".tiff"]:
                tiff = Image.open(fpath)
                n_frames = getattr(tiff, "n_frames", 1)
                for i in range(n_frames):
                    tiff.seek(i)
                    img = tiff.convert("RGB")
                    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                    self.page_images_cache.append(bgr)
            else:
                bgr = cv2.imread(fpath)
                if bgr is not None:
                    self.page_images_cache.append(bgr)

        except Exception as e:
            print(f"Error loading pages for {fpath}: {e}")

        if fpath in self.file_items:
            iid = self.file_items[fpath]
            self.tree.set(iid, "Pages", str(len(self.page_images_cache)))
            max_dpi = self._get_file_max_dpi(fpath)
            self.tree.set(iid, "ImgRes", f"{max_dpi} DPI")

        for idx, bgr_img in enumerate(self.page_images_cache):
            h, w = bgr_img.shape[:2]
            scale = 50.0 / float(h)
            tw, th = max(1, int(w * scale)), 50
            thumb = cv2.resize(bgr_img, (tw, th))
            thumb_rgb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
            pi = ImageTk.PhotoImage(Image.fromarray(thumb_rgb))

            btn = ctk.CTkButton(
                self.thumb_bar,
                text=f"Pág {idx+1}",
                image=pi,
                compound="top",
                width=60,
                height=65,
                fg_color="#2980b9" if idx == self.selected_page_idx else "#333333",
                command=lambda p=idx: self._select_page(p)
            )
            btn.image = pi
            btn.pack(side="left", padx=4)

    def _select_page(self, page_idx):
        self.selected_page_idx = page_idx
        for idx, btn in enumerate(self.thumb_bar.winfo_children()):
            if isinstance(btn, ctk.CTkButton):
                btn.configure(fg_color="#2980b9" if idx == page_idx else "#333333")
        self._sync_tuning_ui_from_state()
        self._update_preview()

    def _update_preview(self):
        if not self.page_images_cache or self.selected_page_idx >= len(self.page_images_cache):
            return

        bgr_orig = self.page_images_cache[self.selected_page_idx]
        h_orig, w_orig = bgr_orig.shape[:2]

        cmode = self.var_color_mode.get()
        target_dpi = int(self.var_dpi.get().replace("DPI", "").strip())
        jpeg_q = self.var_jpeg_quality.get()

        filename = os.path.basename(self.selected_file_path) if self.selected_file_path else None
        page_idx = self.selected_page_idx
        page_str = str(page_idx)

        active_dict = {
            "color_mode": cmode,
            "target_dpi": target_dpi,
            "jpeg_quality": jpeg_q,
            "downsample_max_dim": 1500 if target_dpi <= 150 else 2000
        }

        scope = self.var_scope_choice.get()
        if scope == "page" and filename:
            if filename not in self.page_tunings:
                self.page_tunings[filename] = {}
            self.page_tunings[filename][page_str] = active_dict
        elif scope == "file" and filename:
            self.file_tunings[filename] = active_dict
            if filename in self.page_tunings and page_str in self.page_tunings[filename]:
                del self.page_tunings[filename][page_str]
        else: # "batch"
            self.global_tuning = dict(active_dict)
            if filename in self.file_tunings:
                del self.file_tunings[filename]
            if filename in self.page_tunings and page_str in self.page_tunings[filename]:
                del self.page_tunings[filename][page_str]

        # Apply Tuned Transformations
        bgr_tuned = apply_color_mode(bgr_orig.copy(), cmode)

        max_dim = 1500 if target_dpi <= 150 else 2000
        if max(h_orig, w_orig) > max_dim:
            sc = float(max_dim) / float(max(h_orig, w_orig))
            bgr_tuned = cv2.resize(bgr_tuned, (0, 0), fx=sc, fy=sc, interpolation=cv2.INTER_AREA)

        self.current_orig_bgr = bgr_orig
        self.current_tuned_bgr = bgr_tuned

        # Size estimation via JPEG encoding
        _, enc_orig = cv2.imencode('.jpg', bgr_orig, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        orig_kb = len(enc_orig.tobytes()) / 1024.0

        if cmode == "monochrome":
            _, enc_tuned = cv2.imencode('.png', bgr_tuned)
        else:
            _, enc_tuned = cv2.imencode('.jpg', bgr_tuned, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_q])
        tuned_kb = len(enc_tuned.tobytes()) / 1024.0

        red_pct = ((1.0 - tuned_kb / orig_kb) * 100.0) if orig_kb > 0 else 0.0
        self.lbl_size_stats.configure(
            text=f"Original Page: {orig_kb/1024.0:.2f} MB  |  Tuned Preview: {tuned_kb:.1f} KB  (-{red_pct:.1f}% Reduction)",
            text_color="#2ecc71" if red_pct > 20 else "#e67e22"
        )

        # Update Original Metadata Card
        detect_cmode = "Color (RGB)"
        if len(bgr_orig.shape) == 2 or (bgr_orig.shape[2] == 3 and np.array_equal(bgr_orig[:,:,0], bgr_orig[:,:,1]) and np.array_equal(bgr_orig[:,:,1], bgr_orig[:,:,2])):
            detect_cmode = "Grayscale (8-bit)"

        orig_dpi_val = self._get_file_max_dpi(self.selected_file_path) if self.selected_file_path else 300

        self.lbl_orig_meta_cmode.configure(text=f"Color Mode: {detect_cmode}")
        self.lbl_orig_meta_dpi.configure(text=f"Original DPI: {orig_dpi_val} DPI")
        self.lbl_orig_meta_dims.configure(text=f"Dimensions: {w_orig} × {h_orig} px")

        # Refresh displayed images with matching equal dimensions
        self._refresh_image_displays()

    def _display_image_on_label(self, bgr_img, label_widget, target_bw=None, target_bh=None):
        h, w = bgr_img.shape[:2]

        bw = target_bw or label_widget.winfo_width() or 350
        bh = target_bh or label_widget.winfo_height() or 400

        bw = max(100, bw)
        bh = max(100, bh)

        scale = min(float(bw) / float(w), float(bh) / float(h))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))

        res = cv2.resize(bgr_img, (nw, nh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(res, cv2.COLOR_BGR2RGB)
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))
        label_widget.configure(image=pi, text="")
        label_widget.image = pi

        offset_x = (bw - nw) / 2.0
        offset_y = (bh - nh) / 2.0
        if label_widget == self.lbl_orig_img:
            self.orig_display_info = (nw, nh, offset_x, offset_y)
        else:
            self.tuned_display_info = (nw, nh, offset_x, offset_y)

    def _on_quality_slider_change(self, val):
        q = int(val)
        self.lbl_q_val.configure(text=f"{q}%")
        self._update_preview()

    def _save_rules_to_folder(self):
        in_path = self.input_entry.get().strip()
        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("Error", "Please select a valid input folder first.")
            return

        success = save_tuning_config(in_path, self.global_tuning, self.file_tunings, self.page_tunings)
        if success:
            messagebox.showinfo("Saved", f"Successfully saved configuration options ({TUNING_FILENAME}) inside folder:\n{in_path}")


if __name__ == "__main__":
    app = S2PITApp()
    app.mainloop()
