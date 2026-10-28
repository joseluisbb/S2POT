"""
S2POT Lite Pipeline Engine
Optimized express single-file OCR processing for S2POTlite.
Generates _OCR.txt, _OCR.docx, and 120 DPI searchable _OCR.pdf while retaining original page colors.
"""

import os
import time
import tempfile
import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image

from core.ocr_engine import OCREngine
from core.pdf_builder import create_searchable_pdf, encode_page_jpeg_bytes
from core.pipeline import (
    apply_color_mode,
    compute_native_text_quality,
    compute_ocr_confidence_pct,
    _rescale_bbox_items
)
from core.data_exporter import _export_docx_file

SUPPORTED_EXTENSIONS = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def inspect_lite_file(file_path):
    """
    Fast inspection of a candidate input file for S2POT Lite.
    Returns:
    {
        "valid": bool,
        "error": str or None,
        "filename": str,
        "ext": str,
        "size_bytes": int,
        "size_mb": float,
        "total_pages": int,
        "summary": str
    }
    """
    if not file_path or not os.path.exists(file_path):
        return {"valid": False, "error": "Ficheiro não encontrado."}

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return {"valid": False, "error": f"Formato '{ext}' não suportado."}

    filename = os.path.basename(file_path)
    size_bytes = os.path.getsize(file_path)
    size_mb = round(size_bytes / (1024.0 * 1024.0), 2)
    total_pages = 1

    try:
        if ext == ".pdf":
            doc = fitz.open(file_path)
            total_pages = len(doc)
            doc.close()
            type_label = "Documento PDF"
        elif ext in {".tif", ".tiff"}:
            with Image.open(file_path) as im:
                total_pages = getattr(im, "n_frames", 1)
            type_label = "Imagem TIFF Multipágina" if total_pages > 1 else "Imagem TIFF"
        else:
            total_pages = 1
            type_label = f"Imagem {ext[1:].upper()}"

        page_str = f"{total_pages} páginas" if total_pages > 1 else "1 página"
        summary = f"{type_label} • {page_str} • {size_mb} MB"

        return {
            "valid": True,
            "error": None,
            "filename": filename,
            "ext": ext,
            "size_bytes": size_bytes,
            "size_mb": size_mb,
            "total_pages": total_pages,
            "summary": summary
        }
    except Exception as e:
        return {"valid": False, "error": f"Erro ao ler ficheiro: {e}"}


def process_lite_file(
    file_path,
    output_dir,
    export_txt=True,
    export_docx=True,
    export_pdf=True,
    target_pdf_dpi=120,
    ocr_lang="por",
    doc_type="printed",
    progress_callback=None,
    abort_check_fn=None
):
    """
    Executes express OCR processing on a single file.
    Supports doc_type="printed" (default RapidOCR) or "handwritten" (Microsoft TrOCR cursive HTR).
    Creates:
      - <base_name>_OCR.txt
      - <base_name>_OCR.docx
      - <base_name>_OCR.pdf (120 DPI, original colors retained, invisible sub-pixel text layer)
    Returns a comprehensive result dictionary with quantitative metrics.
    """
    t0 = time.time()
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Input file not found: {file_path}")

    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    ext = os.path.splitext(file_path)[1].lower()

    txt_out_path = os.path.join(output_dir, f"{base_name}_OCR.txt")
    docx_out_path = os.path.join(output_dir, f"{base_name}_OCR.docx")
    pdf_out_path = os.path.join(output_dir, f"{base_name}_OCR.pdf")

    ocr_engine = OCREngine(preferred_lang=ocr_lang, target_dpi=300, doc_type=doc_type)

    # Determine total pages
    pdf_doc = None
    tiff_img = None
    total_pages = 1

    if ext == ".pdf":
        pdf_doc = fitz.open(file_path)
        total_pages = len(pdf_doc)
    elif ext in {".tif", ".tiff"}:
        tiff_img = Image.open(file_path)
        total_pages = getattr(tiff_img, "n_frames", 1)

    pages_data_for_pdf = []
    pages_data_for_docx = []
    full_plain_text_pages = []
    pages_stats = []

    total_chars = 0
    total_words = 0
    weighted_confidence = 0.0

    try:
        for p_idx in range(total_pages):
            if abort_check_fn and abort_check_fn():
                raise InterruptedError("Processo cancelado pelo utilizador.")

            page_t0 = time.time()
            if progress_callback:
                progress_callback(p_idx + 1, total_pages, f"A processar página {p_idx + 1} de {total_pages}...")

            # 1. Acquire original high-res BGR image for OCR and detect color mode
            bgr_orig = None
            detected_color_mode = "color"
            page_orig_dpi = 300
            direct_text = ""
            direct_words = []

            if pdf_doc is not None:
                page = pdf_doc[p_idx]
                pt_w, pt_h = page.rect.width, page.rect.height
                image_list = page.get_images()
                if image_list:
                    for img_item in image_list:
                        try:
                            base_img = pdf_doc.extract_image(img_item[0])
                            w_i, h_i = base_img.get("width", 0), base_img.get("height", 0)
                            cs = base_img.get("colorspace", 3)
                            if cs == 1:
                                detected_color_mode = "grayscale"
                            if pt_w > 0 and pt_h > 0 and w_i > 0 and h_i > 0:
                                page_orig_dpi = max(page_orig_dpi, int(round((w_i * 72.0) / pt_w)))
                        except Exception:
                            pass

                direct_text = page.get_text("text").strip()
                direct_words = page.get_text("words")

                # Render at high DPI (min 300) for pristine OCR recognition
                render_dpi = max(300, min(page_orig_dpi, 400))
                pix = page.get_pixmap(dpi=render_dpi)
                pil_im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                bgr_orig = cv2.cvtColor(np.array(pil_im), cv2.COLOR_RGB2BGR)

            elif tiff_img is not None:
                tiff_img.seek(p_idx)
                pil_im = tiff_img.convert("RGB")
                bgr_orig = cv2.cvtColor(np.array(pil_im), cv2.COLOR_RGB2BGR)
                page_orig_dpi = 300
            else:
                bgr_orig = cv2.imread(file_path)
                page_orig_dpi = 300

            if bgr_orig is None:
                bgr_orig = np.ones((800, 600, 3), dtype=np.uint8) * 255

            h_orig, w_orig = bgr_orig.shape[:2]

            # Auto-detect if image is practically grayscale (identical R, G, B channels)
            if detected_color_mode == "color" and bgr_orig.ndim == 3:
                diff1 = np.abs(bgr_orig[:, :, 0].astype(np.int16) - bgr_orig[:, :, 1].astype(np.int16))
                diff2 = np.abs(bgr_orig[:, :, 0].astype(np.int16) - bgr_orig[:, :, 2].astype(np.int16))
                if np.mean(diff1) < 2.0 and np.mean(diff2) < 2.0:
                    detected_color_mode = "grayscale"

            # 2. Check native PDF text layer quality vs OCR (skip native text if in handwriting mode)
            native_good, native_conf = compute_native_text_quality(direct_words)
            use_native_text = (len(direct_text) >= 15 and native_good and doc_type != "handwritten")

            ocr_results = []
            plain_text = ""
            illustrations = []
            tables = []
            page_confidence = 95.0

            if use_native_text:
                # Convert native PDF words to OCR result structures
                pt_scale_x = float(w_orig) / float(page.rect.width)
                pt_scale_y = float(h_orig) / float(page.rect.height)
                for w_item in direct_words:
                    x0, y0, x1, y1, word_str = w_item[:5]
                    ocr_results.append({
                        "text": word_str,
                        "bbox": (
                            int(round(x0 * pt_scale_x)),
                            int(round(y0 * pt_scale_y)),
                            int(round(x1 * pt_scale_x)),
                            int(round(y1 * pt_scale_y))
                        ),
                        "font_size_pt": 11.0,
                        "font_weight": "Normal",
                        "font_family": "Sans-Serif",
                        "score": 0.98
                    })
                plain_text = direct_text
                page_confidence = native_conf
            else:
                # Run high-accuracy RapidOCR engine
                ocr_full = ocr_engine.run_ocr_full(bgr_orig, page_num=p_idx + 1, target_dpi=page_orig_dpi)
                ocr_results = ocr_full.get("ocr_results", [])
                plain_text = ocr_full.get("plain_text", "")
                illustrations = ocr_full.get("illustrations", [])
                tables = ocr_full.get("tables", [])
                page_confidence = compute_ocr_confidence_pct(ocr_results)

            n_chars = len(plain_text)
            n_words = len(plain_text.split())
            total_chars += n_chars
            total_words += n_words
            weighted_confidence += page_confidence * max(1, n_chars)
            full_plain_text_pages.append(plain_text)

            # 3. Prepare 120 DPI PDF Page (retaining original colors)
            if export_pdf:
                # Target DPI: 120 DPI
                scale_120 = 120.0 / float(page_orig_dpi)
                nw_120 = max(1, int(round(w_orig * scale_120)))
                nh_120 = max(1, int(round(h_orig * scale_120)))

                bgr_120 = cv2.resize(bgr_orig, (nw_120, nh_120), interpolation=cv2.INTER_AREA)
                bgr_120 = apply_color_mode(bgr_120, detected_color_mode)

                # Rescale OCR word bounding boxes to 120 DPI coordinate space
                scale_x = float(nw_120) / float(w_orig)
                scale_y = float(nh_120) / float(h_orig)
                rescaled_ocr_results, _, _ = _rescale_bbox_items(
                    [dict(x) for x in ocr_results], [], [], scale_x, scale_y
                )

                # Pre-encode JPEG bytes to guarantee image is always directly available
                enc_bytes = encode_page_jpeg_bytes(bgr_120, jpeg_quality=75, color_mode=detected_color_mode)

                pages_data_for_pdf.append({
                    "bgr_image": bgr_120,
                    "image_bgr": bgr_120,
                    "jpeg_bytes": enc_bytes,
                    "width_px": nw_120,
                    "height_px": nh_120,
                    "dpi": 120,
                    "color_mode": detected_color_mode,
                    "ocr_results": rescaled_ocr_results
                })

            # 4. Prepare DOCX data structure
            if export_docx:
                pages_data_for_docx.append({
                    "page_idx": p_idx,
                    "width_px": w_orig,
                    "height_px": h_orig,
                    "illustrations": illustrations,
                    "tables": tables,
                    "ocr_results": ocr_results
                })

            page_elapsed = round(time.time() - page_t0, 2)
            pages_stats.append({
                "page_num": p_idx + 1,
                "chars": n_chars,
                "words": n_words,
                "confidence": page_confidence,
                "color_mode": detected_color_mode,
                "time_s": page_elapsed
            })

    finally:
        if pdf_doc is not None:
            pdf_doc.close()
        if tiff_img is not None:
            tiff_img.close()

    created_files = []

    # 5. Export Plain Text (.txt)
    if export_txt:
        if progress_callback:
            progress_callback(total_pages, total_pages, "A gravar ficheiro de texto...")
        with open(txt_out_path, "w", encoding="utf-8") as f:
            f.write("\n\n--- PÁGINA SEGUINTE ---\n\n".join(full_plain_text_pages))
        if os.path.exists(txt_out_path):
            created_files.append({
                "path": txt_out_path,
                "name": os.path.basename(txt_out_path),
                "size_kb": round(os.path.getsize(txt_out_path) / 1024.0, 1),
                "type": "txt"
            })

    # 6. Export Microsoft Word (.docx)
    if export_docx and pages_data_for_docx:
        if progress_callback:
            progress_callback(total_pages, total_pages, "A gerar documento Word (.docx)...")
        _export_docx_file(docx_out_path, f"{base_name}_OCR", pages_data_for_docx)
        if os.path.exists(docx_out_path):
            created_files.append({
                "path": docx_out_path,
                "name": os.path.basename(docx_out_path),
                "size_kb": round(os.path.getsize(docx_out_path) / 1024.0, 1),
                "type": "docx"
            })

    # 7. Export Searchable PDF at 120 DPI (.pdf)
    if export_pdf and pages_data_for_pdf:
        if progress_callback:
            progress_callback(total_pages, total_pages, "A compilar PDF pesquisável a 120 DPI...")
        create_searchable_pdf(
            pages_data=pages_data_for_pdf,
            output_pdf_path=pdf_out_path,
            compress_background=True,
            jpeg_quality=75
        )
        if os.path.exists(pdf_out_path):
            created_files.append({
                "path": pdf_out_path,
                "name": os.path.basename(pdf_out_path),
                "size_kb": round(os.path.getsize(pdf_out_path) / 1024.0, 1),
                "type": "pdf"
            })

    total_time = round(time.time() - t0, 2)
    avg_conf = round(weighted_confidence / max(1.0, float(total_chars)), 1) if total_chars > 0 else 95.0

    return {
        "success": True,
        "input_file": file_path,
        "output_dir": output_dir,
        "total_pages": total_pages,
        "total_chars": total_chars,
        "total_words": total_words,
        "avg_confidence": avg_conf,
        "elapsed_time": total_time,
        "created_files": created_files,
        "pages_stats": pages_stats,
        "full_text": "\n\n".join(full_plain_text_pages)
    }
