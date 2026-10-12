import os
import json
import time
from datetime import datetime
from PIL import Image
import pymupdf as fitz

METADATA_FILENAME = "s2pot_metadata.json"


def load_folder_metadata(folder_path):
    """
    Loads s2pot_metadata.json from folder if present and valid.
    """
    if not folder_path or not os.path.exists(folder_path):
        return None
    meta_path = os.path.join(folder_path, METADATA_FILENAME)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "files" in data:
                return data
    except Exception as e:
        print(f"Error loading {METADATA_FILENAME} from {folder_path}: {e}")
    return None


def save_folder_metadata(folder_path, metadata_dict):
    """
    Saves metadata dict to s2pot_metadata.json in folder_path atomically.
    """
    if not folder_path or not os.path.exists(folder_path):
        return False
    meta_path = os.path.join(folder_path, METADATA_FILENAME)
    tmp_path = meta_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata_dict, f, indent=2, ensure_ascii=False)
        if os.path.exists(meta_path):
            os.remove(meta_path)
        os.rename(tmp_path, meta_path)
        return True
    except Exception as e:
        print(f"Error saving {METADATA_FILENAME} to {folder_path}: {e}")
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        return False


def inspect_single_file(fpath):
    """
    Performs full metadata inspection of a single document/image file.
    Returns:
    {
        "filename": "doc.pdf",
        "format": "PDF",
        "size_mb": 1.25,
        "n_pages": 4,
        "max_dpi": 300,
        "pages": [
            {"page_idx": 0, "width": 1240, "height": 1754, "dpi": 300, "color_mode": "color"},
            ...
        ]
    }
    """
    fname = os.path.basename(fpath)
    ext = os.path.splitext(fpath)[1].lower()
    clean_ext = ext.replace(".", "").upper()

    try:
        size_mb = round(os.path.getsize(fpath) / (1024.0 * 1024.0), 2)
    except Exception:
        size_mb = 0.0

    pages = []
    max_dpi = 0
    total_chars = 0

    try:
        if ext == ".pdf":
            with fitz.open(fpath) as doc:
                n_pages = len(doc)
                for idx, page in enumerate(doc):
                    pt_w, pt_h = page.rect.width, page.rect.height
                    try:
                        t = page.get_text("text").strip()
                        if t:
                            total_chars += len(t)
                    except Exception:
                        pass

                    page_dpi = 300
                    cmode = "color"
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

                    max_dpi = max(max_dpi, page_dpi)
                    pages.append({
                        "page_idx": idx,
                        "width": int(round(pt_w * (page_dpi / 72.0))),
                        "height": int(round(pt_h * (page_dpi / 72.0))),
                        "dpi": page_dpi,
                        "color_mode": cmode
                    })
        elif ext in [".tif", ".tiff"]:
            with Image.open(fpath) as tiff:
                n_pages = getattr(tiff, "n_frames", 1)
                dpi_tuple = tiff.info.get("dpi")
                file_dpi = 300
                if dpi_tuple and isinstance(dpi_tuple, tuple):
                    file_dpi = int(round(max(float(dpi_tuple[0]), float(dpi_tuple[1]))))
                max_dpi = file_dpi

                for i in range(n_pages):
                    tiff.seek(i)
                    w, h = tiff.size
                    cmode = "grayscale" if tiff.mode in ["L", "1"] else "color"
                    pages.append({
                        "page_idx": i,
                        "width": w,
                        "height": h,
                        "dpi": file_dpi,
                        "color_mode": cmode
                    })
        else:
            with Image.open(fpath) as img:
                n_pages = 1
                dpi_tuple = img.info.get("dpi")
                file_dpi = 300
                if dpi_tuple and isinstance(dpi_tuple, tuple):
                    file_dpi = int(round(max(float(dpi_tuple[0]), float(dpi_tuple[1]))))
                max_dpi = file_dpi
                w, h = img.size
                cmode = "grayscale" if img.mode in ["L", "1"] else "color"
                pages.append({
                    "page_idx": 0,
                    "width": w,
                    "height": h,
                    "dpi": file_dpi,
                    "color_mode": cmode
                })

    except Exception as e:
        print(f"Error inspecting {fpath}: {e}")
        n_pages = 1
        max_dpi = 300
        pages = [{"page_idx": 0, "width": 1000, "height": 1400, "dpi": 300, "color_mode": "color"}]

    return {
        "filename": fname,
        "format": clean_ext,
        "size_mb": size_mb,
        "n_pages": len(pages),
        "max_dpi": max_dpi if max_dpi > 0 else 300,
        "total_chars": total_chars if ext == ".pdf" else 0,
        "pages": pages
    }


def inspect_files_batch(file_paths, folder_path, progress_callback=None, abort_check=None):
    """
    Inspects multiple files, updates/creates s2pot_metadata.json in folder_path.
    progress_callback(curr_idx, total_count, curr_filename)
    """
    existing_meta = load_folder_metadata(folder_path) or {
        "version": "1.0",
        "folder": folder_path,
        "updated_at": "",
        "files": {}
    }

    files_dict = existing_meta.get("files", {})
    total = len(file_paths)

    for idx, fpath in enumerate(file_paths):
        if abort_check and abort_check():
            break

        fname = os.path.basename(fpath)
        if progress_callback:
            progress_callback(idx + 1, total, fname)

        f_meta = inspect_single_file(fpath)
        files_dict[fname] = f_meta

    existing_meta["files"] = files_dict
    existing_meta["updated_at"] = datetime.now().isoformat()

    save_folder_metadata(folder_path, existing_meta)
    return existing_meta
