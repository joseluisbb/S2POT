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
from core.tuning_config import load_tuning_config, get_effective_file_tuning
from core.metadata_config import load_folder_metadata, METADATA_FILENAME
from gui.inspection_dialog import InspectionProgressDialog


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
                    image_list = page.get_images()
                    if image_list:
                        for img in image_list:
                            try:
                                base_img = doc.extract_image(img[0])
                                w, h = base_img.get("width", 0), base_img.get("height", 0)
                                if pt_w > 0 and pt_h > 0 and w > 0 and h > 0:
                                    page_dpi = max(page_dpi, int(round((w * 72.0) / pt_w)))
                            except Exception:
                                pass
                    else:
                        pix = page.get_pixmap()
                        if pt_w > 0 and pt_h > 0:
                            page_dpi = max(page_dpi, int(round((pix.width * 72.0) / pt_w)))

                    pages_meta.append({
                        "page_idx": idx,
                        "width": int(round(pt_w * (page_dpi / 72.0))),
                        "height": int(round(pt_h * (page_dpi / 72.0))),
                        "dpi": page_dpi
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
                    pages_meta.append({
                        "page_idx": i,
                        "width": w,
                        "height": h,
                        "dpi": dpi_val
                    })
        else:
            with Image.open(fpath) as img:
                w, h = img.size
                dpi_val = 300
                dpi = img.info.get("dpi")
                if dpi and isinstance(dpi, tuple):
                    dpi_val = int(round(max(float(dpi[0]), float(dpi[1]))))
                pages_meta.append({
                    "page_idx": 0,
                    "width": w,
                    "height": h,
                    "dpi": dpi_val
                })
    except Exception as e:
        print(f"Error extracting page metadata for {fpath}: {e}")
    return pages_meta


class FilePageTreeBrowser(ctk.CTkFrame):
    """
    Reusable File System & Page Expansion Tree Browser widget.
    Used by both S2PIT (Preview/Tuning) and S2POT (Batch Processing & Output Inspection).
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
        on_file_selected=None,
        on_page_selected=None,
        on_page_activated=None,
        on_selection_changed=None,
        **kwargs
    ):
        super().__init__(master, **kwargs)

        self.current_dir = initial_dir or ""
        self.lang = lang
        self.selectmode = selectmode
        self.show_path_bar = show_path_bar
        self.show_filters = show_filters

        self.on_file_selected = on_file_selected
        self.on_page_selected = on_page_selected
        self.on_page_activated = on_page_activated
        self.on_selection_changed = on_selection_changed

        self.found_files = []
        self.file_to_item = {}
        self.item_to_info = {} # item_id -> {'type': 'file'|'page', 'path': str, 'page_idx': int}
        self.loaded_pages = set() # set of fpath loaded

        # Active tuning and metadata configs for folder
        self.tuning_config = None
        self.metadata_config = None

        # Columns definition
        self.columns = columns or ("Format", "Size", "Pages", "ImgRes", "Status")
        self.column_widths = column_widths or {
            "Format": 110,
            "Size": 85,
            "Pages": 75,
            "ImgRes": 85,
            "Chars": 90,
            "Status": 130,
            "Time": 70,
            "Tuning": 140
        }

        # Sorting state
        self.sort_column = None
        self.sort_reverse = False

        self._build_browser_ui()

        if self.current_dir and os.path.exists(self.current_dir):
            self.set_folder(self.current_dir)

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_browser_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2 if (self.show_path_bar or self.show_filters) else 0, weight=1)

        row_idx = 0

        # Optional Top Path Bar
        if self.show_path_bar:
            bar_frame = ctk.CTkFrame(self, fg_color="transparent")
            bar_frame.grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(6, 4))
            row_idx += 1

            self.lbl_path = ctk.CTkLabel(bar_frame, text=self._tr("input_folder"), font=ctk.CTkFont(size=12, weight="bold"))
            self.lbl_path.pack(side="left", padx=(0, 6))

            self.path_entry = ctk.CTkEntry(bar_frame, placeholder_text=self._tr("input_placeholder"))
            self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
            self.path_entry.bind("<Return>", lambda e: self.set_folder(self.path_entry.get().strip()))

            self.btn_browse = ctk.CTkButton(bar_frame, text=self._tr("browse"), width=75, command=self._browse_folder)
            self.btn_browse.pack(side="left", padx=(0, 6))

            self.btn_refresh = ctk.CTkButton(bar_frame, text=f"⟳ {self._tr('refresh')}", width=75, fg_color="#34495e", hover_color="#2c3e50", command=self.refresh)
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

            ctk.CTkCheckBox(filt_frame, text="PDF", variable=self.var_f_pdf, width=60, command=self.refresh).pack(side="left", padx=4)
            ctk.CTkCheckBox(filt_frame, text="TIFF", variable=self.var_f_tiff, width=60, command=self.refresh).pack(side="left", padx=4)
            ctk.CTkCheckBox(filt_frame, text="JPG", variable=self.var_f_jpg, width=60, command=self.refresh).pack(side="left", padx=4)
            ctk.CTkCheckBox(filt_frame, text="PNG", variable=self.var_f_png, width=60, command=self.refresh).pack(side="left", padx=4)

        # Treeview Container
        tree_container = ctk.CTkFrame(self, fg_color="transparent")
        tree_container.grid(row=row_idx, column=0, sticky="nsew", padx=8, pady=4)
        tree_container.grid_columnconfigure(0, weight=1)
        tree_container.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Browser.Treeview",
            background="#2b2b2b",
            foreground="white",
            fieldbackground="#2b2b2b",
            rowheight=25,
            font=("Segoe UI", 9)
        )
        style.configure("Browser.Treeview.Heading", background="#1f1f1f", foreground="white", font=("Segoe UI", 9, "bold"))
        style.map("Browser.Treeview", background=[("selected", "#1f618d")])

        # Treeview with hierarchy column #0 for expanding files into pages
        self.tree = ttk.Treeview(
            tree_container,
            columns=self.columns,
            show="tree headings",
            selectmode=self.selectmode,
            style="Browser.Treeview"
        )

        # Heading and Width for Tree column #0
        self.tree.heading("#0", text=self._tr("col_filename"), anchor="w", command=lambda: self._sort_by_column("#0"))
        self.tree.column("#0", width=280, minwidth=180, anchor="w")

        # Configured Columns
        col_names = {
            "Format": self._tr("col_format"),
            "Size": self._tr("col_size"),
            "Pages": self._tr("col_pages"),
            "ImgRes": self._tr("col_img_res"),
            "Chars": self._tr("col_chars"),
            "Status": self._tr("col_status"),
            "Time": self._tr("col_time"),
            "Tuning": "Tuning / Calibração",
            "DateModified": "Data Modificação"
        }

        for col in self.columns:
            header_text = col_names.get(col, col)
            self.tree.heading(col, text=header_text, command=lambda c=col: self._sort_by_column(c))
            w = self.column_widths.get(col, 80)
            anchor = "center"
            if col in ["Size", "Chars", "Time"]:
                anchor = "e"
            elif col in ["Tuning", "DateModified"]:
                anchor = "w"
            self.tree.column(col, width=w, anchor=anchor)

        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Events
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Return>", self._on_tree_double_click)

    def _browse_folder(self):
        f = filedialog.askdirectory(initialdir=self.current_dir or os.getcwd())
        if f:
            self.set_folder(f)

    def set_folder(self, folder_path):
        if not folder_path or not os.path.exists(folder_path):
            return
        self.current_dir = folder_path
        if hasattr(self, "path_entry"):
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, folder_path)
        self.refresh()

    def refresh(self):
        if not self.current_dir or not os.path.exists(self.current_dir):
            return

        # Load tuning config if present
        self.tuning_config = load_tuning_config(self.current_dir)

        # Load metadata config if present
        self.metadata_config = load_folder_metadata(self.current_dir)
        meta_files = self.metadata_config.get("files", {}) if self.metadata_config else {}

        # Clear existing
        self.tree.delete(*self.tree.get_children())
        self.found_files = []
        self.file_to_item = {}
        self.item_to_info = {}
        self.loaded_pages.clear()

        # Build patterns
        exts = []
        if not self.show_filters or self.var_f_pdf.get():
            exts.append("*.pdf")
        if not self.show_filters or self.var_f_tiff.get():
            exts.extend(["*.tif", "*.tiff"])
        if not self.show_filters or self.var_f_jpg.get():
            exts.extend(["*.jpg", "*.jpeg"])
        if not self.show_filters or self.var_f_png.get():
            exts.extend(["*.png", "*.bmp", "*.webp"])

        files = []
        for pattern in exts:
            files.extend(glob.glob(os.path.join(self.current_dir, pattern)))
            files.extend(glob.glob(os.path.join(self.current_dir, pattern.upper())))

        self.found_files = sorted(list(set(files)))

        for fpath in self.found_files:
            fname = os.path.basename(fpath)
            ext = os.path.splitext(fpath)[1].upper().replace(".", "")
            try:
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                size_str = f"{size_mb:.2f} MB"
            except Exception:
                size_str = "-"

            # Metadata resolution & page counts
            f_meta = meta_files.get(fname)
            inspected = f_meta is not None
            low_ext = os.path.splitext(fpath)[1].lower()

            if inspected:
                n_pages = f_meta.get("n_pages", 1)
                max_dpi = f_meta.get("max_dpi", 300)
                dpi_str = f"{max_dpi} DPI"
                total_chars = f_meta.get("total_chars", 0)
                chars_str = f"{total_chars:,}" if total_chars > 0 else "-"
            else:
                n_pages = 1
                try:
                    if low_ext == ".pdf":
                        with fitz.open(fpath) as doc:
                            n_pages = len(doc)
                    elif low_ext in [".tif", ".tiff"]:
                        with Image.open(fpath) as timg:
                            n_pages = getattr(timg, "n_frames", 1)
                except Exception:
                    pass
                dpi_str = "-"
                chars_str = "-"

            # Check tuning status
            tuning_str = "-"
            if self.tuning_config:
                eff = get_effective_file_tuning(self.tuning_config, fname)
                tuning_str = f"{eff.get('color_mode', 'color')} ({eff.get('target_dpi', 150)} DPI)"

            # Build values according to columns
            row_vals = []
            for col in self.columns:
                if col == "Format": row_vals.append(ext)
                elif col == "Size": row_vals.append(size_str)
                elif col == "Pages": row_vals.append(str(n_pages))
                elif col == "ImgRes": row_vals.append(dpi_str)
                elif col == "Chars": row_vals.append(chars_str)
                elif col == "Status": row_vals.append("Ready")
                elif col == "Time": row_vals.append("-")
                elif col == "Tuning": row_vals.append(tuning_str)
                elif col == "DateModified":
                    mtime = os.path.getmtime(fpath)
                    row_vals.append(datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"))
                else:
                    row_vals.append("-")

            # Insert parent file item with folder/file icon
            parent_id = self.tree.insert("", "end", text=f"📄 {fname}", values=tuple(row_vals), open=False)
            self.file_to_item[fpath] = parent_id
            self.item_to_info[parent_id] = {
                "type": "file",
                "path": fpath,
                "filename": fname,
                "n_pages": n_pages,
                "page_idx": 0,
                "inspected": inspected,
                "meta": f_meta
            }

            # If multi-page or PDF/TIFF, add a dummy child to enable the '+' expander
            if n_pages > 1 or low_ext in [".pdf", ".tif", ".tiff"]:
                dummy_id = f"{parent_id}_dummy"
                self.tree.insert(parent_id, "end", iid=dummy_id, text="Carregando páginas...", values=())

        if self.on_selection_changed:
            self.on_selection_changed([], [])

    def _on_tree_open(self, event):
        """Lazy load page details when user clicks expand (+) on a file node."""
        for parent_id in self.tree.get_children(""):
            if self.tree.item(parent_id, "open"):
                info = self.item_to_info.get(parent_id)
                if not info or info["type"] != "file":
                    continue
                fpath = info["path"]
                if fpath in self.loaded_pages:
                    continue

                # Remove dummy item
                for child in self.tree.get_children(parent_id):
                    if child.endswith("_dummy"):
                        self.tree.delete(child)

                fname = info["filename"]
                f_meta = info.get("meta")
                if not f_meta and self.metadata_config:
                    f_meta = self.metadata_config.get("files", {}).get(fname)

                if f_meta and "pages" in f_meta and len(f_meta["pages"]) > 0:
                    for pinfo in f_meta["pages"]:
                        pidx = pinfo.get("page_idx", 0)
                        dims_str = f"{pinfo.get('width', 0)} × {pinfo.get('height', 0)} px"
                        page_dpi = f"{pinfo.get('dpi', 300)} DPI"

                        tuning_page_str = "-"
                        if self.tuning_config:
                            eff_page = get_effective_file_tuning(self.tuning_config, fname, pidx)
                            tuning_page_str = f"{eff_page.get('color_mode', 'color')} ({eff_page.get('target_dpi', 150)} DPI)"

                        child_vals = []
                        for col in self.columns:
                            if col == "Format": child_vals.append(dims_str)
                            elif col == "Size": child_vals.append("-")
                            elif col == "Pages": child_vals.append(f"Pág {pidx + 1}")
                            elif col == "ImgRes": child_vals.append(page_dpi)
                            elif col == "Chars": child_vals.append("-")
                            elif col == "Status": child_vals.append("Ready")
                            elif col == "Time": child_vals.append("-")
                            elif col == "Tuning": child_vals.append(tuning_page_str)
                            elif col == "DateModified": child_vals.append("-")
                            else: child_vals.append("-")

                        child_id = self.tree.insert(
                            parent_id,
                            "end",
                            text=f"   ├── 📄 {self._tr('tree_item_page').format(idx=pidx + 1)}",
                            values=tuple(child_vals)
                        )
                        self.item_to_info[child_id] = {
                            "type": "page",
                            "path": fpath,
                            "filename": fname,
                            "page_idx": pidx,
                            "parent_id": parent_id
                        }
                else:
                    # NOT inspected yet: simply list pages without heavy extraction!
                    n_pages = info.get("n_pages", 1)
                    for pidx in range(n_pages):
                        tuning_page_str = "-"
                        if self.tuning_config:
                            eff_page = get_effective_file_tuning(self.tuning_config, fname, pidx)
                            tuning_page_str = f"{eff_page.get('color_mode', 'color')} ({eff_page.get('target_dpi', 150)} DPI)"

                        child_vals = []
                        for col in self.columns:
                            if col == "Format": child_vals.append("-")
                            elif col == "Size": child_vals.append("-")
                            elif col == "Pages": child_vals.append(f"Pág {pidx + 1}")
                            elif col == "ImgRes": child_vals.append("-")
                            elif col == "Chars": child_vals.append("-")
                            elif col == "Status": child_vals.append("Ready")
                            elif col == "Time": child_vals.append("-")
                            elif col == "Tuning": child_vals.append(tuning_page_str)
                            elif col == "DateModified": child_vals.append("-")
                            else: child_vals.append("-")

                        child_id = self.tree.insert(
                            parent_id,
                            "end",
                            text=f"   ├── 📄 {self._tr('tree_item_page').format(idx=pidx + 1)}",
                            values=tuple(child_vals)
                        )
                        self.item_to_info[child_id] = {
                            "type": "page",
                            "path": fpath,
                            "filename": fname,
                            "page_idx": pidx,
                            "parent_id": parent_id
                        }

                self.loaded_pages.add(fpath)

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
                if info["type"] == "file":
                    selected_files.append(info["path"])
                elif info["type"] == "page":
                    selected_pages.append((info["path"], info["page_idx"]))

        if self.on_selection_changed:
            self.on_selection_changed(selected_files, selected_pages)

        # Trigger single selection callbacks if 1 item selected
        if len(selected_ids) == 1:
            info = self.item_to_info.get(selected_ids[0])
            if info:
                if info["type"] == "file" and self.on_file_selected:
                    self.on_file_selected(info["path"])
                elif info["type"] == "page" and self.on_page_selected:
                    self.on_page_selected(info["path"], info["page_idx"])

    def _on_tree_double_click(self, event):
        selected_ids = self.tree.selection()
        if not selected_ids:
            return
        iid = selected_ids[0]
        info = self.item_to_info.get(iid)
        if not info:
            return

        if info["type"] == "page" and self.on_page_activated:
            self.on_page_activated(info["path"], info["page_idx"])
        elif info["type"] == "file":
            is_open = self.tree.item(iid, "open")
            self.tree.item(iid, open=not is_open)
            self._on_tree_open(None)
            if self.on_page_activated:
                self.on_page_activated(info["path"], 0)

    def get_selected_files(self):
        """Returns list of selected file paths (or all parent files if pages selected)."""
        selected_ids = self.tree.selection()
        files = []
        for iid in selected_ids:
            info = self.item_to_info.get(iid)
            if info:
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
        if not info:
            return None, 0
        return info["path"], info.get("page_idx", 0)

    def update_file_status(self, fpath, status=None, chars=None, time_s=None):
        """Updates row metadata for a specific file (e.g. during/after batch processing)."""
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
        """Sorts the file nodes by specified column."""
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
            v = str(tup[0]).replace("📄", "").strip()
            if col == "Size":
                try: return float(v.replace("MB", "").strip())
                except: return 0.0
            elif col in ["Pages", "ImgRes", "Chars"]:
                try: return int(v.replace("DPI", "").replace(",", "").strip())
                except: return 0
            elif col == "Time":
                try: return float(v.replace("s", "").strip())
                except: return 0.0
            return v.lower()

        items.sort(key=parse_key, reverse=self.sort_reverse)
        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)
