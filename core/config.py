import os
import json

CONFIG_FILE_PATH = os.path.join(os.path.expanduser("~"), ".ocr_pdf_compressor_config.json")

APP_VERSION = "3.0.0"
APP_NAME = "S2POT - Smart Slim PDF & OCR Tool"

DEFAULT_CONFIG = {
    "last_input_dir": "",
    "last_output_dir": "",
    "last_data_dir": "",
    "enable_auto_deskew": True,
    "export_pdf_original": True,
    "export_pdf_compressed": True,
    "export_docx": True,
    "export_txt": True,
    "export_hocr": True,
    "export_words_json": True,
    "export_words_csv": True,
    "export_words_xlsx": True,
    "export_report_csv": True,
    "export_report_xlsx": True,
    "export_report_json": True,
    "export_audit_report": True,
    "export_txt_from_pdf": True,
    "export_docx_from_pdf": True,
    "organize_subfolders": True,
    "organize_doc_subfolders": True,
    "sync_paths": True,
    "filter_pdf": True,
    "filter_tiff": True,
    "filter_jpg": True,
    "filter_png": True,
    "filter_bmp": True,
    "filter_webp": True,
    "extract_tables": True,
    "extract_illustrations": True,
    "table_font_size": 10,
    "pdf_text_strategy": "reuse",
    "language": "pt_PT",
    "help_mode": True,
    "target_dpi": 300,
    "pdf_jpeg_quality": 70,
    "jpeg_quality": 18,
    "clahe_clip": 2.0
}

def load_config():
    """Load application configuration from JSON file."""
    if os.path.exists(CONFIG_FILE_PATH):
        try:
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in config:
                        config[k] = v
                return config
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config_dict):
    """Save application configuration to JSON file."""
    try:
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")
