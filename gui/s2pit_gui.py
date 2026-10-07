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
from core.tuning_config import load_tuning_config, save_tuning_config, get_effective_file_tuning, DEFAULT_GLOBAL_TUNING, TUNING_FILENAME
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
        self.geometry("1280x820")
        self.minsize(1100, 700)

        self.input_dir = initial_input_dir or self.config_data.get("last_input_dir", "")
        self.found_files = []
        self.file_items = {}
        self.item_to_filepath = {}
        self.selected_file_path = None
        self.selected_page_idx = 0
        self.page_images_cache = []

        # Current Tuning State
        self.global_tuning = dict(DEFAULT_GLOBAL_TUNING)
        self.file_tunings = {}

        self.var_color_mode = ctk.StringVar(value="color")
        self.var_dpi = ctk.StringVar(value="150 DPI")
        self.var_jpeg_quality = ctk.IntVar(value=60)
        self.var_scope = ctk.StringVar(value="batch") # "batch" or "file"

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

        # Main Split Layout: Left Panel (Files) & Right Panel (Preview & Tuning)
        main_paned = ctk.CTkFrame(self, fg_color="transparent")
        main_paned.pack(fill="both", expand=True, padx=10, pady=10)
        main_paned.grid_columnconfigure(0, weight=4) # Left Column
        main_paned.grid_columnconfigure(1, weight=6) # Right Column
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

        self.tree.column("Filename", width=180, anchor="w")
        self.tree.column("Format", width=70, anchor="center")
        self.tree.column("Size", width=65, anchor="e")
        self.tree.column("Pages", width=50, anchor="center")
        self.tree.column("ImgRes", width=65, anchor="center")

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
        self.thumb_bar = ctk.CTkScrollableFrame(right_frame, orientation="horizontal", height=70, fg_color="#1a1a1a")
        self.thumb_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))

        # 2. Central Split View & Preview Canvas
        view_container = ctk.CTkFrame(right_frame, fg_color="transparent")
        view_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        view_container.grid_columnconfigure(0, weight=1)
        view_container.grid_columnconfigure(1, weight=1)
        view_container.grid_rowconfigure(0, weight=1)

        # Left Original View Box
        v_orig_box = ctk.CTkFrame(view_container)
        v_orig_box.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        ctk.CTkLabel(v_orig_box, text="📷 ORIGINAL PAGE", font=ctk.CTkFont(size=12, weight="bold"), text_color="#7f8c8d").pack(pady=4)
        self.lbl_orig_img = ctk.CTkLabel(v_orig_box, text="No file selected")
        self.lbl_orig_img.pack(fill="both", expand=True, padx=5, pady=5)

        # Right Tuned Preview Box
        v_prev_box = ctk.CTkFrame(view_container)
        v_prev_box.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        ctk.CTkLabel(v_prev_box, text="⚡ TUNED PREVIEW RESULT", font=ctk.CTkFont(size=12, weight="bold"), text_color="#2ecc71").pack(pady=4)
        self.lbl_prev_img = ctk.CTkLabel(v_prev_box, text="No file selected")
        self.lbl_prev_img.pack(fill="both", expand=True, padx=5, pady=5)

        # Real-time Stats Badge
        self.lbl_size_stats = ctk.CTkLabel(
            right_frame,
            text="Original: - MB  |  Tuned Preview: - KB  (-0%)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#2ecc71"
        )
        self.lbl_size_stats.grid(row=2, column=0, sticky="ew", padx=10, pady=4)

        # 3. Tuning Controls Panel
        ctrl_panel = ctk.CTkFrame(right_frame)
        ctrl_panel.grid(row=3, column=0, sticky="ew", padx=10, pady=5)

        # Presets Row
        p_row = ctk.CTkFrame(ctrl_panel, fg_color="transparent")
        p_row.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(p_row, text="Presets:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 10))

        ctk.CTkButton(p_row, text=self._tr("preset_max"), width=120, fg_color="#34495e", command=lambda: self._apply_preset("max")).pack(side="left", padx=4)
        ctk.CTkButton(p_row, text=self._tr("preset_balanced"), width=110, fg_color="#2980b9", command=lambda: self._apply_preset("balanced")).pack(side="left", padx=4)
        ctk.CTkButton(p_row, text=self._tr("preset_slim"), width=130, fg_color="#27ae60", command=lambda: self._apply_preset("slim")).pack(side="left", padx=4)

        # Settings Controls Grid
        s_grid = ctk.CTkFrame(ctrl_panel, fg_color="transparent")
        s_grid.pack(fill="x", padx=10, pady=5)

        # Color Mode
        ctk.CTkLabel(s_grid, text=self._tr("color_mode_label"), font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w", padx=5)
        cm_frame = ctk.CTkFrame(s_grid, fg_color="transparent")
        cm_frame.grid(row=0, column=1, columnspan=3, sticky="w")

        ctk.CTkRadioButton(cm_frame, text=self._tr("color_mode_color"), variable=self.var_color_mode, value="color", command=self._update_preview).pack(side="left", padx=5)
        ctk.CTkRadioButton(cm_frame, text=self._tr("color_mode_grayscale"), variable=self.var_color_mode, value="grayscale", command=self._update_preview).pack(side="left", padx=5)
        ctk.CTkRadioButton(cm_frame, text=self._tr("color_mode_bw"), variable=self.var_color_mode, value="monochrome", command=self._update_preview).pack(side="left", padx=5)

        # Target DPI & JPEG Quality Slider
        ctk.CTkLabel(s_grid, text="Target DPI:", font=ctk.CTkFont(size=11, weight="bold")).grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.cmb_dpi = ctk.CTkOptionMenu(s_grid, values=["300 DPI", "200 DPI", "150 DPI", "120 DPI"], variable=self.var_dpi, command=lambda v: self._update_preview(), width=110)
        self.cmb_dpi.grid(row=1, column=1, sticky="w", padx=5, pady=5)

        ctk.CTkLabel(s_grid, text="JPEG Quality:", font=ctk.CTkFont(size=11, weight="bold")).grid(row=1, column=2, sticky="w", padx=(15, 5), pady=5)
        self.lbl_q_val = ctk.CTkLabel(s_grid, text=f"{self.var_jpeg_quality.get()}%", font=ctk.CTkFont(size=11, weight="bold"), text_color="#3498db")
        self.lbl_q_val.grid(row=1, column=3, sticky="w", padx=5, pady=5)

        self.sld_q = ctk.CTkSlider(s_grid, from_=30, to=95, number_of_steps=65, variable=self.var_jpeg_quality, command=self._on_quality_slider_change)
        self.sld_q.grid(row=1, column=4, sticky="ew", padx=10, pady=5)
        s_grid.grid_columnconfigure(4, weight=1)

        # Bottom Scope & Action Bar
        act_bar = ctk.CTkFrame(right_frame, fg_color="transparent")
        act_bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(5, 10))

        ctk.CTkRadioButton(act_bar, text=self._tr("scope_all_batch"), variable=self.var_scope, value="batch", command=self._on_scope_change).pack(side="left", padx=10)
        ctk.CTkRadioButton(act_bar, text=self._tr("scope_this_file"), variable=self.var_scope, value="file", command=self._on_scope_change).pack(side="left", padx=10)

        self.btn_launch = ctk.CTkButton(act_bar, text=self._tr("btn_launch_s2pot"), fg_color="#8e44ad", hover_color="#732d91", command=self._launch_s2pot)
        self.btn_launch.pack(side="right", padx=5)

        self.btn_save = ctk.CTkButton(act_bar, text=self._tr("btn_save_tuning"), fg_color="#27ae60", hover_color="#1e8449", command=self._save_rules_to_folder)
        self.btn_save.pack(side="right", padx=5)

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

        # Load existing s2pot_tuning.json if present
        loaded = load_tuning_config(in_path)
        if loaded:
            self.global_tuning = loaded.get("global_tuning", dict(DEFAULT_GLOBAL_TUNING))
            self.file_tunings = loaded.get("file_tunings", {})
        else:
            self.global_tuning = dict(DEFAULT_GLOBAL_TUNING)
            self.file_tunings = {}

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
        eff = get_effective_file_tuning({"global_tuning": self.global_tuning, "file_tunings": self.file_tunings}, filename)

        self.var_color_mode.set(eff.get("color_mode", "color"))
        self.var_dpi.set(f"{eff.get('target_dpi', 300)} DPI")
        self.var_jpeg_quality.set(eff.get("jpeg_quality", 70))
        self.lbl_q_val.configure(text=f"{eff.get('jpeg_quality', 70)}%")

        if filename and filename in self.file_tunings:
            self.var_scope.set("file")
        else:
            self.var_scope.set("batch")

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
        # Clear thumbnail bar
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

        # Update Tree Pages count
        if fpath in self.file_items:
            iid = self.file_items[fpath]
            self.tree.set(iid, "Pages", str(len(self.page_images_cache)))

        # Build Thumbnails Bar
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
        self._update_preview()

    def _update_preview(self):
        if not self.page_images_cache or self.selected_page_idx >= len(self.page_images_cache):
            return

        bgr_orig = self.page_images_cache[self.selected_page_idx]
        h_orig, w_orig = bgr_orig.shape[:2]

        # Read current tuning settings
        cmode = self.var_color_mode.get()
        target_dpi = int(self.var_dpi.get().replace("DPI", "").strip())
        jpeg_q = self.var_jpeg_quality.get()

        # Update active tuning state based on scope
        filename = os.path.basename(self.selected_file_path) if self.selected_file_path else None
        active_dict = {
            "color_mode": cmode,
            "target_dpi": target_dpi,
            "jpeg_quality": jpeg_q,
            "downsample_max_dim": 1500 if target_dpi <= 150 else 2000
        }

        if self.var_scope.get() == "file" and filename:
            self.file_tunings[filename] = active_dict
        else:
            self.global_tuning = dict(active_dict)
            if filename in self.file_tunings:
                del self.file_tunings[filename]

        # Apply Tuned Transformations
        bgr_tuned = apply_color_mode(bgr_orig.copy(), cmode)

        # Downscaling simulation
        max_dim = 1500 if target_dpi <= 150 else 2000
        if max(h_orig, w_orig) > max_dim:
            sc = float(max_dim) / float(max(h_orig, w_orig))
            bgr_tuned = cv2.resize(bgr_tuned, (0, 0), fx=sc, fy=sc, interpolation=cv2.INTER_AREA)

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

        # Display Images in UI (Scaled to fit box)
        self._display_image_on_label(bgr_orig, self.lbl_orig_img)
        self._display_image_on_label(bgr_tuned, self.lbl_prev_img)

    def _display_image_on_label(self, bgr_img, label_widget):
        h, w = bgr_img.shape[:2]
        bw, bh = label_widget.winfo_width() or 350, label_widget.winfo_height() or 400
        scale = min(float(bw) / float(w), float(bh) / float(h))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))

        res = cv2.resize(bgr_img, (nw, nh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(res, cv2.COLOR_BGR2RGB)
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))
        label_widget.configure(image=pi, text="")
        label_widget.image = pi

    def _on_quality_slider_change(self, val):
        q = int(val)
        self.lbl_q_val.configure(text=f"{q}%")
        self._update_preview()

    def _on_scope_change(self):
        self._update_preview()

    def _apply_preset(self, preset_key):
        if preset_key == "max":
            self.var_color_mode.set("color")
            self.var_dpi.set("300 DPI")
            self.var_jpeg_quality.set(90)
        elif preset_key == "balanced":
            self.var_color_mode.set("color")
            self.var_dpi.set("200 DPI")
            self.var_jpeg_quality.set(65)
        elif preset_key == "slim":
            self.var_color_mode.set("grayscale")
            self.var_dpi.set("150 DPI")
            self.var_jpeg_quality.set(45)
        self.lbl_q_val.configure(text=f"{self.var_jpeg_quality.get()}%")
        self._update_preview()

    def _save_rules_to_folder(self):
        in_path = self.input_entry.get().strip()
        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("Error", "Please select a valid input folder first.")
            return

        success = save_tuning_config(in_path, self.global_tuning, self.file_tunings)
        if success:
            messagebox.showinfo("Saved", f"Successfully saved tuning rules ({TUNING_FILENAME}) inside folder:\n{in_path}")

    def _launch_s2pot(self):
        self._save_rules_to_folder()
        in_path = self.input_entry.get().strip()

        # Launch S2POT process
        s2pot_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        if os.path.exists(s2pot_script):
            subprocess.Popen([sys.executable, s2pot_script])
            messagebox.showinfo("Launched", "Launched S2POT Batch Processing application with active tuning rules!")


if __name__ == "__main__":
    app = S2PITApp()
    app.mainloop()
