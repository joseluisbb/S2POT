import os
import glob
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog
import customtkinter as ctk
import pymupdf as fitz
from PIL import Image

from core.i18n import get_text
from core.tuning_config import load_tuning_config, get_effective_file_tuning, TUNING_FILENAME
from core.metadata_config import load_folder_metadata, METADATA_FILENAME, estimate_text_layer_confidence
from gui.inspection_dialog import InspectionProgressDialog


DOC_SUBFOLDER_NAMES = {"pdf-compressed", "pdf-originalimages", "docx"}
DATA_SUBFOLDER_NAMES = {"text", "hocr", "word_coordinates", "audit_reports", "txt from pdf", "docx from pdf"}
KNOWN_RESULT_SUFFIXES = ("_compressed", "_original", "_words", "_from_pdf")


def get_file_max_dpi(fpath):
    """Calculates max DPI of a file (PDF, TIFF, or image)."""
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
                        except Exception:
                            pass
                else:
                    pix = page.get_pixmap()
                    if pt_w > 0 and pt_h > 0:
                        max_dpi = max(max_dpi, (pix.width * 72.0) / pt_w, (pix.height * 72.0) / pt_h)
            doc.close()
        elif ext in [".tif", ".tiff"]:
            with Image.open(fpath) as tiff:
                dpi = tiff.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    max_dpi = max(float(dpi[0]), float(dpi[1]))
        elif ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
            with Image.open(fpath) as img:
                dpi = img.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    max_dpi = max(float(dpi[0]), float(dpi[1]))
    except Exception:
        pass
    return int(round(max_dpi)) if max_dpi > 0 else 300


def get_file_pages_metadata(fpath):
    """
    Returns list of dicts with per-page details:
    [{'page_idx': 0, 'width': 1240, 'height': 1754, 'dpi': 300}, ...]
    """
    ext = os.path.splitext(fpath)[1].lower()
    pages_meta = []
    try:
        if ext == ".pdf":
            with fitz.open(fpath) as doc:
                for idx, page in enumerate(doc):
                    pt_w, pt_h = page.rect.width, page.rect.height
                    page_dpi = 300
                    cmode = "color"
                    page_chars = 0
                    page_conf = None
                    try:
                        txt = page.get_text("text").strip()
                        w_list = page.get_text("words")
                        if txt:
                            page_chars = len(txt)
                            page_conf = estimate_text_layer_confidence(w_list, txt)
                        else:
                            page_conf = 0.0
                    except Exception:
                        pass
                    image_list = page.get_images()
                    if image_list:
                        for img in image_list:
                            try:
                                base_img = doc.extract_image(img[0])
                                w, h = base_img.get("width", 0), base_img.get("height", 0)
                                cs = base_img.get("colorspace", 3)
                                if cs == 1:
                                    cmode = "grayscale"
                                if pt_w > 0 and pt_h > 0 and w > 0 and h > 0:
                                    page_dpi = max(page_dpi, int(round((w * 72.0) / pt_w)))
                            except Exception:
                                pass
                    else:
                        pix = page.get_pixmap()
                        if pt_w > 0 and pt_h > 0:
                            page_dpi = max(page_dpi, int(round((pix.width * 72.0) / pt_w)))
                        if pix.n == 1 or pix.colorspace == fitz.csGRAY:
                            cmode = "grayscale"

                    pages_meta.append({
                        "page_idx": idx,
                        "width": int(round(pt_w * (page_dpi / 72.0))),
                        "height": int(round(pt_h * (page_dpi / 72.0))),
                        "dpi": page_dpi,
                        "color_mode": cmode,
                        "chars": page_chars,
                        "confidence": page_conf
                    })
        elif ext in [".tif", ".tiff"]:
            with Image.open(fpath) as tiff:
                n_frames = getattr(tiff, "n_frames", 1)
                dpi_val = 300
                dpi = tiff.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    dpi_val = int(round(max(float(dpi[0]), float(dpi[1]))))
                for i in range(n_frames):
                    tiff.seek(i)
                    w, h = tiff.size
                    cmode = "grayscale" if tiff.mode in ["L", "1"] else "color"
                    pages_meta.append({
                        "page_idx": i,
                        "width": w,
                        "height": h,
                        "dpi": dpi_val,
                        "color_mode": cmode
                    })
        else:
            with Image.open(fpath) as img:
                w, h = img.size
                dpi_val = 300
                dpi = img.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    dpi_val = int(round(max(float(dpi[0]), float(dpi[1]))))
                cmode = "grayscale" if img.mode in ["L", "1"] else "color"
                pages_meta.append({
                    "page_idx": 0,
                    "width": w,
                    "height": h,
                    "dpi": dpi_val,
                    "color_mode": cmode
                })
    except Exception as e:
        print(f"Error extracting page metadata for {fpath}: {e}")
    return pages_meta


class FilePageTreeBrowser(ctk.CTkFrame):
    """
    Reusable File System & Page Expansion Tree Browser widget.
    Used by both S2PIT (Preview/Tuning) and S2POT (Results Explorer).
    Supports expandable folders, live metadata synchronization with the main table,
    and full per-file and per-page inspection details.
    """
    def __init__(
        self,
        master,
        initial_dir="",
        lang="pt_PT",
        selectmode="browse",
        show_path_bar=True,
        show_filters=False,
        columns=None,
        column_widths=None,
        browser_mode="input",
        metadata_provider=None,
        on_file_selected=None,
        on_page_selected=None,
        on_page_activated=None,
        on_selection_changed=None,
        defer_initial_load=False,
        **kwargs
    ):
        super().__init__(master, **kwargs)

        self.current_dir = initial_dir or ""
        self.root_dir = initial_dir or ""
        self.lang = lang
        self.selectmode = selectmode
        self.show_path_bar = show_path_bar
        self.show_filters = show_filters
        self.browser_mode = browser_mode  # "input", "results_docs", "results_data"
        self.metadata_provider = metadata_provider

        self.on_file_selected = on_file_selected
        self.on_page_selected = on_page_selected
        self.on_page_activated = on_page_activated
        self.on_selection_changed = on_selection_changed

        self.found_files = []
        self.file_to_item = {}
        self.item_to_info = {}  # item_id -> dict with type ('folder'|'file'|'page'), path, etc.
        self.loaded_pages = set()

        # Active tuning and metadata configs for folder
        self.tuning_config = None
        self.metadata_config = None
        self._live_index = {}
        self._meta_index = {}
        self._refresh_job = None
        self._pending_tasks = []
        self._total_tasks = 0

        # Columns definition
        self.columns = columns or ("Format", "PagesChars", "Size", "ImgRes", "Status", "DateModified")
        self.column_widths = column_widths or {
            "Format": 110,
            "PagesChars": 170,
            "Size": 175,
            "Pages": 80,
            "ImgRes": 215,
            "Chars": 95,
            "Status": 130,
            "Time": 75,
            "Tuning": 140,
            "DateModified": 145
        }

        # Sorting state
        self.sort_column = None
        self.sort_reverse = False

        self._build_browser_ui()

        if self.current_dir and os.path.exists(self.current_dir) and not defer_initial_load:
            self.set_folder(self.current_dir, set_as_root=True)

    def _tr(self, key):
        return get_text(key, self.lang)

    def _get_file_conf_badge_and_tag(self, fconf, min_pconf=None, low_pages=0, total_chars=None, status_val="", f_idx=0, is_virtual_doc=False):
        default_tag = "virtual_doc_row" if is_virtual_doc else ("even" if f_idx % 2 == 0 else "odd")
        if fconf is None:
            return None, default_tag

        is_critical = (
            fconf < 65.0
            or (min_pconf is not None and min_pconf < 65.0)
            or (status_val == "done" and total_chars is not None and total_chars == 0)
        )
        is_warn = (
            not is_critical
            and (fconf < 80.0 or (min_pconf is not None and min_pconf < 80.0) or (low_pages or 0) > 0)
        )

        if is_critical:
            return f"🔴 {fconf:.0f}%", ("virtual_doc_low" if is_virtual_doc else "file_low_conf")
        elif is_warn:
            return f"⚠️ {fconf:.0f}%", ("virtual_doc_warn" if is_virtual_doc else "file_warn_conf")
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

    def _format_color_mode(self, cmode):
        if not cmode:
            return ""
        cmode_lower = str(cmode).lower()
        if "gray" in cmode_lower or "cinz" in cmode_lower:
            return self._tr("color_short_gray")
        elif "mono" in cmode_lower or "bw" in cmode_lower or "pb" in cmode_lower or "black" in cmode_lower:
            return self._tr("color_short_bw")
        return self._tr("color_short_color")

    def _get_col_titles(self):
        return {
            "Format": self._tr("col_format"),
            "PagesChars": self._tr("col_pages_chars"),
            "Size": self._tr("col_size"),
            "Pages": self._tr("col_pages"),
            "ImgRes": self._tr("col_img_res"),
            "Chars": self._tr("col_chars"),
            "Status": self._tr("col_status"),
            "Time": self._tr("col_time"),
            "Tuning": "Tuning / Calibração",
            "DateModified": self._tr("col_date_modified")
        }

    def _build_browser_ui(self):
        self.grid_columnconfigure(0, weight=1)

        row_idx = 0

        # Optional Top Path Bar
        if self.show_path_bar:
            bar_frame = ctk.CTkFrame(self, fg_color="transparent")
            bar_frame.grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(6, 4))
            row_idx += 1

            self.lbl_path = ctk.CTkLabel(
                bar_frame,
                text=self._tr("folder_label"),
                font=ctk.CTkFont(size=12, weight="bold")
            )
            self.lbl_path.pack(side="left", padx=(0, 6))

            self.path_entry = ctk.CTkEntry(
                bar_frame,
                placeholder_text=self._tr("input_placeholder"),
                font=ctk.CTkFont(size=12),
                height=28
            )
            self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
            self.path_entry.bind("<Return>", lambda e: self.set_folder(self.path_entry.get().strip()))

            self.btn_up = ctk.CTkButton(
                bar_frame,
                text=self._tr("btn_up_folder"),
                width=75,
                height=28,
                fg_color="#34495e",
                hover_color="#2c3e50",
                font=ctk.CTkFont(size=12, weight="bold"),
                command=self._go_up_folder
            )
            self.btn_up.pack(side="left", padx=(0, 6))

            self.btn_browse = ctk.CTkButton(
                bar_frame,
                text=self._tr("browse"),
                width=85,
                height=28,
                font=ctk.CTkFont(size=12, weight="bold"),
                command=self._browse_folder
            )
            self.btn_browse.pack(side="left", padx=(0, 6))

            self.btn_refresh = ctk.CTkButton(
                bar_frame,
                text=f"⟳ {self._tr('refresh')}",
                width=95,
                height=28,
                fg_color="#2980b9",
                hover_color="#1f618d",
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda: self.refresh(progressive=True)
            )
            self.btn_refresh.pack(side="left")

        # Optional Filters Row
        if self.show_filters:
            filt_frame = ctk.CTkFrame(self, fg_color="transparent")
            filt_frame.grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(0, 4))
            row_idx += 1

            self.var_f_pdf = ctk.BooleanVar(value=True)
            self.var_f_tiff = ctk.BooleanVar(value=True)
            self.var_f_jpg = ctk.BooleanVar(value=True)
            self.var_f_png = ctk.BooleanVar(value=True)

            for txt, var in [("PDF", self.var_f_pdf), ("TIFF", self.var_f_tiff), ("JPG", self.var_f_jpg), ("PNG", self.var_f_png)]:
                ctk.CTkCheckBox(
                    filt_frame,
                    text=txt,
                    variable=var,
                    width=65,
                    font=ctk.CTkFont(size=12),
                    command=lambda: self.refresh(progressive=True)
                ).pack(side="left", padx=4)

        # Progressive Loading Status Strip (shown while streaming large directories)
        self.loading_frame = ctk.CTkFrame(self, fg_color="transparent", height=24)
        self.loading_frame.grid(row=row_idx, column=0, sticky="ew", padx=10, pady=(0, 2))
        row_idx += 1

        self.lbl_loading = ctk.CTkLabel(
            self.loading_frame,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#3498db"
        )
        self.lbl_loading.pack(side="left", padx=(0, 10))

        self.loading_bar = ctk.CTkProgressBar(self.loading_frame, height=10)
        self.loading_bar.set(0.0)
        self.loading_bar.pack(side="left", fill="x", expand=True)
        self.loading_frame.grid_remove()

        # Treeview Container
        tree_container = ctk.CTkFrame(self, fg_color="transparent")
        tree_container.grid(row=row_idx, column=0, sticky="nsew", padx=8, pady=4)
        self.grid_rowconfigure(row_idx, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)
        tree_container.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Browser.Treeview",
            background="#ffffff",
            foreground="#1a1a1a",
            fieldbackground="#ffffff",
            rowheight=26,
            font=("Segoe UI", 10),
            borderwidth=1,
            relief="solid",
            bordercolor="#d0d0d0",
            lightcolor="#d0d0d0",
            darkcolor="#d0d0d0"
        )
        style.configure(
            "Browser.Treeview.Heading",
            background="#f2f4f7",
            foreground="#1a1a1a",
            relief="solid",
            borderwidth=1,
            bordercolor="#d0d0d0",
            font=("Segoe UI", 10, "bold")
        )
        style.map("Browser.Treeview", background=[("selected", "#2980b9")], foreground=[("selected", "#ffffff")])
        style.map("Browser.Treeview.Heading", background=[("active", "#e2e6ea")])

        self.tree = ttk.Treeview(
            tree_container,
            columns=self.columns,
            show="tree headings",
            selectmode=self.selectmode,
            style="Browser.Treeview"
        )

        self.tree.heading("#0", text=self._tr("col_filename"), anchor="w", command=lambda: self._sort_by_column("#0"))
        self.tree.column("#0", width=310, minwidth=190, anchor="w")

        col_names = self._get_col_titles()
        for col in self.columns:
            header_text = col_names.get(col, col)
            self.tree.heading(col, text=header_text, command=lambda c=col: self._sort_by_column(c))
            w = self.column_widths.get(col, 100)
            anchor = "center"
            if col in ["Tuning"]:
                anchor = "w"
            self.tree.column(col, width=w, anchor=anchor)

        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Alternating row tags matching main window + OCR confidence warning/alert tags
        self.tree.tag_configure("folder_row", background="#eaf2f8", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("virtual_doc_row", background="#e8f6f3", foreground="#0e6251", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("virtual_doc_warn", background="#fef5d1", foreground="#9c6500", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("virtual_doc_low", background="#fadbd8", foreground="#922b21", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f8f9fa")
        self.tree.tag_configure("page_even", background="#f0f4f8")
        self.tree.tag_configure("page_odd", background="#e7eff7")
        self.tree.tag_configure("file_warn_conf", background="#fff8db", foreground="#9c6500")
        self.tree.tag_configure("file_low_conf", background="#fde8e8", foreground="#b91c1c")
        self.tree.tag_configure("page_warn_conf", background="#fff3cd", foreground="#856404")
        self.tree.tag_configure("page_low_conf", background="#fadbd8", foreground="#922b21")

        self._setup_gridlines()

        # Events
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Return>", self._on_tree_double_click)

    def _setup_gridlines(self):
        self._grid_dividers = []
        all_cols = ["#0"] + list(self.columns)[:-1]
        for _ in all_cols:
            div = tk.Frame(self.tree, width=1, bg="#dcdcdc")
            self._grid_dividers.append(div)

        def _update_dividers(event=None):
            try:
                tot = sum(self.tree.column(c, "width") for c in ["#0"] + list(self.columns))
                off = int(self.tree.xview()[0] * tot)
                cx = 0
                for i, c in enumerate(all_cols):
                    cx += self.tree.column(c, "width")
                    px = cx - off
                    div = self._grid_dividers[i]
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

    def _go_up_folder(self):
        if not self.current_dir:
            return
        parent_dir = os.path.dirname(os.path.abspath(self.current_dir))
        if parent_dir and os.path.exists(parent_dir) and parent_dir != os.path.abspath(self.current_dir):
            self.set_folder(parent_dir)

    def _browse_folder(self):
        f = filedialog.askdirectory(initialdir=self.current_dir or os.getcwd())
        if f:
            self.set_folder(f)

    def set_folder(self, folder_path, set_as_root=False, progressive=False):
        if not folder_path or not os.path.exists(folder_path):
            return
        self.current_dir = os.path.abspath(folder_path)
        if set_as_root or not self.root_dir:
            self.root_dir = self.current_dir
        if hasattr(self, "path_entry"):
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, self.current_dir)
        self.refresh(progressive=progressive)

    def _strip_result_suffix(self, stem):
        res = stem
        changed = True
        while changed:
            changed = False
            low = res.lower()
            for sfx in KNOWN_RESULT_SUFFIXES:
                if low.endswith(sfx) and len(res) > len(sfx):
                    res = res[:-len(sfx)]
                    changed = True
                    break
        return res

    def _get_shared_context(self):
        ctx = {
            "input_dir": "",
            "folder_metadata": None,
            "folder_tuning": None,
            "live_file_infos": {},
            "input_files_norm": set(),
            "ui_dpi": 300
        }
        if callable(self.metadata_provider):
            try:
                provided = self.metadata_provider() or {}
                ctx.update(provided)
            except Exception:
                pass
        return ctx

    def _build_metadata_indices(self, meta_files, live_file_infos):
        """Builds O(1) lookup dictionaries by filename and stem for fast resolution."""
        live_idx = {}
        if live_file_infos:
            def _live_score(x):
                return (
                    1 if x.get("status") == "done" else 0,
                    1 if x.get("comp_size_mb") is not None else 0,
                    1 if x.get("total_chars") is not None else 0,
                    1 if x.get("orig_dpi") is not None else 0
                )
            for k_path, finfo in live_file_infos.items():
                if not isinstance(finfo, dict) or finfo.get("type") != "file":
                    continue
                k_name = (finfo.get("filename") or os.path.basename(k_path)).lower()
                k_stem = os.path.splitext(k_name)[0]
                for key in (k_name, k_stem):
                    prev = live_idx.get(key)
                    if prev is None or _live_score(finfo) > _live_score(prev):
                        live_idx[key] = finfo

        meta_idx = {}
        if meta_files:
            def _meta_score(x):
                return (
                    1 if x.get("status") == "done" else 0,
                    1 if x.get("comp_size_mb") is not None else 0,
                    1 if (x.get("total_chars") or 0) > 0 else 0
                )
            for k_name, m_dict in meta_files.items():
                if not isinstance(m_dict, dict):
                    continue
                if "filename" not in m_dict:
                    m_dict["filename"] = k_name
                k_low = k_name.lower()
                k_stem = os.path.splitext(k_low)[0]
                for key in (k_low, k_stem):
                    prev = meta_idx.get(key)
                    if prev is None or _meta_score(m_dict) > _meta_score(prev):
                        meta_idx[key] = m_dict

        self._live_index = live_idx
        self._meta_index = meta_idx

    def _resolve_source_metadata(self, fpath, meta_files=None, live_file_infos=None, tuning_cfg=None, ui_dpi=300):
        """
        Matches a file (either an input file or a generated result file like doc_compressed.pdf,
        doc.docx, doc.txt, doc_words.xlsx) to the latest live metadata from the main table / s2pot_metadata.json.
        Can also be called with (fpath, ctx_dict) for convenience.
        """
        if isinstance(meta_files, dict) and "input_dir" in meta_files and live_file_infos is None:
            ctx = meta_files
            live_file_infos = ctx.get("live_file_infos") or {}
            folder_meta = ctx.get("folder_metadata") or {}
            meta_files = folder_meta.get("files", {}) if isinstance(folder_meta, dict) else {}
            tuning_cfg = ctx.get("folder_tuning")
            ui_dpi = ctx.get("ui_dpi", 300)
            self._build_metadata_indices(meta_files, live_file_infos)
            _, _, src_filename, eff_tune = self._resolve_source_metadata(
                fpath, meta_files, live_file_infos, tuning_cfg, ui_dpi=ui_dpi
            )
            return src_filename, eff_tune

        fname = os.path.basename(fpath)
        fname_low = fname.lower()
        stem = os.path.splitext(fname)[0]
        stem_low = stem.lower()
        base_stem_low = self._strip_result_suffix(stem).lower()

        if not self._live_index and live_file_infos:
            self._build_metadata_indices(meta_files or {}, live_file_infos)
        elif not self._meta_index and meta_files:
            self._build_metadata_indices(meta_files, live_file_infos or {})

        matched_live = (
            self._live_index.get(fname_low)
            or self._live_index.get(base_stem_low)
            or self._live_index.get(stem_low)
        )
        matched_meta = (
            (meta_files.get(fname) if meta_files else None)
            or self._meta_index.get(fname_low)
            or self._meta_index.get(base_stem_low)
            or self._meta_index.get(stem_low)
        )

        src_filename = (
            (matched_live.get("filename") if matched_live else None)
            or (matched_meta.get("filename") if matched_meta else None)
            or fname
        )
        eff_tune = get_effective_file_tuning(tuning_cfg, src_filename, page_idx=None, default_dpi=ui_dpi)

        return matched_live, matched_meta, src_filename, eff_tune

    def _build_file_info_and_values(self, fpath, meta_files, live_file_infos, tuning_cfg, ui_dpi=300, stat_size=None, stat_mtime=None, rel_folder_label="", enriched_meta=None):
        fname = os.path.basename(fpath)
        ext = os.path.splitext(fpath)[1].upper().replace(".", "")
        low_ext = os.path.splitext(fpath)[1].lower()
        low_fname = fname.lower()

        if self.browser_mode == "results_by_doc":
            if rel_folder_label.lower() in ("txt from pdf", "docx from pdf") or low_fname.endswith("_from_pdf.txt") or low_fname.endswith("_from_pdf.docx"):
                fmt_display = f"{ext} (PDF)"
            elif low_fname.endswith("_compressed.pdf"):
                fmt_display = "PDF (Comp.)"
            elif low_fname.endswith("_original.pdf"):
                fmt_display = "PDF (Orig.)"
            elif low_fname.endswith("_words.json"):
                fmt_display = "JSON (Words)"
            elif low_fname.endswith("_words.csv"):
                fmt_display = "CSV (Words)"
            elif low_fname.endswith("_words.xlsx"):
                fmt_display = "XLSX (Words)"
            else:
                fmt_display = ext
        else:
            fmt_display = ext

        try:
            sz_bytes = stat_size if stat_size is not None else os.path.getsize(fpath)
            actual_size_mb = sz_bytes / (1024 * 1024)
        except Exception:
            actual_size_mb = 0.0

        try:
            mtime = stat_mtime if stat_mtime is not None else os.path.getmtime(fpath)
            date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = 0.0
            date_str = "-"

        matched_live, matched_meta, src_filename, eff_tune = self._resolve_source_metadata(
            fpath, meta_files, live_file_infos, tuning_cfg, ui_dpi=ui_dpi
        )
        enriched_meta = enriched_meta or {}

        # Determine n_pages (only open PDF/TIFF on disk if not already known from metadata!)
        n_pages = None
        if matched_live and matched_live.get("n_pages"):
            n_pages = matched_live.get("n_pages")
        elif matched_meta and matched_meta.get("n_pages"):
            n_pages = matched_meta.get("n_pages")
        elif enriched_meta.get("n_pages"):
            n_pages = enriched_meta.get("n_pages")

        if n_pages is None:
            if low_ext == ".pdf":
                try:
                    with fitz.open(fpath) as doc:
                        n_pages = len(doc)
                except Exception:
                    n_pages = 1
            elif low_ext in [".tif", ".tiff"]:
                try:
                    with Image.open(fpath) as timg:
                        n_pages = getattr(timg, "n_frames", 1)
                except Exception:
                    n_pages = 1
            else:
                n_pages = 1

        # Determine total_chars
        total_chars = None
        if matched_live and matched_live.get("total_chars") is not None:
            total_chars = matched_live.get("total_chars")
        elif matched_meta and matched_meta.get("total_chars") is not None:
            total_chars = matched_meta.get("total_chars")
        elif enriched_meta.get("total_chars") is not None:
            total_chars = enriched_meta.get("total_chars")
        elif low_ext == ".txt":
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as tf:
                    total_chars = len(tf.read().strip())
            except Exception:
                pass

        # Determine sizes (Before / After)
        orig_size_mb = None
        if matched_live and matched_live.get("size_mb"):
            orig_size_mb = matched_live.get("size_mb")
        elif matched_meta and matched_meta.get("size_mb"):
            orig_size_mb = matched_meta.get("size_mb")
        elif enriched_meta.get("orig_size_mb"):
            orig_size_mb = enriched_meta.get("orig_size_mb")

        is_audit_report = low_fname.startswith("batch_processing_report") or rel_folder_label.lower() == "audit_reports"

        def _fmt_sz(mb_val, raw_bytes=None):
            if raw_bytes is not None and 0 < raw_bytes < 51200 and self.browser_mode == "results_by_doc":
                return f"{max(0.1, raw_bytes / 1024.0):.1f} KB"
            return f"{max(0.01, mb_val):.2f} MB" if (mb_val is not None and mb_val > 0) else "-"

        if is_audit_report:
            size_str = _fmt_sz(actual_size_mb, sz_bytes)
        elif self.browser_mode == "results_by_doc":
            # In Virtual Document mode, the parent virtual folder row already displays Before / After PDF size;
            # on each child row, show Before / After for PDFs and the specific generated file size for other formats.
            actual_sz_str = _fmt_sz(actual_size_mb, sz_bytes)
            if low_ext == ".pdf" and orig_size_mb and orig_size_mb > 0:
                size_str = f"{max(0.01, orig_size_mb):.2f} MB / {actual_sz_str}"
            else:
                size_str = actual_sz_str
        elif self.browser_mode in ("results_docs", "results_data"):
            if orig_size_mb and orig_size_mb > 0 and os.path.normcase(os.path.abspath(fpath)) != os.path.normcase(os.path.abspath(matched_live.get("filepath", "") if matched_live else "")):
                orig_sz_str = f"{max(0.01, orig_size_mb):.2f} MB"
                comp_sz_str = f"{max(0.01, actual_size_mb):.2f} MB" if actual_size_mb > 0 else "-"
                size_str = f"{orig_sz_str} / {comp_sz_str}"
            else:
                comp_mb = matched_live.get("comp_size_mb") if matched_live else (matched_meta.get("comp_size_mb") if matched_meta else enriched_meta.get("comp_size_mb"))
                orig_sz_str = f"{max(0.01, actual_size_mb):.2f} MB" if actual_size_mb > 0 else "-"
                comp_sz_str = f"{max(0.01, comp_mb):.2f} MB" if (comp_mb is not None and comp_mb > 0) else "-"
                size_str = f"{orig_sz_str} / {comp_sz_str}" if comp_mb else orig_sz_str
        else:
            comp_mb = matched_live.get("comp_size_mb") if matched_live else (matched_meta.get("comp_size_mb") if matched_meta else None)
            orig_sz_str = f"{max(0.01, actual_size_mb):.2f} MB" if actual_size_mb > 0 else "-"
            comp_sz_str = f"{max(0.01, comp_mb):.2f} MB" if (comp_mb is not None and comp_mb > 0) else "-"
            size_str = f"{orig_sz_str} / {comp_sz_str}"

        # Determine Orig / Target DPI & Color Mode
        first_page_meta = matched_meta.get("pages", [{}])[0] if (matched_meta and matched_meta.get("pages")) else {}
        orig_dpi = (matched_live.get("orig_dpi") if matched_live else None) or (matched_meta.get("max_dpi") if matched_meta else None) or first_page_meta.get("dpi")
        orig_cmode = (matched_live.get("orig_cmode") if matched_live else None) or (matched_meta.get("color_mode") if matched_meta else None) or first_page_meta.get("color_mode")
        target_dpi = (matched_live.get("target_dpi") if matched_live else None) or (matched_meta.get("target_dpi") if matched_meta else None) or first_page_meta.get("target_dpi") or eff_tune.get("target_dpi", ui_dpi)
        target_cmode = (matched_live.get("target_cmode") if matched_live else None) or (matched_meta.get("target_cmode") if matched_meta else None) or first_page_meta.get("target_cmode") or eff_tune.get("color_mode", "color")

        if orig_dpi:
            orig_res_str = f"{orig_dpi} DPI ({self._format_color_mode(orig_cmode)})" if orig_cmode else f"{orig_dpi} DPI"
        else:
            orig_res_str = "-"

        if target_dpi:
            target_res_str = f"{target_dpi} DPI ({self._format_color_mode(target_cmode)})" if target_cmode else f"{target_dpi} DPI"
        else:
            target_res_str = "-"

        if is_audit_report or (self.browser_mode == "results_by_doc" and low_ext in (".txt", ".hocr", ".json", ".csv", ".xlsx")):
            img_res_str = "-"
        else:
            img_res_str = f"{orig_res_str} / {target_res_str}"

        # Status, Elapsed & Confidence
        status_val = (matched_live.get("status") if matched_live else None) or (matched_meta.get("status") if matched_meta else None) or ("done" if self.browser_mode.startswith("results") else "")
        elapsed = (matched_live.get("elapsed") if matched_live else None) or (matched_meta.get("elapsed") if matched_meta else 0.0) or 0.0

        fconf = (
            (matched_live.get("confidence") if (matched_live and matched_live.get("confidence") is not None) else None)
            or (matched_meta.get("confidence") if (matched_meta and matched_meta.get("confidence") is not None) else None)
            or enriched_meta.get("confidence")
        )
        min_pconf = (
            (matched_live.get("min_page_confidence") if (matched_live and matched_live.get("min_page_confidence") is not None) else None)
            or (matched_meta.get("min_page_confidence") if (matched_meta and matched_meta.get("min_page_confidence") is not None) else None)
            or enriched_meta.get("min_page_confidence")
        )
        low_pages = (
            (matched_live.get("low_conf_pages") if (matched_live and matched_live.get("low_conf_pages") is not None) else None)
            or (matched_meta.get("low_conf_pages") if (matched_meta and matched_meta.get("low_conf_pages") is not None) else None)
            or enriched_meta.get("low_conf_pages", 0)
            or 0
        )

        if fconf is None and low_ext == ".pdf" and not is_audit_report:
            try:
                with fitz.open(fpath) as doc:
                    p_confs = []
                    tot_c = 0
                    for page in doc:
                        t = page.get_text("text").strip()
                        w_list = page.get_text("words")
                        pc = len(t)
                        tot_c += pc
                        p_confs.append((estimate_text_layer_confidence(w_list, t) if pc > 0 else 0.0, pc))
                    if p_confs:
                        if tot_c > 0:
                            fconf = round(sum(c * n for c, n in p_confs) / float(tot_c), 1)
                        else:
                            fconf = round(sum(c for c, _ in p_confs) / float(len(p_confs)), 1)
                        min_pconf = round(min(c for c, _ in p_confs), 1)
                        low_pages = sum(1 for c, n in p_confs if (c < 80.0 or n == 0))
                        if total_chars is None:
                            total_chars = tot_c
            except Exception:
                pass

        conf_badge = None
        if not is_audit_report:
            conf_badge, _ = self._get_file_conf_badge_and_tag(fconf, min_pconf, low_pages, total_chars, status_val)

        if status_val == "done":
            status_str = f"{elapsed:.1f}s" if elapsed and elapsed > 0 else self._tr("status_ready")
        elif status_val == "processing":
            status_str = self._tr("status_processing")
        elif status_val == "interrupted":
            status_str = self._tr("status_interrupted")
        else:
            status_str = self._tr("status_ready") if self.browser_mode.startswith("results") else ""

        chars_str = f"{total_chars:,}" if (total_chars is not None and (total_chars > 0 or status_val == "done")) else "-"
        if is_audit_report:
            pages_chars_str = "-"
        elif conf_badge:
            pages_chars_str = f"{n_pages} / {chars_str} ({conf_badge})"
        else:
            pages_chars_str = f"{n_pages} / {chars_str}"

        tuning_str = f"{self._format_color_mode(target_cmode)} ({target_dpi} DPI)"

        row_vals = []
        for col in self.columns:
            if col == "Format":
                row_vals.append(fmt_display)
            elif col == "PagesChars":
                row_vals.append(pages_chars_str)
            elif col == "Size":
                row_vals.append(size_str)
            elif col == "Pages":
                row_vals.append(str(n_pages))
            elif col == "ImgRes":
                row_vals.append(img_res_str)
            elif col == "Chars":
                row_vals.append(chars_str)
            elif col == "Status":
                row_vals.append(status_str)
            elif col == "Time":
                row_vals.append(f"{elapsed:.1f}s" if elapsed > 0 else "-")
            elif col == "Tuning":
                row_vals.append(tuning_str)
            elif col == "DateModified":
                row_vals.append(date_str)
            else:
                row_vals.append("-")

        # Resolve page list metadata
        pages_list = []
        if matched_meta and "pages" in matched_meta and len(matched_meta["pages"]) > 0:
            pages_list = matched_meta["pages"]

        finfo = {
            "type": "file",
            "path": fpath,
            "filename": fname,
            "src_filename": src_filename,
            "format": ext,
            "n_pages": n_pages,
            "total_chars": total_chars,
            "confidence": fconf,
            "min_page_confidence": min_pconf,
            "low_conf_pages": low_pages,
            "is_audit_report": is_audit_report,
            "size_mb": actual_size_mb,
            "orig_size_mb": orig_size_mb,
            "orig_dpi": orig_dpi,
            "orig_cmode": orig_cmode,
            "target_dpi": target_dpi,
            "target_cmode": target_cmode,
            "status": status_val,
            "elapsed": elapsed,
            "mtime": mtime,
            "pages": pages_list
        }
        return finfo, tuple(row_vals)

    def _build_page_row_values(self, pinfo, src_filename, tuning_cfg, ui_dpi=300):
        pidx = pinfo.get("page_idx", 0)
        p_prefix = self._tr("page_prefix").format(idx=pidx + 1)
        w, h = pinfo.get("width"), pinfo.get("height")
        dims_str = f"{w} × {h} px" if (w and h) else "-"

        pchars = pinfo.get("chars")
        status_val = pinfo.get("status", "done" if self.browser_mode.startswith("results") else "")
        conf_badge, _, _ = self._get_page_conf_badge_tag_and_label(pinfo, pidx)
        pchars_str = f"{pchars:,}" if (pchars is not None and (pchars > 0 or status_val == "done")) else "-"
        pages_chars_str = f"{pchars_str} ({conf_badge})" if conf_badge else f"{pchars_str}"

        ptune = get_effective_file_tuning(tuning_cfg, src_filename, page_idx=pidx, default_dpi=ui_dpi)
        orig_dpi = pinfo.get("dpi") or pinfo.get("orig_dpi")
        orig_cmode = pinfo.get("color_mode") or pinfo.get("orig_cmode")
        target_dpi = pinfo.get("target_dpi") or ptune.get("target_dpi", ui_dpi)
        target_cmode = pinfo.get("target_cmode") or ptune.get("color_mode", "color")

        if orig_dpi:
            orig_str = f"{orig_dpi} DPI ({self._format_color_mode(orig_cmode)})" if orig_cmode else f"{orig_dpi} DPI"
        else:
            orig_str = "-"

        if target_dpi:
            target_str = f"{target_dpi} DPI ({self._format_color_mode(target_cmode)})" if target_cmode else f"{target_dpi} DPI"
        else:
            target_str = "-"
        img_res_str = f"{orig_str} / {target_str}"

        elapsed = pinfo.get("elapsed", 0.0)
        if status_val == "done" and elapsed:
            status_str = f"{elapsed:.1f}s"
        elif status_val == "done":
            status_str = self._tr("status_ready")
        elif status_val == "processing":
            status_str = self._tr("status_processing")
        else:
            status_str = ""

        tuning_page_str = f"{self._format_color_mode(target_cmode)} ({target_dpi} DPI)"

        child_vals = []
        for col in self.columns:
            if col == "Format":
                child_vals.append(dims_str)
            elif col == "PagesChars":
                child_vals.append(pages_chars_str)
            elif col == "Size":
                child_vals.append("- / -")
            elif col == "Pages":
                child_vals.append(p_prefix)
            elif col == "ImgRes":
                child_vals.append(img_res_str)
            elif col == "Chars":
                child_vals.append(pchars_str)
            elif col == "Status":
                child_vals.append(status_str)
            elif col == "Time":
                child_vals.append(f"{elapsed:.1f}s" if elapsed else "-")
            elif col == "Tuning":
                child_vals.append(tuning_page_str)
            elif col == "DateModified":
                child_vals.append("-")
            else:
                child_vals.append("-")

        return tuple(child_vals)

    def _collect_files_in_dir(self, target_dir, input_dir="", input_files_norm=None):
        """
        Fast single-pass directory scan using os.scandir.
        Returns list of (fpath, stat_size, stat_mtime) sorted by filename.
        """
        input_files_norm = input_files_norm or set()
        is_same_as_input = bool(
            input_dir
            and os.path.exists(input_dir)
            and os.path.normcase(os.path.abspath(target_dir)) == os.path.normcase(os.path.abspath(input_dir))
        )

        dir_basename_low = os.path.basename(target_dir).lower()
        if self.browser_mode == "results_docs":
            valid_exts = {".pdf", ".docx"}
        elif self.browser_mode == "results_data":
            valid_exts = {".txt", ".hocr", ".json", ".csv", ".xlsx"}
            if dir_basename_low == "docx from pdf":
                valid_exts.add(".docx")
        else:
            valid_exts = set()
            if not self.show_filters or self.var_f_pdf.get():
                valid_exts.add(".pdf")
            if not self.show_filters or self.var_f_tiff.get():
                valid_exts.update({".tif", ".tiff"})
            if not self.show_filters or self.var_f_jpg.get():
                valid_exts.update({".jpg", ".jpeg"})
            if not self.show_filters or self.var_f_png.get():
                valid_exts.update({".png", ".bmp", ".webp"})

        entries = []
        try:
            with os.scandir(target_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    bname = entry.name
                    low_name = bname.lower()
                    if low_name in (METADATA_FILENAME.lower(), TUNING_FILENAME.lower()):
                        continue

                    ext_low = os.path.splitext(low_name)[1]
                    if self.browser_mode == "results_data" and low_name.endswith("_from_pdf.docx"):
                        pass
                    elif ext_low not in valid_exts:
                        continue

                    if self.browser_mode == "results_docs":
                        if low_name.endswith("_from_pdf.docx"):
                            continue
                        norm_f = os.path.normcase(os.path.abspath(entry.path))
                        if is_same_as_input and norm_f in input_files_norm:
                            if not (low_name.endswith("_compressed.pdf") or low_name.endswith("_original.pdf")):
                                continue

                    try:
                        st = entry.stat()
                        sz = st.st_size
                        mt = st.st_mtime
                    except Exception:
                        sz = 0
                        mt = 0.0

                    entries.append((bname.lower(), entry.path, sz, mt))
        except Exception:
            pass

        entries.sort(key=lambda x: x[0])
        return [(p, sz, mt) for _, p, sz, mt in entries]

    def _collect_all_virtual_entries(self, docs_dir, data_dir, input_dir="", input_files_norm=None):
        """
        Scans both docs_dir and data_dir (including their result subfolders) in a single pass
        and returns deduplicated list of (fpath, stat_size, stat_mtime, rel_folder_label).
        """
        input_files_norm = input_files_norm or set()
        all_result_exts = {".pdf", ".docx", ".txt", ".hocr", ".json", ".csv", ".xlsx"}
        seen_norm_paths = set()
        results = []

        roots_to_scan = []
        if docs_dir and os.path.exists(docs_dir):
            roots_to_scan.append(docs_dir)
        if data_dir and os.path.exists(data_dir):
            if not roots_to_scan or os.path.normcase(os.path.abspath(data_dir)) != os.path.normcase(os.path.abspath(docs_dir)):
                roots_to_scan.append(data_dir)

        def _scan_dir_for_virtual(target_dir, rel_label=""):
            is_same_as_input = bool(
                input_dir
                and os.path.exists(input_dir)
                and os.path.normcase(os.path.abspath(target_dir)) == os.path.normcase(os.path.abspath(input_dir))
            )
            try:
                with os.scandir(target_dir) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        bname = entry.name
                        low_name = bname.lower()
                        if low_name in (METADATA_FILENAME.lower(), TUNING_FILENAME.lower()):
                            continue
                        ext_low = os.path.splitext(low_name)[1]
                        if ext_low not in all_result_exts:
                            continue

                        norm_f = os.path.normcase(os.path.abspath(entry.path))
                        if norm_f in seen_norm_paths:
                            continue
                        if is_same_as_input and norm_f in input_files_norm:
                            if not (low_name.endswith("_compressed.pdf") or low_name.endswith("_original.pdf")):
                                continue

                        seen_norm_paths.add(norm_f)
                        try:
                            st = entry.stat()
                            sz, mt = st.st_size, st.st_mtime
                        except Exception:
                            sz, mt = 0, 0.0
                        results.append((entry.path, sz, mt, rel_label))
            except Exception:
                pass

        for root_d in roots_to_scan:
            _scan_dir_for_virtual(root_d, rel_label="")
            try:
                with os.scandir(root_d) as it:
                    subdirs = sorted(
                        [e for e in it if e.is_dir() and not e.name.startswith(".") and e.name.lower() != "__pycache__"],
                        key=lambda e: e.name.lower()
                    )
            except Exception:
                subdirs = []
            for sdir_entry in subdirs:
                _scan_dir_for_virtual(sdir_entry.path, rel_label=sdir_entry.name)

        return results

    def _insert_file_node(self, parent_node, fpath, f_idx, meta_files, live_file_infos, tuning_cfg, ui_dpi=300, stat_size=None, stat_mtime=None, lazy_pages=False, rel_folder_label="", enriched_meta=None):
        finfo, row_vals = self._build_file_info_and_values(
            fpath, meta_files, live_file_infos, tuning_cfg, ui_dpi=ui_dpi, stat_size=stat_size, stat_mtime=stat_mtime, rel_folder_label=rel_folder_label, enriched_meta=enriched_meta
        )
        fname = finfo["filename"]
        src_filename = finfo["src_filename"]
        low_ext = os.path.splitext(fpath)[1].lower()
        n_pages = finfo["n_pages"]

        if finfo.get("is_audit_report"):
            file_tag = "even" if f_idx % 2 == 0 else "odd"
        else:
            _, file_tag = self._get_file_conf_badge_and_tag(
                finfo.get("confidence"),
                finfo.get("min_page_confidence"),
                finfo.get("low_conf_pages", 0),
                finfo.get("total_chars"),
                finfo.get("status", ""),
                f_idx=f_idx
            )
        if self.browser_mode == "results_by_doc" and parent_node:
            display_text = f"   ├── 📄 {fname}  [{rel_folder_label}]" if rel_folder_label else f"   ├── 📄 {fname}"
        else:
            display_text = f"📄 {fname}"

        item_id = self.tree.insert(
            parent_node,
            "end",
            text=display_text,
            values=row_vals,
            open=False,
            tags=(file_tag,)
        )
        self.file_to_item[fpath] = item_id
        self.item_to_info[item_id] = finfo

        # Pre-populate pages if expandable (or insert dummy node when lazy_pages=True for huge batches)
        can_expand_pages = low_ext in (".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp", ".webp")
        pages_list = finfo.get("pages", [])
        if can_expand_pages and pages_list and not lazy_pages:
            for p_idx, pinfo in enumerate(pages_list):
                pidx = pinfo.get("page_idx", p_idx)
                child_vals = self._build_page_row_values(pinfo, src_filename, tuning_cfg, ui_dpi=ui_dpi)
                _, ptag, page_label = self._get_page_conf_badge_tag_and_label(pinfo, p_idx)
                child_id = self.tree.insert(
                    item_id,
                    "end",
                    text=page_label,
                    values=child_vals,
                    tags=(ptag,)
                )
                self.item_to_info[child_id] = {
                    "type": "page",
                    "path": fpath,
                    "filename": fname,
                    "src_filename": src_filename,
                    "page_idx": pidx,
                    "parent_id": item_id
                }
            self.loaded_pages.add(fpath)
        elif can_expand_pages and (pages_list or n_pages > 1 or low_ext in (".pdf", ".tif", ".tiff")):
            dummy_id = f"{item_id}_dummy"
            self.tree.insert(item_id, "end", iid=dummy_id, text=self._tr("loading_pages"), values=())

        return item_id, finfo

    def refresh(self, progressive=False):
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None

        if not self.current_dir or not os.path.exists(self.current_dir):
            return

        ctx = self._get_shared_context()
        input_dir = ctx.get("input_dir") or self.current_dir
        docs_dir = ctx.get("docs_dir") or self.current_dir
        data_dir = ctx.get("data_dir") or docs_dir
        ui_dpi = ctx.get("ui_dpi", 300)
        live_file_infos = ctx.get("live_file_infos") or {}
        input_files_norm = ctx.get("input_files_norm") or set()

        # Update path bar state depending on whether we are in virtual view or real folder view
        if hasattr(self, "path_entry"):
            self.path_entry.delete(0, "end")
            if self.browser_mode == "results_by_doc":
                self.path_entry.insert(
                    0,
                    self._tr("virtual_view_banner").format(docs_dir=docs_dir, data_dir=data_dir)
                )
                if hasattr(self, "btn_up"):
                    self.btn_up.configure(state="disabled")
                if hasattr(self, "btn_browse"):
                    self.btn_browse.configure(state="disabled")
            else:
                self.path_entry.insert(0, self.current_dir)
                if hasattr(self, "btn_up"):
                    self.btn_up.configure(state="normal")
                if hasattr(self, "btn_browse"):
                    self.btn_browse.configure(state="normal")

        # Load tuning & metadata from shared context or disk
        self.tuning_config = ctx.get("folder_tuning") or load_tuning_config(input_dir) or load_tuning_config(self.current_dir)
        self.metadata_config = ctx.get("folder_metadata") or load_folder_metadata(input_dir) or load_folder_metadata(self.current_dir)
        meta_files = self.metadata_config.get("files", {}) if self.metadata_config else {}

        self._build_metadata_indices(meta_files, live_file_infos)

        # Clear existing tree
        self.tree.delete(*self.tree.get_children())
        self.found_files = []
        self.file_to_item = {}
        self.item_to_info = {}
        self.loaded_pages.clear()
        self._pending_tasks = []

        if self.browser_mode == "results_by_doc":
            all_virtual_entries = self._collect_all_virtual_entries(
                docs_dir, data_dir, input_dir=input_dir, input_files_norm=input_files_norm
            )
            doc_groups = {}
            audit_entries = []

            for fpath, sz, mt, rel_label in all_virtual_entries:
                bname_low = os.path.basename(fpath).lower()
                if bname_low.startswith("batch_processing_report") or rel_label.lower() == "audit_reports":
                    audit_entries.append((fpath, sz, mt, rel_label))
                    continue

                matched_live, matched_meta, src_filename, eff_tune = self._resolve_source_metadata(
                    fpath, meta_files, live_file_infos, self.tuning_config, ui_dpi=ui_dpi
                )
                if matched_live or matched_meta:
                    doc_stem = os.path.splitext(src_filename)[0]
                    doc_display = src_filename
                else:
                    raw_stem = os.path.splitext(os.path.basename(fpath))[0]
                    doc_stem = self._strip_result_suffix(raw_stem)
                    doc_display = doc_stem

                doc_key = doc_stem.lower()
                if doc_key not in doc_groups:
                    doc_groups[doc_key] = {
                        "doc_stem": doc_stem,
                        "doc_display": doc_display,
                        "src_filename": src_filename,
                        "matched_live": matched_live,
                        "matched_meta": matched_meta,
                        "eff_tune": eff_tune,
                        "entries": []
                    }
                doc_groups[doc_key]["entries"].append((fpath, sz, mt, rel_label))

            ext_priority = {".pdf": 1, ".docx": 2, ".txt": 3, ".hocr": 4, ".xlsx": 5, ".csv": 6, ".json": 7}
            open_by_default = len(doc_groups) <= 30

            for doc_key in sorted(doc_groups.keys()):
                gdata = doc_groups[doc_key]
                entries = gdata["entries"]
                entries.sort(
                    key=lambda item: (
                        ext_priority.get(os.path.splitext(item[0])[1].lower(), 99),
                        0 if item[0].lower().endswith("_compressed.pdf") else 1,
                        os.path.basename(item[0]).lower()
                    )
                )

                matched_live = gdata["matched_live"]
                matched_meta = gdata["matched_meta"]
                eff_tune = gdata["eff_tune"]
                src_filename = gdata["src_filename"]

                latest_mtime = max((mt for _, _, mt, _ in entries), default=0.0)
                tot_group_mb = sum(sz for _, sz, _, _ in entries) / (1024 * 1024)

                # Cross-enrich document metadata across all generated files in this virtual folder
                pdf_entries = [e for e in entries if os.path.splitext(e[0])[1].lower() == ".pdf"]
                txt_entries = [e for e in entries if os.path.splitext(e[0])[1].lower() == ".txt"]

                primary_open_path = entries[0][0] if entries else None
                primary_visual_path = None
                if input_dir and src_filename:
                    cand = os.path.join(input_dir, src_filename)
                    if os.path.exists(cand):
                        primary_visual_path = cand
                if not primary_visual_path and pdf_entries:
                    primary_visual_path = pdf_entries[0][0]

                n_pages = (
                    (matched_live.get("n_pages") if matched_live else None)
                    or (matched_meta.get("n_pages") if matched_meta else None)
                )
                if n_pages is None:
                    if pdf_entries:
                        try:
                            with fitz.open(pdf_entries[0][0]) as doc:
                                n_pages = len(doc)
                        except Exception:
                            n_pages = 1
                    else:
                        n_pages = 1

                total_chars = (
                    (matched_live.get("total_chars") if matched_live else None)
                    if (matched_live and matched_live.get("total_chars") is not None)
                    else (matched_meta.get("total_chars") if matched_meta else None)
                )
                if total_chars is None and txt_entries:
                    try:
                        with open(txt_entries[0][0], "r", encoding="utf-8", errors="ignore") as tf:
                            total_chars = len(tf.read().strip())
                    except Exception:
                        pass

                orig_size_mb = (
                    (matched_live.get("size_mb") if matched_live else None)
                    or (matched_meta.get("size_mb") if matched_meta else None)
                )
                if not orig_size_mb and input_dir and src_filename:
                    cand_src = os.path.join(input_dir, src_filename)
                    if os.path.exists(cand_src):
                        try:
                            orig_size_mb = os.path.getsize(cand_src) / (1024 * 1024)
                        except Exception:
                            pass

                comp_size_mb = (
                    (matched_live.get("comp_size_mb") if matched_live else None)
                    or (matched_meta.get("comp_size_mb") if matched_meta else None)
                )
                if not comp_size_mb and pdf_entries:
                    comp_size_mb = pdf_entries[0][1] / (1024 * 1024)

                doc_conf = (
                    (matched_live.get("confidence") if (matched_live and matched_live.get("confidence") is not None) else None)
                    or (matched_meta.get("confidence") if (matched_meta and matched_meta.get("confidence") is not None) else None)
                )
                doc_min_pconf = (
                    (matched_live.get("min_page_confidence") if (matched_live and matched_live.get("min_page_confidence") is not None) else None)
                    or (matched_meta.get("min_page_confidence") if (matched_meta and matched_meta.get("min_page_confidence") is not None) else None)
                )
                doc_low_pages = (
                    (matched_live.get("low_conf_pages") if (matched_live and matched_live.get("low_conf_pages") is not None) else None)
                    or (matched_meta.get("low_conf_pages") if (matched_meta and matched_meta.get("low_conf_pages") is not None) else 0)
                    or 0
                )

                if doc_conf is None and pdf_entries:
                    try:
                        with fitz.open(pdf_entries[0][0]) as doc:
                            p_confs = []
                            tot_c = 0
                            for page in doc:
                                t = page.get_text("text").strip()
                                w_list = page.get_text("words")
                                pc = len(t)
                                tot_c += pc
                                p_confs.append((estimate_text_layer_confidence(w_list, t) if pc > 0 else 0.0, pc))
                            if p_confs:
                                if tot_c > 0:
                                    doc_conf = round(sum(c * n for c, n in p_confs) / float(tot_c), 1)
                                else:
                                    doc_conf = round(sum(c for c, _ in p_confs) / float(len(p_confs)), 1)
                                doc_min_pconf = round(min(c for c, _ in p_confs), 1)
                                doc_low_pages = sum(1 for c, n in p_confs if (c < 80.0 or n == 0))
                                if total_chars is None:
                                    total_chars = tot_c
                    except Exception:
                        pass

                enriched_meta = {
                    "n_pages": n_pages,
                    "total_chars": total_chars,
                    "confidence": doc_conf,
                    "min_page_confidence": doc_min_pconf,
                    "low_conf_pages": doc_low_pages,
                    "orig_size_mb": orig_size_mb,
                    "comp_size_mb": comp_size_mb
                }

                first_page_meta = matched_meta.get("pages", [{}])[0] if (matched_meta and matched_meta.get("pages")) else {}
                orig_dpi = (matched_live.get("orig_dpi") if matched_live else None) or (matched_meta.get("max_dpi") if matched_meta else None) or first_page_meta.get("dpi")
                orig_cmode = (matched_live.get("orig_cmode") if matched_live else None) or (matched_meta.get("color_mode") if matched_meta else None) or first_page_meta.get("color_mode")
                target_dpi = (matched_live.get("target_dpi") if matched_live else None) or (matched_meta.get("target_dpi") if matched_meta else None) or first_page_meta.get("target_dpi") or eff_tune.get("target_dpi", ui_dpi)
                target_cmode = (matched_live.get("target_cmode") if matched_live else None) or (matched_meta.get("target_cmode") if matched_meta else None) or first_page_meta.get("target_cmode") or eff_tune.get("color_mode", "color")

                orig_res_str = f"{orig_dpi} DPI ({self._format_color_mode(orig_cmode)})" if (orig_dpi and orig_cmode) else (f"{orig_dpi} DPI" if orig_dpi else "-")
                target_res_str = f"{target_dpi} DPI ({self._format_color_mode(target_cmode)})" if (target_dpi and target_cmode) else (f"{target_dpi} DPI" if target_dpi else "-")
                img_res_str = f"{orig_res_str} / {target_res_str}"

                status_val = (matched_live.get("status") if matched_live else None) or (matched_meta.get("status") if matched_meta else None) or "done"
                elapsed = (matched_live.get("elapsed") if matched_live else None) or (matched_meta.get("elapsed") if matched_meta else 0.0) or 0.0

                vconf_badge, vfolder_tag = self._get_file_conf_badge_and_tag(
                    doc_conf, doc_min_pconf, doc_low_pages, total_chars, status_val, is_virtual_doc=True
                )
                status_str = f"{elapsed:.1f}s" if (status_val == "done" and elapsed and elapsed > 0) else self._tr("status_ready")

                chars_str = f"{total_chars:,}" if total_chars is not None else "-"
                pages_chars_str = f"{n_pages} / {chars_str} ({vconf_badge})" if vconf_badge else f"{n_pages} / {chars_str}"

                if orig_size_mb and comp_size_mb:
                    vfolder_size_str = f"{max(0.01, orig_size_mb):.2f} MB / {max(0.01, comp_size_mb):.2f} MB"
                elif orig_size_mb:
                    vfolder_size_str = f"{max(0.01, orig_size_mb):.2f} MB / Σ {max(0.01, tot_group_mb):.2f} MB"
                else:
                    vfolder_size_str = f"Σ {max(0.01, tot_group_mb):.2f} MB"

                vfolder_date_str = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S") if latest_mtime > 0 else "-"
                vfolder_fmt_str = self._tr("virtual_doc_folder_type").format(count=len(entries))

                vfolder_vals = []
                for col in self.columns:
                    if col == "Format":
                        vfolder_vals.append(vfolder_fmt_str)
                    elif col == "PagesChars":
                        vfolder_vals.append(pages_chars_str)
                    elif col == "Size":
                        vfolder_vals.append(vfolder_size_str)
                    elif col == "ImgRes":
                        vfolder_vals.append(img_res_str)
                    elif col == "Status":
                        vfolder_vals.append(status_str)
                    elif col == "DateModified":
                        vfolder_vals.append(vfolder_date_str)
                    else:
                        vfolder_vals.append("-")

                vfolder_id = self.tree.insert(
                    "",
                    "end",
                    text=f"🗂️ {gdata['doc_display']}",
                    values=tuple(vfolder_vals),
                    open=open_by_default,
                    tags=(vfolder_tag,)
                )
                self.item_to_info[vfolder_id] = {
                    "type": "virtual_doc",
                    "path": primary_open_path,
                    "viewable_path": primary_visual_path,
                    "filename": gdata["doc_display"],
                    "src_filename": src_filename,
                    "n_pages": n_pages,
                    "total_chars": total_chars,
                    "size_mb": orig_size_mb or tot_group_mb,
                    "comp_size_mb": comp_size_mb,
                    "orig_dpi": orig_dpi,
                    "target_dpi": target_dpi,
                    "elapsed": elapsed,
                    "mtime": latest_mtime
                }

                for sf_idx, (sf_path, sz, mt, rel_label) in enumerate(entries):
                    self.found_files.append(sf_path)
                    self._pending_tasks.append((vfolder_id, sf_path, sf_idx, sz, mt, rel_label, enriched_meta))

            # Add Batch Audit Reports group at the bottom if present
            if audit_entries:
                tot_audit_mb = sum(sz for _, sz, _, _ in audit_entries) / (1024 * 1024)
                latest_audit_mt = max((mt for _, _, mt, _ in audit_entries), default=0.0)
                audit_date_str = datetime.fromtimestamp(latest_audit_mt).strftime("%Y-%m-%d %H:%M:%S") if latest_audit_mt > 0 else "-"
                audit_fmt_str = self._tr("virtual_doc_folder_type").format(count=len(audit_entries))
                audit_vals = []
                for col in self.columns:
                    if col == "Format":
                        audit_vals.append(audit_fmt_str)
                    elif col == "PagesChars":
                        audit_vals.append(self._tr("files_count").format(count=len(audit_entries)))
                    elif col == "Size":
                        audit_vals.append(f"Σ {max(0.01, tot_audit_mb):.2f} MB")
                    elif col == "DateModified":
                        audit_vals.append(audit_date_str)
                    elif col == "Status":
                        audit_vals.append(self._tr("status_ready"))
                    else:
                        audit_vals.append("-")

                audit_id = self.tree.insert(
                    "",
                    "end",
                    text=f"📊 {self._tr('virtual_audit_group')}",
                    values=tuple(audit_vals),
                    open=open_by_default,
                    tags=("folder_row",)
                )
                self.item_to_info[audit_id] = {
                    "type": "virtual_doc",
                    "path": audit_entries[0][0],
                    "viewable_path": None,
                    "filename": self._tr("virtual_audit_group"),
                    "size_mb": tot_audit_mb,
                    "mtime": latest_audit_mt
                }
                for sf_idx, (sf_path, sz, mt, rel_label) in enumerate(audit_entries):
                    self.found_files.append(sf_path)
                    self._pending_tasks.append((audit_id, sf_path, sf_idx, sz, mt, rel_label, None))

        else:
            # 1. Discover relevant subfolders in current_dir when in physical results mode
            if self.browser_mode in ("results_docs", "results_data"):
                try:
                    with os.scandir(self.current_dir) as it:
                        subdirs = sorted(
                            [e.path for e in it if e.is_dir() and not e.name.startswith(".") and e.name.lower() != "__pycache__"],
                            key=lambda p: os.path.basename(p).lower()
                        )
                except Exception:
                    subdirs = []

                for sdir in subdirs:
                    dname = os.path.basename(sdir)
                    dlow = dname.lower()
                    if self.browser_mode == "results_docs" and dlow in DATA_SUBFOLDER_NAMES:
                        continue
                    if self.browser_mode == "results_data" and dlow in DOC_SUBFOLDER_NAMES:
                        continue

                    sub_entries = self._collect_files_in_dir(sdir, input_dir=input_dir, input_files_norm=input_files_norm)
                    if not sub_entries and dlow not in DOC_SUBFOLDER_NAMES and dlow not in DATA_SUBFOLDER_NAMES:
                        continue

                    tot_mb = 0.0
                    latest_mtime = 0.0
                    for _, sz, mt in sub_entries:
                        tot_mb += sz / (1024 * 1024)
                        if mt > latest_mtime:
                            latest_mtime = mt
                    if latest_mtime == 0.0:
                        try:
                            latest_mtime = os.path.getmtime(sdir)
                        except Exception:
                            pass

                    folder_date_str = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S") if latest_mtime > 0 else "-"
                    folder_size_str = f"{max(0.01, tot_mb):.2f} MB" if tot_mb > 0 else "-"
                    count_str = self._tr("files_count").format(count=len(sub_entries))

                    folder_vals = []
                    for col in self.columns:
                        if col == "Format":
                            folder_vals.append(self._tr("folder_type"))
                        elif col in ("PagesChars", "Pages"):
                            folder_vals.append(count_str)
                        elif col == "Size":
                            folder_vals.append(folder_size_str)
                        elif col == "DateModified":
                            folder_vals.append(folder_date_str)
                        else:
                            folder_vals.append("-")

                    folder_id = self.tree.insert(
                        "",
                        "end",
                        text=f"📁 {dname}",
                        values=tuple(folder_vals),
                        open=True,
                        tags=("folder_row",)
                    )
                    self.item_to_info[folder_id] = {
                        "type": "folder",
                        "path": sdir,
                        "filename": dname
                    }

                    for sf_idx, (sf_path, sz, mt) in enumerate(sub_entries):
                        self.found_files.append(sf_path)
                        self._pending_tasks.append((folder_id, sf_path, sf_idx, sz, mt, "", None))

            # 2. Discover files directly in current_dir
            direct_entries = self._collect_files_in_dir(self.current_dir, input_dir=input_dir, input_files_norm=input_files_norm)
            for f_idx, (fpath, sz, mt) in enumerate(direct_entries):
                self.found_files.append(fpath)
                self._pending_tasks.append(("", fpath, f_idx, sz, mt, "", None))

        self._total_tasks = len(self._pending_tasks)
        self._refresh_ctx = (meta_files, live_file_infos, ui_dpi)

        if self.on_selection_changed:
            self.on_selection_changed([], [])

        if progressive and self._total_tasks > 0:
            self.loading_frame.grid()
            self.loading_bar.set(0.0)
            self.lbl_loading.configure(
                text=self._tr("loading_results_progress").format(curr=0, total=self._total_tasks)
            )
            self.update_idletasks()
            self._refresh_job = self.after(1, self._process_refresh_chunk)
        else:
            self.flush_pending_refresh()

    def _process_refresh_chunk(self):
        self._refresh_job = None
        if not self.winfo_exists():
            return
        if not self._pending_tasks:
            self._finish_refresh()
            return

        meta_files, live_file_infos, ui_dpi = getattr(self, "_refresh_ctx", ({}, {}, 300))
        lazy_pages = self._total_tasks > 25 or self.browser_mode == "results_by_doc"
        chunk_size = 15

        batch = self._pending_tasks[:chunk_size]
        self._pending_tasks = self._pending_tasks[chunk_size:]

        for task_item in batch:
            parent_node, fpath, f_idx, sz, mt = task_item[:5]
            rel_label = task_item[5] if len(task_item) > 5 else ""
            enriched_meta = task_item[6] if len(task_item) > 6 else None
            self._insert_file_node(
                parent_node,
                fpath,
                f_idx,
                meta_files,
                live_file_infos,
                self.tuning_config,
                ui_dpi=ui_dpi,
                stat_size=sz,
                stat_mtime=mt,
                lazy_pages=lazy_pages,
                rel_folder_label=rel_label,
                enriched_meta=enriched_meta
            )

        done_count = self._total_tasks - len(self._pending_tasks)
        if self._total_tasks > 0:
            self.loading_bar.set(float(done_count) / float(self._total_tasks))
            self.lbl_loading.configure(
                text=self._tr("loading_results_progress").format(curr=done_count, total=self._total_tasks)
            )

        if self._pending_tasks:
            self.update_idletasks()
            self._refresh_job = self.after(1, self._process_refresh_chunk)
        else:
            self._finish_refresh()

    def flush_pending_refresh(self):
        """Synchronously completes any pending file insertion tasks."""
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None

        meta_files, live_file_infos, ui_dpi = getattr(self, "_refresh_ctx", ({}, {}, 300))
        lazy_pages = self._total_tasks > 40 or self.browser_mode == "results_by_doc"
        while self._pending_tasks:
            task_item = self._pending_tasks.pop(0)
            parent_node, fpath, f_idx, sz, mt = task_item[:5]
            rel_label = task_item[5] if len(task_item) > 5 else ""
            enriched_meta = task_item[6] if len(task_item) > 6 else None
            self._insert_file_node(
                parent_node,
                fpath,
                f_idx,
                meta_files,
                live_file_infos,
                self.tuning_config,
                ui_dpi=ui_dpi,
                stat_size=sz,
                stat_mtime=mt,
                lazy_pages=lazy_pages,
                rel_folder_label=rel_label,
                enriched_meta=enriched_meta
            )
        self._finish_refresh()

    def _finish_refresh(self):
        if hasattr(self, "loading_frame"):
            self.loading_frame.grid_remove()
        if hasattr(self, "_update_gridlines_fn"):
            self.after(30, self._update_gridlines_fn)

    def _on_tree_open(self, event):
        """Lazy load page details when user clicks expand (+) on a file node."""
        ctx = self._get_shared_context()
        ui_dpi = ctx.get("ui_dpi", 300)

        def _check_nodes(parent_container):
            for node_id in self.tree.get_children(parent_container):
                info = self.item_to_info.get(node_id)
                if not info:
                    continue
                if info.get("type") in ("folder", "virtual_doc"):
                    _check_nodes(node_id)
                    continue
                if info.get("type") != "file" or not self.tree.item(node_id, "open"):
                    continue

                fpath = info["path"]
                if fpath in self.loaded_pages:
                    continue

                for child in self.tree.get_children(node_id):
                    if child.endswith("_dummy"):
                        self.tree.delete(child)

                fname = info["filename"]
                src_filename = info.get("src_filename", fname)
                pages_list = info.get("pages") or get_file_pages_metadata(fpath)

                if pages_list:
                    for p_idx, pinfo in enumerate(pages_list):
                        pidx = pinfo.get("page_idx", p_idx)
                        child_vals = self._build_page_row_values(pinfo, src_filename, self.tuning_config, ui_dpi=ui_dpi)
                        _, ptag, page_label = self._get_page_conf_badge_tag_and_label(pinfo, p_idx)
                        child_id = self.tree.insert(
                            node_id,
                            "end",
                            text=page_label,
                            values=child_vals,
                            tags=(ptag,)
                        )
                        self.item_to_info[child_id] = {
                            "type": "page",
                            "path": fpath,
                            "filename": fname,
                            "src_filename": src_filename,
                            "page_idx": pidx,
                            "parent_id": node_id
                        }
                self.loaded_pages.add(fpath)

        _check_nodes("")

    def inspect_files(self, target_files=None, on_complete=None):
        """Runs batch inspection with progress dialog for the specified or selected/all files."""
        if not target_files:
            target_files = self.get_selected_files()
        if not target_files:
            target_files = list(self.found_files)

        if not target_files or not self.current_dir:
            return

        def _on_done():
            self.refresh()
            if on_complete:
                on_complete()

        InspectionProgressDialog(
            self.winfo_toplevel(),
            file_paths=target_files,
            folder_path=self.current_dir,
            lang=self.lang,
            on_complete=_on_done
        )

    def _on_tree_select(self, event):
        selected_ids = self.tree.selection()
        selected_files = []
        selected_pages = []

        for iid in selected_ids:
            info = self.item_to_info.get(iid)
            if info:
                if info["type"] in ("file", "virtual_doc") and info.get("path"):
                    selected_files.append(info["path"])
                elif info["type"] == "page":
                    selected_pages.append((info["path"], info["page_idx"]))

        if self.on_selection_changed:
            self.on_selection_changed(selected_files, selected_pages)

        if len(selected_ids) == 1:
            info = self.item_to_info.get(selected_ids[0])
            if info:
                if info["type"] in ("file", "virtual_doc") and info.get("path") and self.on_file_selected:
                    self.on_file_selected(info["path"])
                elif info["type"] == "page" and self.on_page_selected:
                    self.on_page_selected(info["path"], info["page_idx"])

    def _on_tree_double_click(self, event):
        if event and hasattr(event, "x") and hasattr(event, "y"):
            elt = self.tree.identify_element(event.x, event.y)
            if "indicator" in str(elt).lower():
                return

        selected_ids = self.tree.selection()
        if not selected_ids:
            return
        iid = selected_ids[0]
        info = self.item_to_info.get(iid)
        if not info:
            return

        if info["type"] == "folder":
            self.set_folder(info["path"])
        elif info["type"] == "virtual_doc":
            is_open = self.tree.item(iid, "open")
            self.tree.item(iid, open=not is_open)
        elif info["type"] == "page" and self.on_page_activated:
            self.on_page_activated(info["path"], info["page_idx"])
        elif info["type"] == "file":
            if self.on_page_activated and not self.tree.get_children(iid):
                self.on_page_activated(info["path"], 0)
            else:
                is_open = self.tree.item(iid, "open")
                self.tree.item(iid, open=not is_open)
                self._on_tree_open(None)

    def get_selected_files(self):
        """Returns list of selected file paths (or folder path if a folder is selected)."""
        selected_ids = self.tree.selection()
        files = []
        for iid in selected_ids:
            info = self.item_to_info.get(iid)
            if info and info.get("path"):
                fpath = info["path"]
                if fpath not in files:
                    files.append(fpath)
        return files

    def get_selected_page(self):
        """Returns (file_path, page_idx) of currently active/focused item."""
        selected_ids = self.tree.selection()
        if not selected_ids:
            return None, 0
        info = self.item_to_info.get(selected_ids[0])
        if not info or info.get("type") == "folder":
            return None, 0
        if info.get("type") == "virtual_doc":
            return info.get("viewable_path") or info.get("path"), 0
        return info["path"], info.get("page_idx", 0)

    def update_file_status(self, fpath, status=None, chars=None, time_s=None):
        """Updates row metadata for a specific file."""
        parent_id = self.file_to_item.get(fpath)
        if not parent_id:
            return

        current_vals = list(self.tree.item(parent_id, "values"))
        for idx, col in enumerate(self.columns):
            if col == "Status" and status is not None:
                current_vals[idx] = status
            elif col == "Chars" and chars is not None:
                current_vals[idx] = f"{chars:,}" if isinstance(chars, int) else str(chars)
            elif col == "Time" and time_s is not None:
                current_vals[idx] = f"{time_s:.2f}s" if isinstance(time_s, (int, float)) else str(time_s)

        self.tree.item(parent_id, values=tuple(current_vals))

    def _sort_by_column(self, col):
        """Sorts top-level nodes by specified column."""
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False

        parent_items = self.tree.get_children("")
        items = []

        for k in parent_items:
            if col == "#0":
                val = self.tree.item(k, "text")
            else:
                col_idx = self.columns.index(col)
                vals = self.tree.item(k, "values")
                val = vals[col_idx] if col_idx < len(vals) else ""
            items.append((val, k))

        def parse_key(tup):
            val, item_id = tup
            info = self.item_to_info.get(item_id, {})
            is_folder = 0 if info.get("type") == "folder" else 1
            v = str(val).replace("📄", "").replace("📁", "").strip()
            if col == "Size":
                return (is_folder, float(info.get("size_mb", 0.0) or 0.0))
            elif col == "PagesChars":
                return (is_folder, int(info.get("n_pages", 0) or 0), int(info.get("total_chars", 0) or 0))
            elif col in ["Pages", "ImgRes", "Chars"]:
                try:
                    return (is_folder, int(v.split()[0].replace("DPI", "").replace(",", "").strip()))
                except Exception:
                    return (is_folder, 0)
            elif col in ["Time", "Status"]:
                return (is_folder, float(info.get("elapsed", 0.0) or 0.0))
            return (is_folder, v.lower())

        items.sort(key=parse_key, reverse=self.sort_reverse)
        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)

    def set_language(self, new_lang):
        """Dynamically retranslates browser headers, controls, and refreshes tree."""
        self.lang = new_lang
        col_names = self._get_col_titles()
        self.tree.heading("#0", text=self._tr("col_filename"))
        for col in self.columns:
            self.tree.heading(col, text=col_names.get(col, col))
        if hasattr(self, "lbl_path"):
            self.lbl_path.configure(text=self._tr("folder_label"))
        if hasattr(self, "btn_up"):
            self.btn_up.configure(text=self._tr("btn_up_folder"))
        if hasattr(self, "btn_browse"):
            self.btn_browse.configure(text=self._tr("browse"))
        if hasattr(self, "btn_refresh"):
            self.btn_refresh.configure(text=f"⟳ {self._tr('refresh')}")
        self.refresh()
