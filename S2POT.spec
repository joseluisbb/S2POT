# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import customtkinter
import rapidocr_onnxruntime

ctk_path = os.path.dirname(customtkinter.__file__)
rocr_path = os.path.dirname(rapidocr_onnxruntime.__file__)

block_cipher = None

datas = [
    (ctk_path, 'customtkinter'),
    (rocr_path, 'rapidocr_onnxruntime'),
    ('models/trocr_small', 'models/trocr_small'),
]

hiddenimports = [
    'customtkinter',
    'rapidocr_onnxruntime',
    'tokenizers',
    'pymupdf',
    'fitz',
    'cv2',
    'numpy',
    'PIL',
    'PIL.Image',
    'docx',
    'openpyxl',
    'core.config',
    'core.pipeline',
    'core.lite_pipeline',
    'core.i18n',
    'core.enhancer',
    'core.deskew',
    'core.ocr_engine',
    'core.htr_engine',
    'core.pdf_builder',
    'core.data_exporter',
    'core.tuning_config',
    'core.metadata_config',
    'core.ocr_text_editor',
    'gui.app_gui',
    'gui.lite_gui',
    'gui.file_browser',
    'gui.inspection_dialog',
    'gui.tooltip',
    'gui.s2pit_gui',
]

# 1. Main Suite Application (S2POT.exe)
a1 = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz1 = PYZ(a1.pure, a1.zipped_data, cipher=block_cipher)
exe1 = EXE(
    pyz1,
    a1.scripts,
    [],
    exclude_binaries=True,
    name='S2POT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# 2. Urgent Express Application (S2POTlite.exe)
a2 = Analysis(
    ['main_lite.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz2 = PYZ(a2.pure, a2.zipped_data, cipher=block_cipher)
exe2 = EXE(
    pyz2,
    a2.scripts,
    [],
    exclude_binaries=True,
    name='S2POTlite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Shared Distribution Directory (S2POT/ with S2POT.exe, S2POTlite.exe and shared _internal)
coll = COLLECT(
    exe1,
    exe2,
    a1.binaries + a2.binaries,
    a1.zipfiles + a2.zipfiles,
    a1.datas + a2.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='S2POT',
)
