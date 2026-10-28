"""
S2POT Lite - Ponto de Entrada da Variante Expresso
Executável leve e ultra-rápido para OCR urgente de ficheiro único.
"""

import sys
import os
import ctypes

# Enable High-DPI awareness on Windows before creating Tkinter instance
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import customtkinter as ctk
from gui.lite_gui import S2POTLiteApp


def main():
    initial_file = None
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        initial_file = sys.argv[1]

    app = S2POTLiteApp(initial_file=initial_file)
    app.mainloop()


if __name__ == "__main__":
    main()
