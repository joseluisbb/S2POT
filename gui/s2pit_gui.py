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
from core.i18n import get_text, LANGUAGES
from core.tuning_config import (
    load_tuning_config,
    save_tuning_config,
    get_effective_file_tuning,
    DEFAULT_GLOBAL_TUNING,
    TUNING_FILENAME
)
from core.pipeline import apply_color_mode
from gui.tooltip import ToolTip
from gui.file_browser import FilePageTreeBrowser, get_file_max_dpi, get_file_pages_metadata

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class S2PITPageTuningWindow(ctk.CTkToplevel):
    """
    Dedicated Page Tuning Studio & Lens Window for S2PIT.
    Provides side-by-side original vs tuned comparison, zoom lens inspection,
    and 3-line tuning controls with persistence to s2pot_tuning.json.
    """
    def __init__(self, master, file_path, page_idx=0, lang="pt_PT", on_save_callback=None):
        super().__init__(master)

        self.file_path = file_path
        self.filename = os.path.basename(file_path)
        self.folder_path = os.path.dirname(file_path)
        self.page_idx = page_idx
        self.total_pages = 1
        self.lang = lang
        self.on_save_callback = on_save_callback

        self.title(f"{self._tr('page_viewer_title')} — {self.filename} ({self._tr('tree_item_page').format(idx=self.page_idx + 1)})")
        self.geometry("1260x820")
        self.minsize(1020, 680)

        # Thumbnails state
        self.thumb_cards = {}
        self.thumb_images = {}
        self._current_thumb_path = None

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

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_studio_ui(self):
        # 1. Header Bar
        hdr = ctk.CTkFrame(self, height=45, fg_color="#1e1e1e", corner_radius=0)
        hdr.pack(side="top", fill="x")

        # Document / Page Navigator
        nav_box = ctk.CTkFrame(hdr, fg_color="transparent")
        nav_box.pack(side="left", padx=12, pady=6)

        self.btn_prev_page = ctk.CTkButton(
            nav_box,
            text="◀",
            width=32,
            height=28,
            command=self._prev_page
        )
        self.btn_prev_page.pack(side="left", padx=(0, 4))

        self.lbl_doc_info = ctk.CTkLabel(
            nav_box,
            text=f"📄 {self.filename}  |  Pág {self.page_idx + 1}",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_doc_info.pack(side="left", padx=6)

        self.btn_next_page = ctk.CTkButton(
            nav_box,
            text="▶",
            width=32,
            height=28,
            command=self._next_page
        )
        self.btn_next_page.pack(side="left", padx=(4, 0))

        # Lens Zoom Controls in Header
        lens_frame = ctk.CTkFrame(hdr, fg_color="transparent")
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
            variable=self.var_lens_zoom,
            command=self._on_lens_zoom_change
        )
        self.cmb_lens.pack(side="left")

        # 2. Main Central Split View Container (Thumbnails sidebar + 50%/50% previews)
        self.view_container = ctk.CTkFrame(self, fg_color="transparent")
        self.view_container.pack(fill="both", expand=True, padx=12, pady=6)
        self.view_container.grid_columnconfigure(0, weight=0, minsize=165)
        self.view_container.grid_columnconfigure(1, weight=1, uniform="preview_cols")
        self.view_container.grid_columnconfigure(2, weight=1, uniform="preview_cols")
        self.view_container.grid_rowconfigure(0, weight=1)
        self.view_container.bind("<Configure>", self._on_container_resize)

        # === COLUMN 0: THUMBNAILS SIDEBAR ===
        v_thumb_box = ctk.CTkFrame(self.view_container, width=165)
        v_thumb_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        v_thumb_box.grid_rowconfigure(1, weight=1)
        v_thumb_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            v_thumb_box,
            text=f"📑 {self._tr('sidebar_pages')}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#3498db"
        ).grid(row=0, column=0, pady=(6, 4))

        self.thumb_scroll = ctk.CTkScrollableFrame(v_thumb_box, width=155, fg_color="transparent")
        self.thumb_scroll.grid(row=1, column=0, sticky="nsew", padx=2, pady=(0, 4))

        # === COLUMN 1: ORIGINAL PAGE BOX ===
        v_orig_box = ctk.CTkFrame(self.view_container)
        v_orig_box.grid(row=0, column=1, sticky="nsew", padx=(0, 5))
        v_orig_box.grid_rowconfigure(1, weight=1)
        v_orig_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            v_orig_box,
            text="📷 ORIGINAL PAGE",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#7f8c8d"
        ).grid(row=0, column=0, pady=(6, 2))

        self.lbl_orig_img = ctk.CTkLabel(v_orig_box, text="Loading image...")
        self.lbl_orig_img.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        # Original Metadata Card (3 Lines)
        orig_card = ctk.CTkFrame(v_orig_box, fg_color=("gray90", "#242424"), corner_radius=6)
        orig_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))

        self.lbl_orig_meta_cmode = ctk.CTkLabel(orig_card, text="Color Mode: -", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_orig_meta_cmode.pack(anchor="w", padx=8, pady=(4, 1))

        self.lbl_orig_meta_dpi = ctk.CTkLabel(orig_card, text="Original DPI: -", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_orig_meta_dpi.pack(anchor="w", padx=8, pady=1)

        self.lbl_orig_meta_dims = ctk.CTkLabel(orig_card, text="Dimensions: -", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_orig_meta_dims.pack(anchor="w", padx=8, pady=(1, 4))

        # === COLUMN 2: TUNED RESULT PAGE BOX ===
        v_prev_box = ctk.CTkFrame(self.view_container)
        v_prev_box.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        v_prev_box.grid_rowconfigure(1, weight=1)
        v_prev_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            v_prev_box,
            text="⚡ TUNED PREVIEW RESULT",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#2ecc71"
        ).grid(row=0, column=0, pady=(6, 2))

        self.lbl_prev_img = ctk.CTkLabel(v_prev_box, text="Calculating preview...")
        self.lbl_prev_img.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        # Attach Lens Motion and Leave Event Handlers to both image labels
        for lbl_widget, ttype in [(self.lbl_orig_img, "orig"), (self.lbl_prev_img, "tuned")]:
            lbl_widget.bind("<Motion>", lambda e, t=ttype: self._on_lens_motion(e, t))
            lbl_widget.bind("<Leave>", lambda e: self._hide_lens())
            if hasattr(lbl_widget, "_label"):
                lbl_widget._label.bind("<Motion>", lambda e, t=ttype: self._on_lens_motion(e, t))
                lbl_widget._label.bind("<Leave>", lambda e: self._hide_lens())

        # 3-Line Vertical Tuning Controls Card
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
            command=self._on_color_dropdown_change
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

        # 3. Bottom Action & Persistence Bar
        bot_bar = ctk.CTkFrame(self, fg_color="#1e1e1e", height=45, corner_radius=0)
        bot_bar.pack(side="bottom", fill="x")

        # Real-time Stats Badge
        self.lbl_stats = ctk.CTkLabel(
            bot_bar,
            text="Est. Size: - KB (Redução: -%)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#2ecc71"
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
            font=ctk.CTkFont(weight="bold"),
            command=self._save_options_to_file
        )
        self.btn_save.pack(side="right", padx=6, pady=8)

        # Sync controls with active config
        self._sync_tuning_ui_from_state()

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
        self.var_dpi.set(f"{eff.get('target_dpi', 150)} DPI")
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
        ext = os.path.splitext(self.file_path)[1].lower()
        bgr = None
        self.total_pages = 1

        try:
            if ext == ".pdf":
                doc = fitz.open(self.file_path)
                self.total_pages = len(doc)
                if self.page_idx < len(doc):
                    page = doc[self.page_idx]
                    pix = page.get_pixmap(dpi=150)
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

    def _render_thumbnails_if_needed(self):
        if self._current_thumb_path == self.file_path and self.thumb_cards:
            return
        self._current_thumb_path = self.file_path

        # Clear previous cards
        for widget in self.thumb_scroll.winfo_children():
            widget.destroy()
        self.thumb_cards.clear()
        self.thumb_images.clear()

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
                    pix = doc[pidx].get_pixmap(dpi=36)
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
                pil_thumb = Image.new("RGB", (100, 130), color=(200, 200, 200))
            else:
                pil_thumb.thumbnail((120, 150))

            ctk_thumb = ctk.CTkImage(light_image=pil_thumb, dark_image=pil_thumb, size=(pil_thumb.width, pil_thumb.height))
            self.thumb_images[pidx] = ctk_thumb

            card = ctk.CTkFrame(
                self.thumb_scroll,
                corner_radius=6,
                border_width=1,
                border_color=("gray75", "#3a3a3a"),
                fg_color=("gray95", "#252525"),
                cursor="hand2"
            )
            card.pack(fill="x", padx=4, pady=3)

            lbl_img = ctk.CTkLabel(card, image=ctk_thumb, text="", cursor="hand2")
            lbl_img.pack(padx=4, pady=(4, 2))

            lbl_txt = ctk.CTkLabel(
                card,
                text=f"{self._tr('tree_item_page').format(idx=pidx + 1)}",
                font=ctk.CTkFont(size=10, weight="bold"),
                cursor="hand2"
            )
            lbl_txt.pack(padx=4, pady=(0, 4))

            for w in [card, lbl_img, lbl_txt]:
                w.bind("<Button-1>", lambda e, p=pidx: self._go_to_page(p))
                if hasattr(w, "_label"):
                    w._label.bind("<Button-1>", lambda e, p=pidx: self._go_to_page(p))

            self.thumb_cards[pidx] = card

        if doc is not None:
            doc.close()
        if tiff is not None:
            tiff.close()

    def _on_container_resize(self, event):
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

    def _on_lens_motion(self, event, target_type):
        zoom_str = self.var_lens_zoom.get()
        if zoom_str == "Off":
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

        # Track cursor relative to the image using absolute screen coordinates
        # to ensure flawless tracking across the entire image including the top-left corner (0, 0)
        img_root_x = inner_widget.winfo_rootx()
        img_root_y = inner_widget.winfo_rooty()
        img_disp_w = inner_widget.winfo_width() or nw
        img_disp_h = inner_widget.winfo_height() or nh

        cursor_x_in_img = float(event.x_root - img_root_x)
        cursor_y_in_img = float(event.y_root - img_root_y)

        if cursor_x_in_img < 0 or cursor_x_in_img > img_disp_w or cursor_y_in_img < 0 or cursor_y_in_img > img_disp_h:
            self._hide_lens()
            return

        norm_x = max(0.0, min(1.0, cursor_x_in_img / float(img_disp_w))) if img_disp_w > 0 else 0.0
        norm_y = max(0.0, min(1.0, cursor_y_in_img / float(img_disp_h))) if img_disp_h > 0 else 0.0

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

        lens_size = 220
        zoomed = cv2.resize(patch, (lens_size, lens_size), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(zoomed, cv2.COLOR_BGR2RGB)
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))

        root_x = event.x_root + 5
        root_y = event.y_root + 5

        # Prevent lens popover from escaping the screen boundary
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        if root_x + lens_size > screen_w:
            root_x = max(0, event.x_root - lens_size - 5)
        if root_y + lens_size > screen_h:
            root_y = max(0, event.y_root - lens_size - 5)

        if self.lens_popover is None or not self.lens_popover.winfo_exists():
            self.lens_popover = tk.Toplevel(self)
            self.lens_popover.overrideredirect(True)
            self.lens_popover.attributes("-topmost", True)
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.lens_popover.winfo_id()) or self.lens_popover.winfo_id()
                GWL_EXSTYLE = -20
                WS_EX_TRANSPARENT = 0x00000020
                WS_EX_LAYERED = 0x00080000
                curr_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, curr_style | WS_EX_TRANSPARENT | WS_EX_LAYERED)
            except Exception:
                pass
            self.lbl_lens_img = tk.Label(self.lens_popover, bd=2, relief="solid", bg="#f1c40f")
            self.lbl_lens_img.pack()

        self.lens_popover.geometry(f"{lens_size}x{lens_size}+{root_x}+{root_y}")
        self.lbl_lens_img.configure(image=pi)
        self.lbl_lens_img.image = pi

    def _hide_lens(self):
        if self.lens_popover is not None and self.lens_popover.winfo_exists():
            self.lens_popover.destroy()
            self.lens_popover = None

    def _update_preview(self):
        if self.current_orig_bgr is None:
            return

        bgr_orig = self.current_orig_bgr
        h_orig, w_orig = bgr_orig.shape[:2]

        cmode = self.var_color_mode.get()
        target_dpi = int(self.var_dpi.get().replace("DPI", "").strip())
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

        # Process tuned image preview
        bgr_tuned = apply_color_mode(bgr_orig, cmode)

        orig_dpi = get_file_max_dpi(self.file_path)
        if target_dpi < orig_dpi and orig_dpi > 0:
            scale = float(target_dpi) / float(orig_dpi)
            new_w = max(1, int(w_orig * scale))
            new_h = max(1, int(h_orig * scale))
            bgr_tuned = cv2.resize(bgr_tuned, (new_w, new_h), interpolation=cv2.INTER_AREA)

        self.current_tuned_bgr = bgr_tuned

        # Estimate file size reduction
        orig_bytes = w_orig * h_orig * 3
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_q]
        _, encimg = cv2.imencode(".jpg", bgr_tuned, encode_param)
        tuned_bytes = len(encimg)

        orig_kb = orig_bytes / 1024.0
        tuned_kb = tuned_bytes / 1024.0
        red_pct = max(0.0, (1.0 - (tuned_kb / orig_kb)) * 100.0) if orig_kb > 0 else 0.0

        self.lbl_stats.configure(
            text=f"Est. Size: ~{tuned_kb:.1f} KB  |  Redução: ~{red_pct:.1f}% vs Raw",
            text_color="#2ecc71" if red_pct > 50 else "#f39c12"
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
        pi = ImageTk.PhotoImage(Image.fromarray(rgb))

        label_widget.configure(image=pi, text="")
        label_widget.image = pi

        offset_x = (bw - nw) / 2.0
        offset_y = (bh - nh) / 2.0
        if label_widget == self.lbl_orig_img:
            self.orig_display_info = (nw, nh, offset_x, offset_y)
        else:
            self.tuned_display_info = (nw, nh, offset_x, offset_y)

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
        hdr_frame = ctk.CTkFrame(self, height=48, fg_color="#1e1e1e", corner_radius=0)
        hdr_frame.pack(side="top", fill="x")

        self.lbl_title = ctk.CTkLabel(
            hdr_frame,
            text=f"S2PIT — Smart Slim Preview Tool v{APP_VERSION}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_title.pack(side="left", padx=15, pady=10)

        # Action Buttons in Header
        act_frame = ctk.CTkFrame(hdr_frame, fg_color="transparent")
        act_frame.pack(side="right", padx=15, pady=8)

        self.btn_inspect = ctk.CTkButton(
            act_frame,
            text=self._tr("btn_inspect"),
            font=ctk.CTkFont(weight="bold"),
            fg_color="#2980b9",
            hover_color="#1f618d",
            command=self._on_inspect_clicked
        )
        self.btn_inspect.pack(side="left", padx=(0, 10))

        self.btn_tune = ctk.CTkButton(
            act_frame,
            text=self._tr("btn_tune_selected_page"),
            font=ctk.CTkFont(weight="bold"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            command=self._open_tuning_studio
        )
        self.btn_tune.pack(side="left", padx=(0, 10))

        # Language dropdown
        ctk.CTkLabel(act_frame, text=self._tr("language"), font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        self.cmb_lang = ctk.CTkOptionMenu(
            act_frame,
            values=list(LANGUAGES.values()),
            width=110,
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
            columns=("Format", "Size", "Pages", "ImgRes", "Tuning"),
            on_page_activated=self._on_page_activated,
            on_selection_changed=self._on_selection_changed
        )
        self.browser.pack(fill="both", expand=True, padx=12, pady=10)

        # Status Bar
        self.status_bar = ctk.CTkFrame(self, height=28, fg_color="#1e1e1e", corner_radius=0)
        self.status_bar.pack(side="bottom", fill="x")

        self.lbl_status = ctk.CTkLabel(
            self.status_bar,
            text="Faça duplo clique numa página ou clique em 'Afinar Página' para abrir o estúdio de calibração.",
            font=ctk.CTkFont(size=11),
            text_color="gray70"
        )
        self.lbl_status.pack(side="left", padx=12, pady=4)

    def _on_selection_changed(self, selected_files, selected_pages):
        count = len(selected_files) + len(selected_pages)
        if selected_pages:
            fpath, pidx = selected_pages[0]
            fname = os.path.basename(fpath)
            self.lbl_status.configure(text=f"Página selecionada: {fname} (Pág {pidx + 1}). Clique em 'Afinar Página' para calibrar.")
        elif selected_files:
            fname = os.path.basename(selected_files[0])
            self.lbl_status.configure(text=f"Ficheiro selecionado: {fname}. Expanda para ver as páginas ou afine diretamente.")

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
