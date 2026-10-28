"""
S2POT Lite GUI - Express OCR Interface (Urgent Mode)
Ultra-fast, single-window, high-efficiency interface for one-click OCR.
Supports visual file navigation, informative startup panel, and formats selection (_OCR.txt, _OCR.docx, _OCR.pdf).
"""

import os
import sys
import time
import threading
import subprocess
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

from core.i18n import get_text, LANGUAGES
from core.config import load_config, save_config, APP_VERSION
from core.lite_pipeline import inspect_lite_file, process_lite_file, SUPPORTED_EXTENSIONS

# Windows Native APIs setup
GWLP_WNDPROC = -4
WM_DROPFILES = 0x0233
GA_ROOT = 2

FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
SIGDN_FILESYSPATH = 0x80058000

ole32 = getattr(ctypes.windll, "ole32", None)
shell32 = getattr(ctypes.windll, "shell32", None)
user32 = getattr(ctypes.windll, "user32", None)
comctl32 = getattr(ctypes.windll, "comctl32", None)

try:
    MSGFLT_ADD = 1
    if user32:
        try:
            user32.ChangeWindowMessageFilter(WM_DROPFILES, MSGFLT_ADD)
            user32.ChangeWindowMessageFilter(0x0049, MSGFLT_ADD)
            user32.ChangeWindowMessageFilter(0x004A, MSGFLT_ADD)
        except Exception:
            pass

    DragAcceptFiles = shell32.DragAcceptFiles
    DragAcceptFiles.argtypes = [ctypes.c_void_p, wintypes.BOOL]

    DragQueryFileW = shell32.DragQueryFileW
    DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    DragQueryFileW.restype = wintypes.UINT

    DragFinish = shell32.DragFinish
    DragFinish.argtypes = [ctypes.c_void_p]

    SUBCLASSPROC = ctypes.WINFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )

    SetWindowSubclass = comctl32.SetWindowSubclass
    SetWindowSubclass.argtypes = [ctypes.c_void_p, SUBCLASSPROC, ctypes.c_void_p, ctypes.c_void_p]
    SetWindowSubclass.restype = wintypes.BOOL

    DefSubclassProc = comctl32.DefSubclassProc
    DefSubclassProc.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    DefSubclassProc.restype = ctypes.c_void_p

    RemoveWindowSubclass = comctl32.RemoveWindowSubclass
    RemoveWindowSubclass.argtypes = [ctypes.c_void_p, SUBCLASSPROC, ctypes.c_void_p]
    RemoveWindowSubclass.restype = wintypes.BOOL

    DND_SUPPORTED = True
except Exception:
    DND_SUPPORTED = False


class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', wintypes.BYTE * 8)
    ]

CLSID_FileOpenDialog = GUID(0xDC1C5A9C, 0xE88A, 0x4DDE, (wintypes.BYTE*8)(0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7))
IID_IFileOpenDialog = GUID(0xD57C7288, 0xD4AD, 0x4768, (wintypes.BYTE*8)(0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60))
IID_IShellItem = GUID(0x43826D1E, 0xE718, 0x42EE, (wintypes.BYTE*8)(0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE))


def pick_folder_showing_files_win32(parent_hwnd=None, title='Escolha a Pasta de Destino', initial_dir=None):
    """
    Opens the modern Windows Explorer dialog configured with FOS_PICKFOLDERS.
    Unlike standard Tkinter askdirectory, this dialog displays all existing files
    inside folders during navigation, preventing user confusion while picking a destination.
    """
    if ole32 is None:
        return None

    ole32.CoInitialize(None)
    pDialog = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_FileOpenDialog),
            None,
            1,
            ctypes.byref(IID_IFileOpenDialog),
            ctypes.byref(pDialog)
        )
        if hr != 0 or not pDialog.value:
            return None

        vtable = ctypes.cast(
            ctypes.cast(pDialog.value, ctypes.POINTER(ctypes.c_void_p)).contents.value,
            ctypes.POINTER(ctypes.c_void_p)
        )

        # GetOptions (10) & SetOptions (9)
        GetOptions = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))(vtable[10])
        SetOptions = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.DWORD)(vtable[9])
        opts = wintypes.DWORD()
        GetOptions(pDialog, ctypes.byref(opts))
        SetOptions(pDialog, opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)

        # SetTitle (17)
        if title:
            SetTitle = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[17])
            SetTitle(pDialog, title)

        # SetFolder (12)
        if initial_dir and os.path.exists(initial_dir):
            try:
                pInitItem = ctypes.c_void_p()
                sh_hr = shell32.SHCreateItemFromParsingName(
                    wintypes.LPCWSTR(os.path.abspath(initial_dir)),
                    None,
                    ctypes.byref(IID_IShellItem),
                    ctypes.byref(pInitItem)
                )
                if sh_hr == 0 and pInitItem.value:
                    SetFolder = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(vtable[12])
                    SetFolder(pDialog, pInitItem)
                    item_v = ctypes.cast(ctypes.cast(pInitItem.value, ctypes.POINTER(ctypes.c_void_p)).contents.value, ctypes.POINTER(ctypes.c_void_p))
                    ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(item_v[2])(pInitItem)
            except Exception:
                pass

        # Show (3)
        Show = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(vtable[3])
        show_res = Show(pDialog, parent_hwnd)
        if show_res != 0:
            return None

        # GetResult (20)
        pItem = ctypes.c_void_p()
        GetResult = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(vtable[20])
        if GetResult(pDialog, ctypes.byref(pItem)) == 0 and pItem.value:
            item_v = ctypes.cast(ctypes.cast(pItem.value, ctypes.POINTER(ctypes.c_void_p)).contents.value, ctypes.POINTER(ctypes.c_void_p))
            GetDisplayName = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(item_v[5])
            pStr = ctypes.c_void_p()
            chosen_path = None
            if GetDisplayName(pItem, SIGDN_FILESYSPATH, ctypes.byref(pStr)) == 0 and pStr.value:
                chosen_path = ctypes.wstring_at(pStr.value)
                ole32.CoTaskMemFree(pStr)
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(item_v[2])(pItem)
            return chosen_path

        return None
    except Exception:
        return None
    finally:
        if pDialog.value:
            try:
                vtable = ctypes.cast(ctypes.cast(pDialog.value, ctypes.POINTER(ctypes.c_void_p)).contents.value, ctypes.POINTER(ctypes.c_void_p))
                ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(pDialog)
            except Exception:
                pass
        ole32.CoUninitialize()


class S2POTLiteApp(ctk.CTk):
    """
    S2POT Lite Standalone Express Application.
    Designed for maximum productivity, minimal clicks, and instant OCR execution.
    """
    def __init__(self, initial_file=None):
        super().__init__()

        cfg = load_config() or {}
        self.lang = cfg.get("language", "pt_PT")
        self.ocr_lang = cfg.get("ocr_lang", "por")
        self.doc_type = cfg.get("doc_type", "printed")

        self.title(f"{self._tr('lite_title')} v{APP_VERSION}")
        # Compact and balanced initial geometry
        self.geometry("740x660")
        self.minsize(700, 560)

        # State
        self.selected_file = None
        self.file_info = None
        self.manual_output_dir = None
        self.is_processing = False
        self.abort_requested = False
        self.last_results = None
        self.details_expanded = False

        self._hooked_hwnds = set()
        self._dnd_cb = None

        # Build UI Components
        self._build_ui()

        # Handle window close cleanly
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Global paste shortcut to load copied file or path
        self.bind("<Control-v>", self._on_paste_shortcut)

        # Install Native Drag & Drop Hook safely on Windows
        self._install_dnd_hook()

        # Handle command-line arguments or pre-selected file
        if initial_file and os.path.exists(initial_file):
            self.after(200, lambda: self.load_file(initial_file))
        elif len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
            self.after(200, lambda: self.load_file(sys.argv[1]))

    def _tr(self, key):
        return get_text(key, self.lang)

    def _build_ui(self):
        # 1. Header Frame
        hdr = ctk.CTkFrame(self, fg_color=("gray92", "#1a1a1a"), corner_radius=0, height=52)
        hdr.pack(side="top", fill="x")

        title_box = ctk.CTkFrame(hdr, fg_color="transparent")
        title_box.pack(side="left", padx=16, pady=6)

        lbl_app = ctk.CTkLabel(
            title_box,
            text=f"⚡ {self._tr('lite_title')}",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color="#e67e22"
        )
        lbl_app.pack(anchor="w")

        lbl_sub = ctk.CTkLabel(
            title_box,
            text=self._tr("lite_subtitle"),
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray70")
        )
        lbl_sub.pack(anchor="w")

        # UI Language dropdown only on header
        lang_box = ctk.CTkFrame(hdr, fg_color="transparent")
        lang_box.pack(side="right", padx=14, pady=8)

        ctk.CTkLabel(lang_box, text="🌐 UI:", font=ctk.CTkFont(size=11, weight="bold")).pack(side="left", padx=(0, 4))
        self.cmb_ui_lang = ctk.CTkOptionMenu(
            lang_box,
            values=list(LANGUAGES.values()),
            width=125,
            height=26,
            font=ctk.CTkFont(size=11),
            command=self._on_ui_lang_change
        )
        self.cmb_ui_lang.set(LANGUAGES.get(self.lang, "Português (PT)"))
        self.cmb_ui_lang.pack(side="left")

        # 2. Main Scrollable Container
        self.main_scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.main_scroll.pack(fill="both", expand=True, padx=14, pady=8)

        # === SECTION A: FILE DROP ZONE & DOCUMENT OCR LANGUAGE ===
        self.drop_frame = ctk.CTkFrame(
            self.main_scroll,
            fg_color=("gray95", "#202020"),
            border_width=2,
            border_color=("#3498db", "#2980b9"),
            corner_radius=10,
            cursor="hand2"
        )
        self.drop_frame.pack(fill="x", padx=4, pady=(2, 6))
        self.drop_frame.bind("<Button-1>", lambda e: self._browse_file())

        self.drop_content = ctk.CTkFrame(self.drop_frame, fg_color="transparent", cursor="hand2")
        self.drop_content.pack(fill="x", padx=14, pady=10)
        self.drop_content.bind("<Button-1>", lambda e: self._browse_file())

        self.lbl_drop_icon = ctk.CTkLabel(
            self.drop_content,
            text="📥",
            font=ctk.CTkFont(size=26),
            cursor="hand2"
        )
        self.lbl_drop_icon.pack(side="top", pady=(0, 1))
        self.lbl_drop_icon.bind("<Button-1>", lambda e: self._browse_file())

        self.lbl_drop_text = ctk.CTkLabel(
            self.drop_content,
            text=self._tr("lite_drop_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=("#2980b9", "#3498db"),
            cursor="hand2"
        )
        self.lbl_drop_text.pack(side="top", pady=1)
        self.lbl_drop_text.bind("<Button-1>", lambda e: self._browse_file())

        self.lbl_drop_sub = ctk.CTkLabel(
            self.drop_content,
            text=self._tr("lite_drop_sub"),
            font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray65"),
            cursor="hand2"
        )
        self.lbl_drop_sub.pack(side="top", pady=(1, 4))
        self.lbl_drop_sub.bind("<Button-1>", lambda e: self._browse_file())

        self.btn_browse = ctk.CTkButton(
            self.drop_content,
            text="📂 Procurar Ficheiro no Computador...",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#3498db",
            hover_color="#2980b9",
            height=28,
            command=self._browse_file
        )
        self.btn_browse.pack(side="top", pady=(2, 0))

        # Selected File Details Card (hidden initially)
        self.file_card = ctk.CTkFrame(self.drop_frame, fg_color=("gray90", "#262626"), corner_radius=8)

        # Contextual Document OCR Language & Document Type selector
        self.doc_lang_frame = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        self.doc_lang_frame.pack(fill="x", padx=6, pady=(2, 4))

        lbl_doc_lang = ctk.CTkLabel(
            self.doc_lang_frame,
            text="🌐 Idioma:",
            font=ctk.CTkFont(size=12, weight="bold")
        )
        lbl_doc_lang.pack(side="left", padx=(2, 6))

        self.ocr_lang_map = {
            "Português": "por",
            "English": "eng",
            "Español": "spa",
            "Français": "fra",
            "Deutsch": "deu"
        }
        self.cmb_ocr_lang = ctk.CTkOptionMenu(
            self.doc_lang_frame,
            values=list(self.ocr_lang_map.keys()),
            width=115,
            height=26,
            font=ctk.CTkFont(size=12),
            command=self._on_ocr_lang_change
        )
        curr_ocr_label = "Português"
        for k, v in self.ocr_lang_map.items():
            if v == self.ocr_lang:
                curr_ocr_label = k
                break
        self.cmb_ocr_lang.set(curr_ocr_label)
        self.cmb_ocr_lang.pack(side="left", padx=(0, 24))

        # Document Type Checkbox (Handwriting / Cursive with TrOCR)
        self.var_handwritten = ctk.BooleanVar(value=(self.doc_type == "handwritten"))
        self.chk_handwritten = ctk.CTkCheckBox(
            self.doc_lang_frame,
            text=self._tr("doc_type_handwritten"),
            variable=self.var_handwritten,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#8e44ad", "#a569bd"),
            command=self._on_handwritten_checkbox_toggle
        )
        self.chk_handwritten.pack(side="left")

        # === SECTION B: OUTPUT FOLDER WITH VISIBLE FILES ===
        out_box = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        out_box.pack(fill="x", padx=4, pady=3)

        lbl_out = ctk.CTkLabel(out_box, text=self._tr("lite_output_folder"), font=ctk.CTkFont(size=12, weight="bold"))
        lbl_out.pack(anchor="w", padx=2, pady=(0, 1))

        out_row = ctk.CTkFrame(out_box, fg_color="transparent")
        out_row.pack(fill="x")

        self.entry_out_dir = ctk.CTkEntry(out_row, font=ctk.CTkFont(size=12), height=30)
        self.entry_out_dir.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_change_out = ctk.CTkButton(
            out_row,
            text="📁 Procurar Pasta...",
            width=120,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._choose_output_dir
        )
        self.btn_change_out.pack(side="right")

        # === SECTION C: DESIRED OUTPUT FORMATS (compact horizontal row) ===
        fmt_box = ctk.CTkFrame(self.main_scroll, fg_color=("gray95", "#202020"), corner_radius=8)
        fmt_box.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            fmt_box,
            text=f"⚙️ {self._tr('lite_formats_title')}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#2c3e50", "#ecf0f1")
        ).pack(anchor="w", padx=12, pady=(6, 2))

        fmt_row = ctk.CTkFrame(fmt_box, fg_color="transparent")
        fmt_row.pack(fill="x", padx=12, pady=(2, 6))

        self.var_opt_txt = ctk.BooleanVar(value=True)
        self.var_opt_docx = ctk.BooleanVar(value=True)
        self.var_opt_pdf = ctk.BooleanVar(value=True)

        self.chk_txt = ctk.CTkCheckBox(
            fmt_row,
            text=self._tr("lite_opt_txt"),
            variable=self.var_opt_txt,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#27ae60", "#2ecc71")
        )
        self.chk_txt.pack(side="left", padx=(0, 18))

        self.chk_docx = ctk.CTkCheckBox(
            fmt_row,
            text=self._tr("lite_opt_docx"),
            variable=self.var_opt_docx,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#2980b9", "#3498db")
        )
        self.chk_docx.pack(side="left", padx=(0, 18))

        self.chk_pdf = ctk.CTkCheckBox(
            fmt_row,
            text=self._tr("lite_opt_pdf"),
            variable=self.var_opt_pdf,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#d35400", "#e67e22")
        )
        self.chk_pdf.pack(side="left")

        # === SECTION D: ACTION & PROGRESS BAR ===
        self.action_box = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        self.action_box.pack(fill="x", padx=4, pady=6)

        self.btn_run = ctk.CTkButton(
            self.action_box,
            text=self._tr("lite_btn_start"),
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#27ae60",
            hover_color="#1e8449",
            command=self._on_click_run_or_cancel
        )
        self.btn_run.pack(fill="x", pady=(2, 3))

        self.progress_bar = ctk.CTkProgressBar(self.action_box, height=12)
        self.progress_bar.set(0.0)

        self.lbl_progress_status = ctk.CTkLabel(
            self.action_box,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=("#16a085", "#1abc9c")
        )

        # === SECTION E: WELCOME & STARTUP INFORMATIVE PANEL ===
        self.welcome_card = ctk.CTkFrame(self.main_scroll, fg_color=("gray90", "#242424"), corner_radius=8)
        self.welcome_card.pack(fill="x", padx=4, pady=6)

        ctk.CTkLabel(
            self.welcome_card,
            text="💡 Como Funciona o S2POT Lite (Modo Urgente):",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#2c3e50", "#f39c12")
        ).pack(anchor="w", padx=14, pady=(10, 6))

        instructions = [
            ("1", "Ficheiro:", "Arraste e solte o ficheiro na caixa acima ou clique para procurar (PDF, TIFF multipágina, JPG, PNG)."),
            ("2", "Destino & Formatos:", "A pasta assume automaticamente a mesma do ficheiro. Selecione se pretende Texto, Word e/ou PDF."),
            ("3", "1-Clique Expresso:", "Clique em 'Executar OCR Expresso'. Os novos ficheiros são gravados com o sufixo '_OCR'.")
        ]

        for num, title, desc in instructions:
            step_row = ctk.CTkFrame(self.welcome_card, fg_color="transparent")
            step_row.pack(fill="x", padx=14, pady=3)

            badge = ctk.CTkLabel(
                step_row,
                text=num,
                width=22,
                height=22,
                corner_radius=11,
                fg_color=("#3498db", "#2980b9"),
                text_color="white",
                font=ctk.CTkFont(size=11, weight="bold")
            )
            badge.pack(side="left", padx=(0, 8))

            ctk.CTkLabel(
                step_row,
                text=title,
                width=135,
                font=ctk.CTkFont(size=11, weight="bold"),
                anchor="w",
                text_color=("#1f618d", "#5dade2")
            ).pack(side="left")

            ctk.CTkLabel(
                step_row,
                text=desc,
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray70"),
                anchor="w"
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            self.welcome_card,
            text="⚡ Após a conclusão, este espaço dará lugar ao resumo estatístico com botões para abrir os ficheiros e copiar o texto.",
            font=ctk.CTkFont(size=10, slant="italic"),
            text_color=("gray50", "gray60")
        ).pack(anchor="w", padx=14, pady=(6, 10))

        # === SECTION F: QUANTITATIVE SUMMARY CARD & QUICK ACTIONS (hidden initially) ===
        self.summary_box = ctk.CTkFrame(self.main_scroll, fg_color=("gray90", "#242424"), corner_radius=8)

    def _install_dnd_hook(self):
        """Attaches Win32 DragAcceptFiles and SetWindowSubclass across the window hierarchy."""
        if not DND_SUPPORTED or not comctl32:
            return

        def dnd_proc(hwnd, msg, wp, lp, uid, ref):
            if msg == WM_DROPFILES:
                try:
                    hdrop = wp
                    count = DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    paths = []
                    for i in range(count):
                        length = DragQueryFileW(hdrop, i, None, 0)
                        if length > 0:
                            buf = ctypes.create_unicode_buffer(length + 1)
                            DragQueryFileW(hdrop, i, buf, length + 1)
                            if buf.value:
                                paths.append(buf.value)
                    DragFinish(hdrop)
                    if paths:
                        self.after(20, lambda p=paths[0]: self.load_file(p))
                    return 0
                except Exception:
                    return 0
            return DefSubclassProc(hwnd, msg, wp, lp)

        # Store persistent reference to keep callback alive
        self._dnd_cb = SUBCLASSPROC(dnd_proc)
        self._hooked_hwnds = set()
        self.after(150, self._apply_dnd_to_all_windows)

    def _apply_dnd_to_all_windows(self):
        """Finds all HWNDs in this window hierarchy and hooks drop events with SetWindowSubclass."""
        if not hasattr(self, "_dnd_cb") or not self._dnd_cb or not comctl32:
            return
        try:
            self.update_idletasks()
            raw_id = self.winfo_id()
            top_id = user32.GetAncestor(raw_id, GA_ROOT) or raw_id

            hwnds = set()
            if top_id:
                hwnds.add(top_id)
            if raw_id:
                hwnds.add(raw_id)

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)
            def enum_cb(h, lp):
                if h:
                    hwnds.add(h)
                return True

            enum_func = WNDENUMPROC(enum_cb)
            if top_id:
                user32.EnumChildWindows(top_id, enum_func, 0)

            for idx, h in enumerate(hwnds):
                if h not in self._hooked_hwnds:
                    try:
                        DragAcceptFiles(h, True)
                        uid = ctypes.c_void_p(1000 + len(self._hooked_hwnds) + 1)
                        if SetWindowSubclass(h, self._dnd_cb, uid, ctypes.c_void_p(0)):
                            self._hooked_hwnds.add(h)
                    except Exception:
                        pass
        except Exception as e:
            print(f"DND hook setup notice: {e}")

    def _on_window_close(self):
        """Cleanly remove window subclasses before destruction."""
        if hasattr(self, "_hooked_hwnds") and hasattr(self, "_dnd_cb") and self._dnd_cb and comctl32:
            for idx, h in enumerate(list(self._hooked_hwnds)):
                try:
                    DragAcceptFiles(h, False)
                    RemoveWindowSubclass(h, self._dnd_cb, ctypes.c_void_p(1000 + idx + 1))
                except Exception:
                    pass
            self._hooked_hwnds.clear()
            self._dnd_cb = None
        self.destroy()

    def _on_paste_shortcut(self, event=None):
        """Allows pasting a copied file or file path directly using Ctrl+V."""
        try:
            txt = self.clipboard_get().strip().strip('"\'')
            if txt and os.path.exists(txt):
                self.load_file(txt)
        except Exception:
            pass

    def load_file(self, file_path):
        """Loads and inspects candidate file, updating the UI drop card and default destination."""
        if not file_path or not os.path.exists(file_path):
            return

        info = inspect_lite_file(file_path)
        if not info["valid"]:
            messagebox.showerror("Ficheiro Inválido", info.get("error", "Erro ao carregar ficheiro."), parent=self)
            return

        self.selected_file = file_path
        self.file_info = info

        # Auto-fill output directory if not manually customized
        file_dir = os.path.dirname(os.path.abspath(file_path))
        if not self.manual_output_dir or not self.entry_out_dir.get().strip():
            self.entry_out_dir.delete(0, "end")
            self.entry_out_dir.insert(0, file_dir)

        # Update File Card Display
        self.drop_content.pack_forget()

        for w in self.file_card.winfo_children():
            w.destroy()

        card_row = ctk.CTkFrame(self.file_card, fg_color="transparent")
        card_row.pack(fill="x", padx=12, pady=8)

        icon_str = "📄" if info["ext"] == ".pdf" else "🖼️"
        lbl_icon = ctk.CTkLabel(card_row, text=icon_str, font=ctk.CTkFont(size=26))
        lbl_icon.pack(side="left", padx=(0, 10))

        meta_box = ctk.CTkFrame(card_row, fg_color="transparent")
        meta_box.pack(side="left", fill="x", expand=True)

        lbl_fname = ctk.CTkLabel(
            meta_box,
            text=info["filename"],
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#1f618d", "#3498db")
        )
        lbl_fname.pack(anchor="w")

        lbl_fsum = ctk.CTkLabel(
            meta_box,
            text=info["summary"],
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray70")
        )
        lbl_fsum.pack(anchor="w")

        btn_rem = ctk.CTkButton(
            card_row,
            text=self._tr("lite_change_file"),
            width=110,
            height=26,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#7f8c8d",
            hover_color="#95a5a6",
            command=self._clear_selected_file
        )
        btn_rem.pack(side="right", padx=(8, 0))

        self.file_card.pack(fill="x", padx=4, pady=4)
        self.after(100, self._apply_dnd_to_all_windows)

    def _clear_selected_file(self):
        self.selected_file = None
        self.file_info = None
        self.file_card.pack_forget()
        self.drop_content.pack(fill="x", padx=14, pady=10)
        self.summary_box.pack_forget()
        self.welcome_card.pack(fill="x", padx=4, pady=4)
        self.after(100, self._apply_dnd_to_all_windows)

    def _browse_file(self):
        file_path = filedialog.askopenfilename(
            parent=self,
            title="Escolha o Ficheiro para OCR",
            filetypes=[
                ("Ficheiros Suportados", "*.pdf;*.tif;*.tiff;*.jpg;*.jpeg;*.png;*.bmp;*.webp"),
                ("Documentos PDF", "*.pdf"),
                ("Imagens TIFF", "*.tif;*.tiff"),
                ("Imagens JPEG / PNG / BMP", "*.jpg;*.jpeg;*.png;*.bmp;*.webp"),
                ("Todos os Ficheiros", "*.*")
            ]
        )
        if file_path:
            self.load_file(file_path)

    def _choose_output_dir(self):
        cur = self.entry_out_dir.get().strip() or (os.path.dirname(self.selected_file) if self.selected_file else "")
        parent_h = self._top_hwnd or self.winfo_id()

        # Try native modern Windows dialog that reveals existing files inside folders
        d = pick_folder_showing_files_win32(parent_hwnd=parent_h, title="Escolha a Pasta de Destino dos Resultados", initial_dir=cur)

        # Fallback to standard askdirectory if native picker is not available or returned None
        if not d:
            d = filedialog.askdirectory(parent=self, title="Escolha a Pasta para os Resultados", initialdir=cur)

        if d and os.path.exists(d):
            self.manual_output_dir = d
            self.entry_out_dir.delete(0, "end")
            self.entry_out_dir.insert(0, d)

    def _on_ocr_lang_change(self, choice):
        self.ocr_lang = self.ocr_lang_map.get(choice, "por")
        cfg = load_config() or {}
        cfg["ocr_lang"] = self.ocr_lang
        save_config(cfg)

    def _on_handwritten_checkbox_toggle(self):
        self.doc_type = "handwritten" if self.var_handwritten.get() else "printed"
        cfg = load_config() or {}
        cfg["doc_type"] = self.doc_type
        save_config(cfg)

    def _on_ui_lang_change(self, choice):
        code = "pt_PT"
        for k, v in LANGUAGES.items():
            if v == choice:
                code = k
                break
        self.lang = code
        cfg = load_config() or {}
        cfg["language"] = code
        save_config(cfg)
        self._retranslate_ui()

    def _retranslate_ui(self):
        self.title(f"{self._tr('lite_title')} v{APP_VERSION}")
        self.lbl_drop_text.configure(text=self._tr("lite_drop_title"))
        self.lbl_drop_sub.configure(text=self._tr("lite_drop_sub"))
        self.chk_txt.configure(text=self._tr("lite_opt_txt"))
        self.chk_docx.configure(text=self._tr("lite_opt_docx"))
        self.chk_pdf.configure(text=self._tr("lite_opt_pdf"))
        self.chk_handwritten.configure(text=self._tr("doc_type_handwritten"))
        if not self.is_processing:
            self.btn_run.configure(text=self._tr("lite_btn_start"))

    def _on_click_run_or_cancel(self):
        if self.is_processing:
            self.abort_requested = True
            self.lbl_progress_status.configure(text="A cancelar...")
            return

        # Validate inputs
        if not self.selected_file or not os.path.exists(self.selected_file):
            messagebox.showwarning("Aviso", "Por favor, indique ou selecione um ficheiro de entrada válido.", parent=self)
            return

        out_dir = self.entry_out_dir.get().strip()
        if not out_dir:
            out_dir = os.path.dirname(self.selected_file)
            self.entry_out_dir.insert(0, out_dir)

        do_txt = self.var_opt_txt.get()
        do_docx = self.var_opt_docx.get()
        do_pdf = self.var_opt_pdf.get()

        if not (do_txt or do_docx or do_pdf):
            messagebox.showwarning("Aviso", "Selecione pelo menos um formato de saída pretendido (.txt, .docx ou .pdf).", parent=self)
            return

        # Start Processing Thread
        self.is_processing = True
        self.abort_requested = False
        self.welcome_card.pack_forget()
        self.summary_box.pack_forget()

        self.btn_run.configure(
            text=self._tr("lite_btn_cancel"),
            fg_color="#c0392b",
            hover_color="#962d22"
        )
        self.progress_bar.pack(fill="x", pady=(2, 4))
        self.progress_bar.set(0.0)
        self.lbl_progress_status.pack(anchor="w", pady=(0, 4))
        self.lbl_progress_status.configure(text="A inicializar motor OCR...")

        t = threading.Thread(
            target=self._run_processing_worker,
            args=(self.selected_file, out_dir, do_txt, do_docx, do_pdf, self.ocr_lang, self.doc_type),
            daemon=True
        )
        t.start()

    def _run_processing_worker(self, file_path, out_dir, do_txt, do_docx, do_pdf, ocr_lang, doc_type):
        def _prog(cur, total, msg):
            pct = float(cur) / float(total) if total > 0 else 0.0
            self.after(0, lambda: self._update_progress_ui(pct, msg))

        def _abort():
            return self.abort_requested

        try:
            res = process_lite_file(
                file_path=file_path,
                output_dir=out_dir,
                export_txt=do_txt,
                export_docx=do_docx,
                export_pdf=do_pdf,
                target_pdf_dpi=120,
                ocr_lang=ocr_lang,
                doc_type=doc_type,
                progress_callback=_prog,
                abort_check_fn=_abort
            )
            self.after(0, lambda: self._on_processing_complete(res))
        except InterruptedError:
            self.after(0, lambda: self._on_processing_cancelled())
        except Exception as e:
            self.after(0, lambda: self._on_processing_error(str(e)))

    def _update_progress_ui(self, pct, msg):
        self.progress_bar.set(pct)
        self.lbl_progress_status.configure(text=msg)

    def _on_processing_cancelled(self):
        self.is_processing = False
        self.btn_run.configure(text=self._tr("lite_btn_start"), fg_color="#27ae60", hover_color="#1e8449")
        self.progress_bar.pack_forget()
        self.lbl_progress_status.configure(text="Processamento cancelado.")
        self.welcome_card.pack(fill="x", padx=4, pady=6)
        messagebox.showinfo("Cancelado", "O processamento foi interrompido.", parent=self)

    def _on_processing_error(self, err_msg):
        self.is_processing = False
        self.btn_run.configure(text=self._tr("lite_btn_start"), fg_color="#27ae60", hover_color="#1e8449")
        self.progress_bar.pack_forget()
        self.lbl_progress_status.configure(text="Erro durante o processamento.")
        self.welcome_card.pack(fill="x", padx=4, pady=6)
        messagebox.showerror("Erro de Processamento", f"Ocorreu um erro ao processar o OCR:\n\n{err_msg}", parent=self)

    def _on_processing_complete(self, res):
        self.is_processing = False
        self.last_results = res

        self.btn_run.configure(text=self._tr("lite_btn_start"), fg_color="#27ae60", hover_color="#1e8449")
        self.progress_bar.pack_forget()
        self.lbl_progress_status.pack_forget()

        # Render Quantitative Summary
        self._render_summary_card(res)

    def _render_summary_card(self, res):
        for w in self.summary_box.winfo_children():
            w.destroy()

        self.summary_box.pack(fill="x", padx=4, pady=4)

        # Single consolidated metrics box: ALL 5 indicators on the SAME line in a condensed card
        metrics_box = ctk.CTkFrame(self.summary_box, fg_color=("gray95", "#1e1e1e"), corner_radius=8)
        metrics_box.pack(fill="x", padx=8, pady=(6, 4))

        m_row = ctk.CTkFrame(metrics_box, fg_color="transparent")
        m_row.pack(fill="x", padx=10, pady=8)

        # 5 columns distributed evenly
        for c in range(5):
            m_row.grid_columnconfigure(c, weight=1)

        conf = res["avg_confidence"]
        badge_col = "#27ae60" if conf >= 80 else ("#f39c12" if conf >= 65 else "#e74c3c")
        qual_icon = "🟢" if conf >= 80 else ("⚠️" if conf >= 65 else "🔴")

        items = [
            ("📄 Páginas:", f"{res['total_pages']}", "#2980b9"),
            ("🔤 Caracteres:", f"{res['total_chars']:,}", "#27ae60"),
            ("📝 Palavras:", f"{res['total_words']:,}", "#8e44ad"),
            ("⏱️ Tempo:", f"{res['elapsed_time']}s", "#d35400"),
            (f"{qual_icon} Qualidade:", f"{conf:.1f}%", badge_col)
        ]

        for idx, (label, val, col) in enumerate(items):
            cell = ctk.CTkFrame(m_row, fg_color="transparent")
            cell.grid(row=0, column=idx, sticky="ew", padx=2)

            box = ctk.CTkFrame(cell, fg_color="transparent")
            box.pack(anchor="center")

            ctk.CTkLabel(
                box,
                text=label,
                font=ctk.CTkFont(size=11),
                text_color=("gray45", "gray65")
            ).pack(side="left", padx=(0, 3))

            ctk.CTkLabel(
                box,
                text=val,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=col
            ).pack(side="left")

        # Created Files List (compact rows)
        files_frame = ctk.CTkFrame(self.summary_box, fg_color="transparent")
        files_frame.pack(fill="x", padx=8, pady=(2, 4))

        for f_item in res.get("created_files", []):
            f_row = ctk.CTkFrame(files_frame, fg_color=("gray95", "#282828"), corner_radius=6)
            f_row.pack(fill="x", pady=2)

            f_icon = "📄" if f_item["type"] == "txt" else ("📝" if f_item["type"] == "docx" else "📕")
            ctk.CTkLabel(
                f_row,
                text=f"{f_icon} {f_item['name']}",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=("#1f618d", "#5dade2")
            ).pack(side="left", padx=8, pady=3)

            ctk.CTkLabel(
                f_row,
                text=f"({f_item['size_kb']} KB)",
                font=ctk.CTkFont(size=11),
                text_color=("gray50", "gray70")
            ).pack(side="left", padx=4)

            btn_open = ctk.CTkButton(
                f_row,
                text="Abrir",
                width=55,
                height=22,
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda p=f_item["path"]: self._open_file(p)
            )
            btn_open.pack(side="right", padx=6, pady=3)

        # 1-Click Action Buttons
        btn_action_row = ctk.CTkFrame(self.summary_box, fg_color="transparent")
        btn_action_row.pack(fill="x", padx=8, pady=(4, 6))

        btn_folder = ctk.CTkButton(
            btn_action_row,
            text="📁 Abrir Pasta de Destino",
            height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#2980b9", "#1f618d"),
            command=lambda: self._open_output_folder(res["output_dir"], res.get("created_files"))
        )
        btn_folder.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.btn_copy = ctk.CTkButton(
            btn_action_row,
            text=self._tr("lite_btn_copy_text"),
            height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#8e44ad",
            hover_color="#732d91",
            command=lambda: self._copy_text_to_clipboard(res.get("full_text", ""))
        )
        self.btn_copy.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Re-apply drag & drop acceptance to any new frames
        self.after(100, self._apply_dnd_to_all_windows)

        # Page-by-Page Toggle
        if res.get("pages_stats") and len(res["pages_stats"]) > 1:
            self.details_expanded = False
            self.btn_toggle = ctk.CTkButton(
                self.summary_box,
                text=f"▼ {self._tr('lite_btn_toggle_details')} ({res['total_pages']} págs)",
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color="transparent",
                text_color=("#2980b9", "#3498db"),
                hover_color=("gray85", "#303030"),
                command=lambda: self._toggle_page_details(res["pages_stats"])
            )
            self.btn_toggle.pack(pady=(0, 6))

            self.page_details_frame = ctk.CTkFrame(self.summary_box, fg_color=("gray95", "#1c1c1c"))

    def _toggle_page_details(self, pages_stats):
        self.details_expanded = not self.details_expanded
        if self.details_expanded:
            self.btn_toggle.configure(text="▲ Ocultar Detalhe por Página")
            for w in self.page_details_frame.winfo_children():
                w.destroy()

            # Header row
            hdr_f = ctk.CTkFrame(self.page_details_frame, fg_color="transparent")
            hdr_f.pack(fill="x", padx=6, pady=2)
            ctk.CTkLabel(hdr_f, text="Página", width=60, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(hdr_f, text="Caracteres", width=80, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(hdr_f, text="Palavras", width=70, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(hdr_f, text="Qualidade", width=90, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(hdr_f, text="Esquema Cores", width=90, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(hdr_f, text="Tempo", width=60, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")

            for p in pages_stats:
                row = ctk.CTkFrame(self.page_details_frame, fg_color="transparent")
                row.pack(fill="x", padx=6, pady=1)

                q_badge = f"{p['confidence']:.0f}%"
                q_col = "#27ae60" if p['confidence'] >= 80 else ("#f39c12" if p['confidence'] >= 65 else "#e74c3c")

                ctk.CTkLabel(row, text=f"Pág {p['page_num']}", width=60, font=ctk.CTkFont(size=11)).pack(side="left")
                ctk.CTkLabel(row, text=f"{p['chars']:,}", width=80, font=ctk.CTkFont(size=11)).pack(side="left")
                ctk.CTkLabel(row, text=f"{p['words']:,}", width=70, font=ctk.CTkFont(size=11)).pack(side="left")
                ctk.CTkLabel(row, text=q_badge, width=90, font=ctk.CTkFont(size=11, weight="bold"), text_color=q_col).pack(side="left")
                ctk.CTkLabel(row, text=p['color_mode'].capitalize(), width=90, font=ctk.CTkFont(size=11)).pack(side="left")
                ctk.CTkLabel(row, text=f"{p['time_s']}s", width=60, font=ctk.CTkFont(size=11)).pack(side="left")

            self.page_details_frame.pack(fill="x", padx=10, pady=(0, 10))
        else:
            self.btn_toggle.configure(text=f"▼ {self._tr('lite_btn_toggle_details')}")
            self.page_details_frame.pack_forget()

    def _open_file(self, path):
        if path and os.path.exists(path):
            try:
                os.startfile(os.path.abspath(path))
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível abrir o ficheiro:\n{e}", parent=self)

    def _open_output_folder(self, folder_path, created_files):
        if not folder_path or not os.path.exists(folder_path):
            return
        try:
            if created_files and len(created_files) > 0 and os.path.exists(created_files[0]["path"]):
                target = os.path.abspath(created_files[0]["path"])
                subprocess.Popen(f'explorer /select,"{target}"')
            else:
                os.startfile(os.path.abspath(folder_path))
        except Exception:
            try:
                os.startfile(os.path.abspath(folder_path))
            except Exception:
                pass

    def _copy_text_to_clipboard(self, full_text):
        if not full_text:
            return
        self.clipboard_clear()
        self.clipboard_append(full_text)
        self.btn_copy.configure(text=self._tr("lite_copied_success"), fg_color="#27ae60")
        self.after(2000, lambda: self.btn_copy.configure(text=self._tr("lite_btn_copy_text"), fg_color="#8e44ad"))
