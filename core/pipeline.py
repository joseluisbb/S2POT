import os
import time
import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image
from core.enhancer import enhance_contrast_lab
from core.deskew import auto_align_page
from core.ocr_engine import OCREngine
from core.pdf_builder import create_searchable_pdf, encode_page_jpeg_bytes
from core.data_exporter import export_file_data, export_batch_reports
from core.tuning_config import load_tuning_config, get_effective_file_tuning

def apply_color_mode(bgr_img, color_mode):
    if not color_mode or color_mode == "color":
        return bgr_img
    if color_mode == "grayscale":
        gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if color_mode in ["monochrome", "bw", "binarized"]:
        gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)
        bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 10)
        return cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    return bgr_img


def compute_native_text_quality(direct_words):
    """
    Evaluates if an embedded native PDF text layer is high-quality or contains corrupt/OCR garbage noise.
    Returns (is_good: bool, confidence_pct: float).
    """
    if not direct_words:
        return False, 0.0
    if len(direct_words) < 5:
        return True, 95.0
    garbage_count = 0
    for w in direct_words:
        txt = w[4].strip()
        if not txt:
            continue
        has_noise = (
            "\\" in txt or
            "..." in txt or
            "___" in txt or
            "||" in txt or
            ".." in txt or
            sum(1 for c in txt if not (c.isalnum() or c in " .,;-:'\"!?()[]/@#$%&*+=-")) > 0
        )
        if has_noise:
            garbage_count += 1
    ratio = garbage_count / float(len(direct_words))
    conf_pct = round(max(0.0, min(100.0, (1.0 - ratio) * 100.0)), 1)
    return ratio <= 0.15, conf_pct


def is_native_text_quality_good(direct_words):
    is_good, _ = compute_native_text_quality(direct_words)
    return is_good


def compute_ocr_confidence_pct(ocr_results):
    """Computes character-weighted average OCR confidence (0.0 to 100.0%)."""
    if not ocr_results:
        return 0.0
    tot_chars = 0
    weighted_score = 0.0
    for item in ocr_results:
        txt = item.get("text", "").strip()
        n_c = len(txt)
        if n_c <= 0:
            continue
        sc = float(item.get("score", 0.92))
        if sc > 1.0:
            sc = sc / 100.0
        weighted_score += sc * n_c
        tot_chars += n_c
    if tot_chars <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (weighted_score / float(tot_chars)) * 100.0)), 1)


def _rescale_bbox_items(ocr_results, illustrations, tables, scale_x, scale_y):
    """Rescales OCR word, illustration, and table bounding boxes from high-res OCR space to Target DPI space."""
    if abs(scale_x - 1.0) < 1e-4 and abs(scale_y - 1.0) < 1e-4:
        return ocr_results, illustrations, tables

    for item in (ocr_results or []):
        if isinstance(item, dict):
            bx = item.get("bbox")
            if bx and len(bx) == 4:
                item["bbox"] = [
                    int(round(bx[0] * scale_x)),
                    int(round(bx[1] * scale_y)),
                    int(round(bx[2] * scale_x)),
                    int(round(bx[3] * scale_y))
                ]

    scaled_illustrations = []
    for ill in (illustrations or []):
        if isinstance(ill, dict):
            bx = ill.get("bbox")
            if bx and len(bx) == 4:
                ill["bbox"] = [
                    int(round(bx[0] * scale_x)),
                    int(round(bx[1] * scale_y)),
                    int(round(bx[2] * scale_x)),
                    int(round(bx[3] * scale_y))
                ]
            scaled_illustrations.append(ill)
        elif isinstance(ill, np.ndarray) and ill.size > 0:
            nh = max(1, int(round(ill.shape[0] * scale_y)))
            nw = max(1, int(round(ill.shape[1] * scale_x)))
            scaled_illustrations.append(cv2.resize(ill, (nw, nh), interpolation=cv2.INTER_AREA))
        else:
            scaled_illustrations.append(ill)
    illustrations = scaled_illustrations

    for tbl in (tables or []):
        if isinstance(tbl, dict):
            bx = tbl.get("bbox")
            if bx and len(bx) == 4:
                tbl["bbox"] = [
                    int(round(bx[0] * scale_x)),
                    int(round(bx[1] * scale_y)),
                    int(round(bx[2] * scale_x)),
                    int(round(bx[3] * scale_y))
                ]

    return ocr_results, illustrations, tables


def process_single_file(file_path, output_dir, ocr_engine, data_dir=None, export_options=None, enable_auto_deskew=True, pdf_text_strategy="reuse", target_dpi=300, jpeg_quality=70, progress_callback=None, abort_check_fn=None):
    """
    Processes a single multi-page or image file into searchable PDF (original and/or compressed) and DOCX/data exports.
    Supports PDF, TIFF, JPG, PNG, BMP, WEBP.
    """
    start_time = time.time()
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    ext = os.path.splitext(file_path)[1].lower()

    options = export_options or {}
    filename = os.path.basename(file_path)
    input_dir = os.path.dirname(file_path)
    tuning_cfg = options.get("tuning_config") or load_tuning_config(input_dir)
    effective_tuning = get_effective_file_tuning(tuning_cfg, filename, default_dpi=target_dpi)

    eff_color_mode = options.get("color_mode", effective_tuning.get("color_mode", "color"))
    target_dpi = options.get("target_dpi", effective_tuning.get("target_dpi", target_dpi))
    eff_quality = options.get("pdf_jpeg_quality", options.get("jpeg_quality", effective_tuning.get("jpeg_quality", jpeg_quality)))
    eff_max_dim = options.get("downsample_max_dim", effective_tuning.get("downsample_max_dim", 2000))
    eff_doc_type = options.get("doc_type", getattr(ocr_engine, "doc_type", "printed"))

    if eff_doc_type == "handwritten":
        pdf_text_strategy = "force_ocr"
    elif "pdf_text_strategy" in options:
        pdf_text_strategy = options["pdf_text_strategy"]

    pages_data = []
    pages_info = []
    full_plain_text = []
    full_hocr_pages = []
    total_words_count = 0
    total_chars_count = 0

    has_orig_text_layer = False
    orig_plain_text = []
    orig_hocr_pages = []
    orig_pages_data = []

    if ext == ".pdf":
        doc = fitz.open(file_path)
        total_pages = len(doc)

        for p_idx in range(total_pages):
            if abort_check_fn and abort_check_fn():
                doc.close()
                raise InterruptedError("Process aborted by user.")

            page_t0 = time.time()
            if progress_callback:
                progress_callback(p_idx + 1, total_pages, f"Processing page {p_idx+1}/{total_pages}...")

            page_tune = get_effective_file_tuning(tuning_cfg, filename, page_idx=p_idx, default_dpi=target_dpi)
            p_target_dpi = options.get("target_dpi", page_tune.get("target_dpi", target_dpi))
            p_color_mode = options.get("color_mode", page_tune.get("color_mode", eff_color_mode))

            page = doc[p_idx]
            pt_w, pt_h = page.rect.width, page.rect.height
            page_orig_dpi = 300
            page_orig_cmode = "color"
            image_list = page.get_images()
            if image_list:
                for img_item in image_list:
                    try:
                        base_img = doc.extract_image(img_item[0])
                        w_i, h_i = base_img.get("width", 0), base_img.get("height", 0)
                        cs = base_img.get("colorspace", 3)
                        if cs == 1:
                            page_orig_cmode = "grayscale"
                        if pt_w > 0 and pt_h > 0 and w_i > 0 and h_i > 0:
                            page_orig_dpi = max(page_orig_dpi, int(round((w_i * 72.0) / pt_w)))
                    except Exception:
                        pass

            direct_text = page.get_text("text").strip()
            direct_words = page.get_text("words")
            native_quality_good, native_conf_pct = compute_native_text_quality(direct_words)
            page_has_text = (len(direct_text) >= 10 and len(direct_words) > 0 and native_quality_good)
            need_ocr = not (page_has_text and pdf_text_strategy != "force_ocr")

            # Option A: Always run OCR at high resolution (>= 300 DPI) even when Target PDF DPI is lower (72-200 DPI)
            ocr_dpi = max(p_target_dpi, min(max(page_orig_dpi, 300), 300)) if need_ocr else p_target_dpi

            pix = page.get_pixmap(dpi=ocr_dpi)
            if not image_list:
                if pix.n == 1 or pix.colorspace == fitz.csGRAY:
                    page_orig_cmode = "grayscale"
            orig_w = int(round(pt_w * (page_orig_dpi / 72.0))) if pt_w > 0 else pix.width
            orig_h = int(round(pt_h * (page_orig_dpi / 72.0))) if pt_h > 0 else pix.height

            if pix.n == 4:
                bgr_resampled = cv2.cvtColor(np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4)), cv2.COLOR_RGBA2BGR)
            else:
                bgr_resampled = cv2.cvtColor(np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3)), cv2.COLOR_RGB2BGR)

            bgr_aligned, rot_deg, skew_deg = auto_align_page(bgr_resampled, enable_deskew=enable_auto_deskew)
            bgr_enhanced_ocr = enhance_contrast_lab(bgr_aligned, dpi=ocr_dpi)

            if len(direct_text) > 0 or len(direct_words) > 0:
                has_orig_text_layer = True
                orig_ocr_results = []
                pt_to_px = float(p_target_dpi) / 72.0
                for w in direct_words:
                    x0, y0, x1, y1, w_text = w[0], w[1], w[2], w[3], w[4]
                    orig_ocr_results.append({
                        "text": w_text,
                        "bbox": [int(round(x0 * pt_to_px)), int(round(y0 * pt_to_px)), int(round(x1 * pt_to_px)), int(round(y1 * pt_to_px))],
                        "font_size_pt": round(max(8.0, min(32.0, y1 - y0)), 1),
                        "font_weight": "Normal",
                        "font_family": "Sans-Serif",
                        "score": round(native_conf_pct / 100.0, 3)
                    })

                orig_plain_text.append(f"--- Page {p_idx+1} ---\n" + direct_text)
                orig_hocr_html = f'<div class="ocr_page" id="page_{p_idx+1}">\n'
                for item in orig_ocr_results:
                    bx = item["bbox"]
                    orig_hocr_html += f'  <span class="ocrx_word" title="bbox {bx[0]} {bx[1]} {bx[2]} {bx[3]}">{item["text"]}</span>\n'
                orig_hocr_html += '</div>'
                orig_hocr_pages.append(orig_hocr_html)

                orig_pages_data.append({
                    "page_num": p_idx + 1,
                    "ocr_results": orig_ocr_results
                })

            if not need_ocr:
                bgr_enhanced = apply_color_mode(bgr_enhanced_ocr, p_color_mode)
                ocr_results = orig_ocr_results
                full_plain_text.append(f"--- Page {p_idx+1} ---\n" + direct_text)
                page_chars = len(direct_text)
                page_conf = native_conf_pct
                total_words_count += len(ocr_results)
                total_chars_count += page_chars
                full_hocr_pages.append(orig_hocr_html)
                illustrations, tables = [], []
            else:
                ocr_input_bgr = apply_color_mode(bgr_enhanced_ocr, "grayscale") if p_color_mode != "color" else bgr_enhanced_ocr
                ocr_pack = ocr_engine.run_ocr_full(ocr_input_bgr, page_num=p_idx+1, target_dpi=ocr_dpi)
                ocr_results = ocr_pack["ocr_results"]
                illustrations = ocr_pack.get("illustrations", [])
                tables = ocr_pack.get("tables", [])

                if p_target_dpi != ocr_dpi:
                    h_ocr, w_ocr = bgr_enhanced_ocr.shape[:2]
                    scale_to_target = float(p_target_dpi) / float(ocr_dpi)
                    target_w = max(100, int(round(w_ocr * scale_to_target)))
                    target_h = max(100, int(round(h_ocr * scale_to_target)))
                    bgr_resized = cv2.resize(bgr_enhanced_ocr, (target_w, target_h), interpolation=cv2.INTER_AREA)
                    sx = float(target_w) / float(w_ocr)
                    sy = float(target_h) / float(h_ocr)
                    ocr_results, illustrations, tables = _rescale_bbox_items(ocr_results, illustrations, tables, sx, sy)
                else:
                    bgr_resized = bgr_enhanced_ocr

                bgr_enhanced = apply_color_mode(bgr_resized, p_color_mode)
                page_chars = sum(len(w.get("text", "")) for w in ocr_results)
                page_conf = compute_ocr_confidence_pct(ocr_results)
                total_words_count += len(ocr_results)
                total_chars_count += page_chars

                p_text = ocr_pack.get("plain_text", "").strip()
                full_plain_text.append(f"--- Page {p_idx+1} ---\n" + p_text)

                h_html = ocr_pack.get("hocr_html", "")
                full_hocr_pages.append(h_html)

            jpeg_bytes = encode_page_jpeg_bytes(bgr_enhanced, jpeg_quality=95, color_mode=p_color_mode)

            pages_data.append({
                "page_num": p_idx + 1,
                "jpeg_bytes": jpeg_bytes,
                "bgr_image": bgr_enhanced,
                "width_px": bgr_enhanced.shape[1],
                "height_px": bgr_enhanced.shape[0],
                "dpi": p_target_dpi,
                "color_mode": p_color_mode,
                "ocr_results": ocr_results,
                "illustrations": illustrations,
                "tables": tables
            })
            pages_info.append({
                "page_idx": p_idx,
                "width": orig_w,
                "height": orig_h,
                "chars": page_chars,
                "confidence": page_conf,
                "orig_dpi": page_orig_dpi,
                "orig_cmode": page_orig_cmode,
                "target_dpi": p_target_dpi,
                "target_cmode": p_color_mode,
                "elapsed": time.time() - page_t0,
                "status": "done"
            })

        doc.close()

    elif ext in [".tif", ".tiff"]:
        tiff = Image.open(file_path)
        total_pages = getattr(tiff, "n_frames", 1)

        for p_idx in range(total_pages):
            if abort_check_fn and abort_check_fn():
                raise InterruptedError("Process aborted by user.")

            page_t0 = time.time()
            if progress_callback:
                progress_callback(p_idx + 1, total_pages, f"Processing page {p_idx+1}/{total_pages}...")

            page_tune = get_effective_file_tuning(tuning_cfg, filename, page_idx=p_idx, default_dpi=target_dpi)
            p_target_dpi = options.get("target_dpi", page_tune.get("target_dpi", target_dpi))
            p_color_mode = options.get("color_mode", page_tune.get("color_mode", eff_color_mode))

            tiff.seek(p_idx)
            page_orig_cmode = "grayscale" if tiff.mode in ["L", "1"] else "color"
            page_img = np.array(tiff.convert('RGB'))
            bgr_full = cv2.cvtColor(page_img, cv2.COLOR_RGB2BGR)

            h_orig, w_orig = bgr_full.shape[:2]
            input_dpi = 300
            if "dpi" in tiff.info and isinstance(tiff.info["dpi"], tuple):
                input_dpi = int(round(max(float(tiff.info["dpi"][0]), float(tiff.info["dpi"][1]))))

            # Option A: Always run OCR at >= 300 DPI (or original DPI), then rescale to p_target_dpi
            ocr_dpi = max(p_target_dpi, min(max(input_dpi, 300), 300))
            if ocr_dpi != input_dpi and input_dpi > 0:
                scale_ocr = float(ocr_dpi) / float(input_dpi)
                ocr_w = max(100, int(round(w_orig * scale_ocr)))
                ocr_h = max(100, int(round(h_orig * scale_ocr)))
                bgr_for_ocr = cv2.resize(bgr_full, (ocr_w, ocr_h), interpolation=cv2.INTER_AREA if scale_ocr < 1.0 else cv2.INTER_CUBIC)
            else:
                bgr_for_ocr = bgr_full

            bgr_aligned, rot_deg, skew_deg = auto_align_page(bgr_for_ocr, enable_deskew=enable_auto_deskew)
            bgr_enhanced_ocr = enhance_contrast_lab(bgr_aligned, dpi=ocr_dpi)

            ocr_input_bgr = apply_color_mode(bgr_enhanced_ocr, "grayscale") if p_color_mode != "color" else bgr_enhanced_ocr
            ocr_pack = ocr_engine.run_ocr_full(ocr_input_bgr, page_num=p_idx+1, target_dpi=ocr_dpi)
            ocr_results = ocr_pack["ocr_results"]
            illustrations = ocr_pack.get("illustrations", [])
            tables = ocr_pack.get("tables", [])

            if p_target_dpi != ocr_dpi:
                h_ocr, w_ocr = bgr_enhanced_ocr.shape[:2]
                scale_to_target = float(p_target_dpi) / float(ocr_dpi)
                target_w = max(100, int(round(w_ocr * scale_to_target)))
                target_h = max(100, int(round(h_ocr * scale_to_target)))
                bgr_resized = cv2.resize(bgr_enhanced_ocr, (target_w, target_h), interpolation=cv2.INTER_AREA)
                sx = float(target_w) / float(w_ocr)
                sy = float(target_h) / float(h_ocr)
                ocr_results, illustrations, tables = _rescale_bbox_items(ocr_results, illustrations, tables, sx, sy)
            else:
                bgr_resized = bgr_enhanced_ocr

            bgr_enhanced = apply_color_mode(bgr_resized, p_color_mode)
            page_chars = sum(len(w.get("text", "")) for w in ocr_results)
            page_conf = compute_ocr_confidence_pct(ocr_results)
            total_words_count += len(ocr_results)
            total_chars_count += page_chars

            if ocr_pack["plain_text"]:
                full_plain_text.append(f"--- Page {p_idx+1} ---\n" + ocr_pack["plain_text"])
            if ocr_pack["hocr_html"]:
                full_hocr_pages.append(ocr_pack["hocr_html"])

            jpeg_bytes = encode_page_jpeg_bytes(bgr_enhanced, jpeg_quality=95, color_mode=p_color_mode)

            pages_data.append({
                "page_num": p_idx + 1,
                "jpeg_bytes": jpeg_bytes,
                "bgr_image": bgr_enhanced,
                "width_px": bgr_enhanced.shape[1],
                "height_px": bgr_enhanced.shape[0],
                "dpi": p_target_dpi,
                "color_mode": p_color_mode,
                "ocr_results": ocr_results,
                "illustrations": illustrations,
                "tables": tables
            })
            pages_info.append({
                "page_idx": p_idx,
                "width": w_orig,
                "height": h_orig,
                "chars": page_chars,
                "confidence": page_conf,
                "orig_dpi": input_dpi,
                "orig_cmode": page_orig_cmode,
                "target_dpi": p_target_dpi,
                "target_cmode": p_color_mode,
                "elapsed": time.time() - page_t0,
                "status": "done"
            })

    else:
        # Standard images: JPG, PNG, BMP, WEBP
        total_pages = 1
        page_t0 = time.time()
        if progress_callback:
            progress_callback(1, 1, "Processing image file...")

        page_tune = get_effective_file_tuning(tuning_cfg, filename, page_idx=0, default_dpi=target_dpi)
        p_target_dpi = options.get("target_dpi", page_tune.get("target_dpi", target_dpi))
        p_color_mode = options.get("color_mode", page_tune.get("color_mode", eff_color_mode))

        input_dpi = 300
        page_orig_cmode = "color"
        try:
            with Image.open(file_path) as img_meta:
                dpi_tuple = img_meta.info.get("dpi")
                if dpi_tuple and isinstance(dpi_tuple, tuple):
                    input_dpi = int(round(max(float(dpi_tuple[0]), float(dpi_tuple[1]))))
                page_orig_cmode = "grayscale" if img_meta.mode in ["L", "1"] else "color"
        except Exception:
            pass

        bgr_full = cv2.imread(file_path)
        if bgr_full is None:
            pil_img = Image.open(file_path).convert("RGB")
            bgr_full = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

        h_orig_raw, w_orig_raw = bgr_full.shape[:2]
        ocr_dpi = max(p_target_dpi, min(max(input_dpi, 300), 300))
        if max(h_orig_raw, w_orig_raw) < 1500:
            scale = 2000.0 / float(max(h_orig_raw, w_orig_raw))
            bgr_for_ocr = cv2.resize(bgr_full, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        elif ocr_dpi != input_dpi and input_dpi > 0:
            scale_ocr = float(ocr_dpi) / float(input_dpi)
            ocr_w = max(100, int(round(w_orig_raw * scale_ocr)))
            ocr_h = max(100, int(round(h_orig_raw * scale_ocr)))
            bgr_for_ocr = cv2.resize(bgr_full, (ocr_w, ocr_h), interpolation=cv2.INTER_AREA if scale_ocr < 1.0 else cv2.INTER_CUBIC)
        else:
            bgr_for_ocr = bgr_full

        bgr_aligned, rot_deg, skew_deg = auto_align_page(bgr_for_ocr, enable_deskew=enable_auto_deskew)
        bgr_enhanced_ocr = enhance_contrast_lab(bgr_aligned, dpi=ocr_dpi)

        ocr_input_bgr = apply_color_mode(bgr_enhanced_ocr, "grayscale") if p_color_mode != "color" else bgr_enhanced_ocr
        ocr_pack = ocr_engine.run_ocr_full(ocr_input_bgr, page_num=1, target_dpi=ocr_dpi)
        ocr_results = ocr_pack["ocr_results"]
        illustrations = ocr_pack.get("illustrations", [])
        tables = ocr_pack.get("tables", [])

        if p_target_dpi != ocr_dpi:
            h_ocr, w_ocr = bgr_enhanced_ocr.shape[:2]
            scale_to_target = float(p_target_dpi) / float(ocr_dpi)
            target_w = max(100, int(round(w_ocr * scale_to_target)))
            target_h = max(100, int(round(h_ocr * scale_to_target)))
            bgr_resized = cv2.resize(bgr_enhanced_ocr, (target_w, target_h), interpolation=cv2.INTER_AREA)
            sx = float(target_w) / float(w_ocr)
            sy = float(target_h) / float(h_ocr)
            ocr_results, illustrations, tables = _rescale_bbox_items(ocr_results, illustrations, tables, sx, sy)
        else:
            bgr_resized = bgr_enhanced_ocr

        bgr_enhanced = apply_color_mode(bgr_resized, p_color_mode)
        page_chars = sum(len(w.get("text", "")) for w in ocr_results)
        page_conf = compute_ocr_confidence_pct(ocr_results)
        total_words_count += len(ocr_results)
        total_chars_count += page_chars

        if ocr_pack["plain_text"]:
            full_plain_text.append("--- Page 1 ---\n" + ocr_pack["plain_text"])
        if ocr_pack["hocr_html"]:
            full_hocr_pages.append(ocr_pack["hocr_html"])

        jpeg_bytes = encode_page_jpeg_bytes(bgr_enhanced, jpeg_quality=95, color_mode=p_color_mode)

        pages_data.append({
            "page_num": 1,
            "jpeg_bytes": jpeg_bytes,
            "bgr_image": bgr_enhanced,
            "width_px": bgr_enhanced.shape[1],
            "height_px": bgr_enhanced.shape[0],
            "dpi": p_target_dpi,
            "color_mode": p_color_mode,
            "ocr_results": ocr_results,
            "illustrations": illustrations,
            "tables": tables
        })
        pages_info.append({
            "page_idx": 0,
            "width": w_orig_raw,
            "height": h_orig_raw,
            "chars": page_chars,
            "confidence": page_conf,
            "orig_dpi": input_dpi,
            "orig_cmode": page_orig_cmode,
            "target_dpi": p_target_dpi,
            "target_cmode": p_color_mode,
            "elapsed": time.time() - page_t0,
            "status": "done"
        })

    primary_pdf_path = None
    out_orig_pdf = None
    out_comp_pdf = None

    organize_docs = options.get("organize_doc_subfolders", False)

    # Assemble searchable PDFs based on independent checkboxes
    if options.get("export_pdf_original", False):
        pdf_name = f"{base_name}.pdf" if not options.get("export_pdf_compressed") else f"{base_name}_original.pdf"
        if organize_docs:
            doc_sub = os.path.join(output_dir, "PDF-OriginalImages")
            os.makedirs(doc_sub, exist_ok=True)
            out_orig_pdf = os.path.join(doc_sub, pdf_name)
        else:
            out_orig_pdf = os.path.join(output_dir, pdf_name)
        create_searchable_pdf(pages_data, out_orig_pdf, compress_background=False)
        primary_pdf_path = out_orig_pdf

    if options.get("export_pdf_compressed", False):
        if organize_docs:
            doc_sub = os.path.join(output_dir, "PDF-Compressed")
            os.makedirs(doc_sub, exist_ok=True)
            out_comp_pdf = os.path.join(doc_sub, f"{base_name}_compressed.pdf")
        else:
            out_comp_pdf = os.path.join(output_dir, f"{base_name}_compressed.pdf")
        create_searchable_pdf(pages_data, out_comp_pdf, compress_background=True, jpeg_quality=eff_quality, max_downsample_dim=eff_max_dim)
        if not primary_pdf_path:
            primary_pdf_path = out_comp_pdf

    if not primary_pdf_path:
        primary_pdf_path = os.path.join(output_dir, f"{base_name}.pdf")

    # Export Data & Document Formats (DOCX, TXT, hOCR, JSON, CSV, XLSX, TXT/DOCX from PDF)
    if pdf_text_strategy == "force_ocr" and has_orig_text_layer:
        export_file_data(data_dir or output_dir, f"{base_name}_original", orig_pages_data, orig_plain_text, orig_hocr_pages, options, docs_dir=output_dir, has_orig_text_layer=has_orig_text_layer, orig_plain_text=orig_plain_text, orig_pages_data=orig_pages_data)
    export_file_data(data_dir or output_dir, base_name, pages_data, full_plain_text, full_hocr_pages, options, docs_dir=output_dir, has_orig_text_layer=has_orig_text_layer, orig_plain_text=orig_plain_text, orig_pages_data=orig_pages_data)

    out_docx_path = os.path.join(output_dir, "DOCX", f"{base_name}.docx") if organize_docs else os.path.join(output_dir, f"{base_name}.docx")
    primary_out_path = out_comp_pdf or out_orig_pdf or (primary_pdf_path if os.path.exists(primary_pdf_path) else None) or (out_docx_path if os.path.exists(out_docx_path) else None)

    duration = time.time() - start_time
    orig_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    pdf_size_mb = os.path.getsize(primary_out_path) / (1024 * 1024) if (primary_out_path and os.path.exists(primary_out_path)) else 0.0
    reduction_pct = ((1 - pdf_size_mb / orig_size_mb) * 100) if orig_size_mb > 0 else 0

    max_orig_dpi = max((p.get("orig_dpi", 300) for p in pages_info), default=300)
    file_orig_cmode = "grayscale" if (pages_info and all(p.get("orig_cmode") == "grayscale" for p in pages_info)) else "color"

    if pages_info:
        if total_chars_count > 0:
            file_conf = round(sum(p.get("confidence", 0.0) * p.get("chars", 0) for p in pages_info) / float(total_chars_count), 1)
        else:
            file_conf = round(sum(p.get("confidence", 0.0) for p in pages_info) / float(len(pages_info)), 1)
        min_page_conf = round(min(p.get("confidence", 0.0) for p in pages_info), 1)
        low_conf_pages = sum(1 for p in pages_info if (p.get("confidence", 0.0) < 80.0 or p.get("chars", 0) == 0))
    else:
        file_conf = 0.0
        min_page_conf = 0.0
        low_conf_pages = 0

    record = {
        "filename": base_name,
        "pages": total_pages,
        "tiff_size_mb": orig_size_mb,
        "pdf_size_mb": pdf_size_mb,
        "output_pdf": primary_pdf_path if (primary_pdf_path and os.path.exists(primary_pdf_path)) else None,
        "output_pdf_compressed": out_comp_pdf if (out_comp_pdf and os.path.exists(out_comp_pdf)) else None,
        "output_primary_path": primary_out_path,
        "reduction_pct": reduction_pct,
        "total_words": total_words_count,
        "total_chars": total_chars_count,
        "confidence": file_conf,
        "min_page_confidence": min_page_conf,
        "low_conf_pages": low_conf_pages,
        "duration_sec": duration,
        "orig_dpi": max_orig_dpi,
        "orig_cmode": file_orig_cmode,
        "target_dpi": target_dpi,
        "target_cmode": eff_color_mode,
        "pages_info": pages_info,
        "status": "COMPLETED"
    }

    return primary_pdf_path, record

process_single_tiff = process_single_file

def batch_process_files(file_list, output_dir, data_dir=None, export_options=None, enable_auto_deskew=True, pdf_text_strategy="reuse", target_dpi=300, jpeg_quality=70, doc_type="printed", progress_callback=None, file_complete_callback=None, abort_check_fn=None):
    os.makedirs(output_dir, exist_ok=True)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)

    opts = export_options or {}
    eff_doc_type = opts.get("doc_type", doc_type)
    ocr_engine = OCREngine(target_dpi=target_dpi, doc_type=eff_doc_type)
    total_files = len(file_list)
    results = []
    report_records = []

    for f_idx, file_path in enumerate(file_list):
        if abort_check_fn and abort_check_fn():
            raise InterruptedError("Process aborted by user.")

        filename = os.path.basename(file_path)

        def page_cb(curr_p, tot_p, msg):
            if progress_callback:
                progress_callback(f_idx + 1, total_files, filename, curr_p, tot_p, msg)

        out_path, record = process_single_file(
            file_path,
            output_dir,
            ocr_engine,
            data_dir=data_dir,
            export_options=export_options,
            enable_auto_deskew=enable_auto_deskew,
            pdf_text_strategy=pdf_text_strategy,
            target_dpi=target_dpi,
            jpeg_quality=jpeg_quality,
            progress_callback=page_cb,
            abort_check_fn=abort_check_fn
        )
        results.append(out_path)
        report_records.append(record)

        if file_complete_callback:
            file_complete_callback(file_path, record)

    if export_options and data_dir:
        export_batch_reports(data_dir, report_records, export_options)

    return results, report_records


def get_expected_output_files(target_files, output_dir, data_dir=None, export_options=None):
    """
    Computes all output file paths that will be generated for the given list of target input files and export options.
    """
    opts = export_options or {}
    data_dest = data_dir if data_dir else output_dir
    use_subfolders = opts.get("organize_subfolders", False)
    organize_docs = opts.get("organize_doc_subfolders", False)

    expected_files = []

    for file_path in target_files:
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        has_pdf_orig = opts.get("export_pdf_original", False)
        has_pdf_comp = opts.get("export_pdf_compressed", False)

        # 1. Searchable PDF (Original)
        if has_pdf_orig:
            pdf_name = f"{base_name}.pdf" if not has_pdf_comp else f"{base_name}_original.pdf"
            if organize_docs:
                expected_files.append(os.path.join(output_dir, "PDF-OriginalImages", pdf_name))
            else:
                expected_files.append(os.path.join(output_dir, pdf_name))

        # 2. Searchable PDF (Compressed)
        if has_pdf_comp:
            if organize_docs:
                expected_files.append(os.path.join(output_dir, "PDF-Compressed", f"{base_name}_compressed.pdf"))
            else:
                expected_files.append(os.path.join(output_dir, f"{base_name}_compressed.pdf"))

        # 3. Microsoft Word (.docx)
        if opts.get("export_docx", True):
            if organize_docs:
                expected_files.append(os.path.join(output_dir, "DOCX", f"{base_name}.docx"))
            else:
                expected_files.append(os.path.join(output_dir, f"{base_name}.docx"))

        # 4. Plain Text (.txt)
        if opts.get("export_txt", True) and data_dest:
            if use_subfolders:
                expected_files.append(os.path.join(data_dest, "Text", f"{base_name}.txt"))
            else:
                expected_files.append(os.path.join(data_dest, f"{base_name}.txt"))

        # 5. hOCR (.hocr)
        if opts.get("export_hocr", False) and data_dest:
            if use_subfolders:
                expected_files.append(os.path.join(data_dest, "hOCR", f"{base_name}.hocr"))
            else:
                expected_files.append(os.path.join(data_dest, f"{base_name}.hocr"))

        # 6. JSON (.json)
        if opts.get("export_words_json", False) and data_dest:
            if use_subfolders:
                expected_files.append(os.path.join(data_dest, "Word_Coordinates", f"{base_name}_words.json"))
            else:
                expected_files.append(os.path.join(data_dest, f"{base_name}_words.json"))

        # 7. CSV (.csv)
        if opts.get("export_words_csv", False) and data_dest:
            if use_subfolders:
                expected_files.append(os.path.join(data_dest, "Word_Coordinates", f"{base_name}_words.csv"))
            else:
                expected_files.append(os.path.join(data_dest, f"{base_name}_words.csv"))

        # 8. Excel (.xlsx)
        if opts.get("export_words_xlsx", False) and data_dest:
            if use_subfolders:
                expected_files.append(os.path.join(data_dest, "Word_Coordinates", f"{base_name}_words.xlsx"))
            else:
                expected_files.append(os.path.join(data_dest, f"{base_name}_words.xlsx"))

        # 8b. TXT & DOCX from Native PDF Text Layer
        if os.path.splitext(file_path)[1].lower() == ".pdf":
            has_text_layer = False
            try:
                doc = fitz.open(file_path)
                tc = sum(len(p.get_text("text").strip()) for p in doc)
                doc.close()
                if tc > 0:
                    has_text_layer = True
            except Exception:
                pass

            if has_text_layer and data_dest:
                if opts.get("export_txt_from_pdf", False):
                    if use_subfolders:
                        expected_files.append(os.path.join(data_dest, "TXT from PDF", f"{base_name}.txt"))
                    else:
                        expected_files.append(os.path.join(data_dest, f"{base_name}_from_pdf.txt"))

                if opts.get("export_docx_from_pdf", False):
                    if use_subfolders:
                        expected_files.append(os.path.join(data_dest, "DOCX from PDF", f"{base_name}.docx"))
                    else:
                        expected_files.append(os.path.join(data_dest, f"{base_name}_from_pdf.docx"))

    # 9. Audit Report Files
    has_report = opts.get("export_audit_report", False) or any([
        opts.get("export_report_json", False),
        opts.get("export_report_csv", False),
        opts.get("export_report_xlsx", False)
    ])
    if has_report and data_dest:
        rep_dir = os.path.join(data_dest, "Audit_Reports") if use_subfolders else data_dest
        if opts.get("export_audit_report", False) or opts.get("export_report_csv", False):
            expected_files.append(os.path.join(rep_dir, "Batch_Processing_Report.csv"))
        if opts.get("export_audit_report", False) or opts.get("export_report_xlsx", False):
            expected_files.append(os.path.join(rep_dir, "Batch_Processing_Report.xlsx"))
        if opts.get("export_report_json", False):
            expected_files.append(os.path.join(rep_dir, "Batch_Processing_Report.json"))

    return list(set(expected_files))


def normalize_path(p):
    if not p:
        return ""
    try:
        if os.path.exists(p):
            return os.path.normcase(os.path.normpath(os.path.realpath(p)))
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def check_input_overwrite_conflict(target_files, output_dir, data_dir=None, export_options=None):
    """
    Checks if any generated output file path matches an input file path.
    Returns a list of tuples: [(input_file_path, conflicting_output_path), ...]
    """
    if not target_files or not output_dir:
        return []

    expected_outputs = get_expected_output_files(target_files, output_dir, data_dir=data_dir, export_options=export_options)

    target_map = {}
    for f in target_files:
        norm_f = normalize_path(f)
        if norm_f:
            target_map[norm_f] = f

    conflicts = []
    for out_p in expected_outputs:
        norm_out = normalize_path(out_p)
        if norm_out in target_map:
            conflicts.append((target_map[norm_out], out_p))
        elif os.path.exists(out_p):
            for norm_f, orig_f in target_map.items():
                if os.path.exists(orig_f):
                    try:
                        if os.path.samefile(orig_f, out_p):
                            conflicts.append((orig_f, out_p))
                            break
                    except Exception:
                        pass

    return conflicts

