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
    'core.config',
    'core.i18n',
    'core.enhancer',
    'core.deskew',
    'core.ocr_engine',
    'core.htr_engine',
    'core.pdf_builder',
    'core.data_exporter',
    'core.tuning_config',
    'core.pipeline',
    'core.lite_pipeline',
    'gui.lite_gui',
]

a = Analysis(
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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='S2POTlite',
)
