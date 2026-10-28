import os
import json
import time
from datetime import datetime
from PIL import Image
import pymupdf as fitz

METADATA_FILENAME = "s2pot_metadata.json"


def estimate_text_layer_confidence(words_list, raw_text=""):
    """
    Estimates OCR / text layer quality (0.0 to 100.0%) for a page from its extracted words and text.
    Detects broken OCR fragments, symbol noise, consonant clusters without vowels, mixed alpha-digit
    recognition errors, and blank/near-blank pages.
    """
    txt_clean = (raw_text or "").strip()
    if not txt_clean and not words_list:
        return 0.0

    tokens = []
    for w in (words_list or []):
        if isinstance(w, (list, tuple)) and len(w) >= 5:
            t = str(w[4]).strip()
        elif isinstance(w, dict):
            if "score" in w and w["score"] is not None:
                # Direct OCR score available
                pass
            t = str(w.get("text", "")).strip()
        else:
            t = str(w).strip()
        if t:
            tokens.append(t)

    if not tokens and txt_clean:
        tokens = txt_clean.split()

    if not tokens:
        return 0.0

    # If words carry explicit OCR confidence scores, use character-weighted mean
    if words_list and isinstance(words_list[0], dict) and "score" in words_list[0]:
        tot_c = 0
        w_sum = 0.0
        for item in words_list:
            t = str(item.get("text", "")).strip()
            nc = len(t)
            if nc > 0:
                sc = float(item.get("score", 0.9))
                if sc > 1.0:
                    sc /= 100.0
                w_sum += sc * nc
                tot_c += nc
        if tot_c > 0:
            return round(max(0.0, min(100.0, (w_sum / float(tot_c)) * 100.0)), 1)

    vowels = set("aeiouáéíóúàèìòùâêîôûãõäëïöüAEIOUÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÃÕÄËÏÖÜyY")
    allowed_punct = set(".,;-:'\"!?()[]/@#$%&*+=-«»—–…ºª°")
    valid_single_letter = set("aeoàáéíóúyAEOÀÁÉÍÓÚY1234567890")

    total_weight = 0
    good_weight = 0.0

    for tok in tokens:
        core = tok.strip(".,;-:'\"!?()[]/«»—–…")
        w_len = max(1, len(tok))
        total_weight += w_len

        if not core:
            # Pure punctuation token
            if len(tok) <= 2 and all(c in allowed_punct for c in tok):
                good_weight += w_len * 0.85
            else:
                good_weight += w_len * 0.2
            continue

        # Check for hard OCR noise patterns
        weird_chars = sum(1 for c in tok if not (c.isalnum() or c in allowed_punct))
        if weird_chars > 0 or "\\\\" in tok or "||" in tok or "__" in tok or ".." in tok:
            good_weight += w_len * 0.15
            continue

        if core.isdigit():
            good_weight += w_len * 0.98
            continue

        if core.isalpha():
            if len(core) == 1:
                good_weight += w_len * (0.92 if core in valid_single_letter else 0.45)
            else:
                has_vowel = any(c in vowels for c in core)
                if not has_vowel and len(core) >= 3 and not core.isupper():
                    # Unlikely consonant cluster from misread ink
                    good_weight += w_len * 0.35
                elif len(core) >= 3 and any(c.isupper() for c in core[1:]) and any(c.islower() for c in core):
                    # Random mid-word casing (common in degraded OCR)
                    good_weight += w_len * 0.55
                else:
                    good_weight += w_len * 0.96
            continue

        # Mixed alphanumeric token (e.g., "1894-1895" or "1º" vs OCR error "c0m0" / "1lI")
        cleaned_alnum = core.replace("-", "").replace("º", "").replace("ª", "").replace("°", "").replace(".", "")
        if cleaned_alnum.isdigit() or cleaned_alnum.isalpha():
            good_weight += w_len * 0.94
        else:
            # Mixed letters and digits inside same word (e.g. 'a1b', '0s') -> likely OCR confusion
            good_weight += w_len * 0.30

    if total_weight <= 0:
        return 0.0

    base_pct = (good_weight / float(total_weight)) * 100.0
    # Penalize pages with suspiciously few characters (< 15 chars on a page)
    if len(txt_clean) < 15:
        base_pct *= 0.65

    return round(max(0.0, min(100.0, base_pct)), 1)


def load_folder_metadata(folder_path):
    """
    Loads s2pot_metadata.json from folder if present and valid.
    Automatically backfills OCR/text quality confidence for any PDF entries inspected in older versions.
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
                updated = False
                files_dict = data.get("files", {})
                for fname, f_meta in files_dict.items():
                    if not isinstance(f_meta, dict):
                        continue
                    if f_meta.get("confidence") is None and fname.lower().endswith(".pdf"):
                        fpath = os.path.join(folder_path, fname)
                        if os.path.exists(fpath):
                            try:
                                with fitz.open(fpath) as doc:
                                    p_list = f_meta.get("pages", [])
                                    tot_c = 0
                                    for idx, page in enumerate(doc):
                                        t = page.get_text("text").strip()
                                        w_list = page.get_text("words")
                                        p_chars = len(t)
                                        tot_c += p_chars
                                        p_conf = estimate_text_layer_confidence(w_list, t) if p_chars > 0 else 0.0
                                        if idx < len(p_list) and isinstance(p_list[idx], dict):
                                            p_list[idx]["chars"] = p_chars
                                            p_list[idx]["confidence"] = p_conf
                                    conf_pages = [p for p in p_list if isinstance(p, dict) and p.get("confidence") is not None]
                                    if conf_pages:
                                        if tot_c > 0:
                                            f_meta["confidence"] = round(sum((p.get("confidence") or 0.0) * (p.get("chars") or 0) for p in conf_pages) / float(tot_c), 1)
                                        else:
                                            f_meta["confidence"] = round(sum((p.get("confidence") or 0.0) for p in conf_pages) / float(len(conf_pages)), 1)
                                        f_meta["min_page_confidence"] = round(min((p.get("confidence") or 0.0) for p in conf_pages), 1)
                                        f_meta["low_conf_pages"] = sum(1 for p in conf_pages if ((p.get("confidence") or 0.0) < 80.0 or (p.get("chars") or 0) == 0))
                                        f_meta["total_chars"] = tot_c
                                        updated = True
                            except Exception:
                                pass
                if updated:
                    save_folder_metadata(folder_path, data)
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
                    page_chars = 0
                    page_conf = None
                    try:
                        t = page.get_text("text").strip()
                        w_list = page.get_text("words")
                        if t:
                            page_chars = len(t)
                            total_chars += page_chars
                            page_conf = estimate_text_layer_confidence(w_list, t)
                        else:
                            page_conf = 0.0
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
                        "color_mode": cmode,
                        "chars": page_chars,
                        "confidence": page_conf
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

    file_cmode = "grayscale" if (pages and all(p.get("color_mode") == "grayscale" for p in pages)) else "color"

    conf_pages = [p for p in pages if p.get("confidence") is not None]
    if conf_pages:
        if total_chars > 0:
            file_conf = round(sum((p.get("confidence") or 0.0) * (p.get("chars") or 0) for p in conf_pages) / float(total_chars), 1)
        else:
            file_conf = round(sum((p.get("confidence") or 0.0) for p in conf_pages) / float(len(conf_pages)), 1)
        min_page_conf = round(min((p.get("confidence") or 0.0) for p in conf_pages), 1)
        low_conf_pages = sum(1 for p in conf_pages if ((p.get("confidence") or 0.0) < 80.0 or (p.get("chars") or 0) == 0))
    else:
        file_conf = None
        min_page_conf = None
        low_conf_pages = 0

    return {
        "filename": fname,
        "format": clean_ext,
        "size_mb": size_mb,
        "n_pages": len(pages),
        "max_dpi": max_dpi if max_dpi > 0 else 300,
        "color_mode": file_cmode,
        "total_chars": total_chars if ext == ".pdf" else 0,
        "confidence": file_conf,
        "min_page_confidence": min_page_conf,
        "low_conf_pages": low_conf_pages,
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
        prev_meta = files_dict.get(fname)
        if isinstance(prev_meta, dict) and prev_meta.get("status") == "done":
            for k in ("status", "comp_size_mb", "confidence", "min_page_confidence", "low_conf_pages", "elapsed", "target_dpi", "target_cmode"):
                if k in prev_meta and prev_meta[k] is not None:
                    f_meta[k] = prev_meta[k]
            if prev_meta.get("total_chars"):
                f_meta["total_chars"] = prev_meta["total_chars"]
            prev_pages = prev_meta.get("pages", [])
            for p_idx, p_dict in enumerate(f_meta.get("pages", [])):
                if p_idx < len(prev_pages) and isinstance(prev_pages[p_idx], dict):
                    pp = prev_pages[p_idx]
                    for pk in ("chars", "confidence", "status", "elapsed", "target_dpi", "target_cmode"):
                        if pk in pp and pp[pk] is not None:
                            p_dict[pk] = pp[pk]
        files_dict[fname] = f_meta

    existing_meta["files"] = files_dict
    existing_meta["updated_at"] = datetime.now().isoformat()

    save_folder_metadata(folder_path, existing_meta)
    return existing_meta
