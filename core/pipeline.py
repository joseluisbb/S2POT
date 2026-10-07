import os
import time
import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image
from core.enhancer import enhance_contrast_lab
from core.deskew import auto_align_page
from core.ocr_engine import OCREngine
from core.pdf_builder import create_searchable_pdf
from core.data_exporter import export_file_data, export_batch_reports

def is_native_text_quality_good(direct_words):
    """Evaluates if an embedded native PDF text layer is high-quality or contains corrupt/OCR garbage noise."""
    if not direct_words:
        return False
    if len(direct_words) < 5:
        return True
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
    return ratio <= 0.15


def process_single_file(file_path, output_dir, ocr_engine, data_dir=None, export_options=None, enable_auto_deskew=True, pdf_text_strategy="reuse", target_dpi=300, jpeg_quality=70, progress_callback=None, abort_check_fn=None):
    """
    Processes a single multi-page or image file into searchable PDF (original and/or compressed) and DOCX/data exports.
    Supports PDF, TIFF, JPG, PNG, BMP, WEBP.
    """
    start_time = time.time()
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    ext = os.path.splitext(file_path)[1].lower()

    options = export_options or {}
    if "pdf_text_strategy" in options:
        pdf_text_strategy = options["pdf_text_strategy"]

    pages_data = []
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

            if progress_callback:
                progress_callback(p_idx + 1, total_pages, f"Processing page {p_idx+1}/{total_pages}...")

            page = doc[p_idx]
            direct_text = page.get_text("text").strip()
            direct_words = page.get_text("words")

            pix = page.get_pixmap(dpi=target_dpi)
            if pix.n == 4:
                bgr_resampled = cv2.cvtColor(np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4)), cv2.COLOR_RGBA2BGR)
            else:
                bgr_resampled = cv2.cvtColor(np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3)), cv2.COLOR_RGB2BGR)

            bgr_aligned, rot_deg, skew_deg = auto_align_page(bgr_resampled, enable_deskew=enable_auto_deskew)
            bgr_enhanced = enhance_contrast_lab(bgr_aligned)

            if len(direct_text) > 0 or len(direct_words) > 0:
                has_orig_text_layer = True
                orig_ocr_results = []
                pt_to_px = float(target_dpi) / 72.0
                for w in direct_words:
                    x0, y0, x1, y1, w_text = w[0], w[1], w[2], w[3], w[4]
                    orig_ocr_results.append({
                        "text": w_text,
                        "bbox": [int(round(x0 * pt_to_px)), int(round(y0 * pt_to_px)), int(round(x1 * pt_to_px)), int(round(y1 * pt_to_px))],
                        "font_size_pt": round(max(8.0, min(32.0, y1 - y0)), 1),
                        "font_weight": "Normal",
                        "font_family": "Sans-Serif"
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

            native_quality_good = is_native_text_quality_good(direct_words)
            page_has_text = (len(direct_text) >= 10 and len(direct_words) > 0 and native_quality_good)

            if page_has_text and pdf_text_strategy != "force_ocr":
                ocr_results = orig_ocr_results
                full_plain_text.append(f"--- Page {p_idx+1} ---\n" + direct_text)
                total_words_count += len(ocr_results)
                total_chars_count += len(direct_text)
                full_hocr_pages.append(orig_hocr_html)
                illustrations, tables = [], []
            else:
                ocr_pack = ocr_engine.run_ocr_full(bgr_enhanced, page_num=p_idx+1, target_dpi=target_dpi)
                ocr_results = ocr_pack["ocr_results"]
                illustrations = ocr_pack.get("illustrations", [])
                tables = ocr_pack.get("tables", [])
                total_words_count += len(ocr_results)
                total_chars_count += sum(len(w.get("text", "")) for w in ocr_results)

                p_text = ocr_pack.get("plain_text", "").strip()
                full_plain_text.append(f"--- Page {p_idx+1} ---\n" + p_text)

                h_html = ocr_pack.get("hocr_html", "")
                full_hocr_pages.append(h_html)

            _, jpeg_bytes = cv2.imencode('.jpg', bgr_enhanced, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])

            pages_data.append({
                "page_num": p_idx + 1,
                "jpeg_bytes": jpeg_bytes.tobytes(),
                "bgr_image": bgr_enhanced,
                "width_px": bgr_enhanced.shape[1],
                "height_px": bgr_enhanced.shape[0],
                "dpi": target_dpi,
                "ocr_results": ocr_results,
                "illustrations": illustrations,
                "tables": tables
            })

        doc.close()

    elif ext in [".tif", ".tiff"]:
        tiff = Image.open(file_path)
        total_pages = getattr(tiff, "n_frames", 1)

        for p_idx in range(total_pages):
            if abort_check_fn and abort_check_fn():
                raise InterruptedError("Process aborted by user.")

            if progress_callback:
                progress_callback(p_idx + 1, total_pages, f"Processing page {p_idx+1}/{total_pages}...")

            tiff.seek(p_idx)
            page_img = np.array(tiff.convert('RGB'))
            bgr_full = cv2.cvtColor(page_img, cv2.COLOR_RGB2BGR)

            h_orig, w_orig = bgr_full.shape[:2]
            input_dpi = 300
            if "dpi" in tiff.info and isinstance(tiff.info["dpi"], tuple):
                input_dpi = int(tiff.info["dpi"][0])

            scale = float(target_dpi) / float(input_dpi)
            target_w = max(100, int(w_orig * scale))
            target_h = max(100, int(h_orig * scale))

            bgr_resampled = cv2.resize(bgr_full, (target_w, target_h), interpolation=cv2.INTER_AREA)
            bgr_aligned, rot_deg, skew_deg = auto_align_page(bgr_resampled, enable_deskew=enable_auto_deskew)
            bgr_enhanced = enhance_contrast_lab(bgr_aligned)

            ocr_pack = ocr_engine.run_ocr_full(bgr_enhanced, page_num=p_idx+1, target_dpi=target_dpi)
            ocr_results = ocr_pack["ocr_results"]
            illustrations = ocr_pack.get("illustrations", [])
            tables = ocr_pack.get("tables", [])
            total_words_count += len(ocr_results)
            total_chars_count += sum(len(w.get("text", "")) for w in ocr_results)

            if ocr_pack["plain_text"]:
                full_plain_text.append(f"--- Page {p_idx+1} ---\n" + ocr_pack["plain_text"])
            if ocr_pack["hocr_html"]:
                full_hocr_pages.append(ocr_pack["hocr_html"])

            _, jpeg_bytes = cv2.imencode('.jpg', bgr_enhanced, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])

            pages_data.append({
                "page_num": p_idx + 1,
                "jpeg_bytes": jpeg_bytes.tobytes(),
                "bgr_image": bgr_enhanced,
                "width_px": bgr_enhanced.shape[1],
                "height_px": bgr_enhanced.shape[0],
                "dpi": target_dpi,
                "ocr_results": ocr_results,
                "illustrations": illustrations,
                "tables": tables
            })

    else:
        # Standard images: JPG, PNG, BMP, WEBP
        total_pages = 1
        if progress_callback:
            progress_callback(1, 1, "Processing image file...")

        bgr_full = cv2.imread(file_path)
        if bgr_full is None:
            pil_img = Image.open(file_path).convert("RGB")
            bgr_full = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

        h_orig, w_orig = bgr_full.shape[:2]
        if max(h_orig, w_orig) < 1500:
            scale = 2000.0 / float(max(h_orig, w_orig))
            bgr_full = cv2.resize(bgr_full, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        bgr_aligned, rot_deg, skew_deg = auto_align_page(bgr_full, enable_deskew=enable_auto_deskew)
        bgr_enhanced = enhance_contrast_lab(bgr_aligned)

        ocr_pack = ocr_engine.run_ocr_full(bgr_enhanced, page_num=1, target_dpi=target_dpi)
        ocr_results = ocr_pack["ocr_results"]
        illustrations = ocr_pack.get("illustrations", [])
        tables = ocr_pack.get("tables", [])
        total_words_count += len(ocr_results)
        total_chars_count += sum(len(w.get("text", "")) for w in ocr_results)

        if ocr_pack["plain_text"]:
            full_plain_text.append("--- Page 1 ---\n" + ocr_pack["plain_text"])
        if ocr_pack["hocr_html"]:
            full_hocr_pages.append(ocr_pack["hocr_html"])

        _, jpeg_bytes = cv2.imencode('.jpg', bgr_enhanced, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])

        pages_data.append({
            "page_num": 1,
            "jpeg_bytes": jpeg_bytes.tobytes(),
            "bgr_image": bgr_enhanced,
            "width_px": bgr_enhanced.shape[1],
            "height_px": bgr_enhanced.shape[0],
            "dpi": target_dpi,
            "ocr_results": ocr_results,
            "illustrations": illustrations,
            "tables": tables
        })

    primary_pdf_path = None

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
        create_searchable_pdf(pages_data, out_comp_pdf, compress_background=True, jpeg_quality=options.get("pdf_jpeg_quality", 70))
        if not primary_pdf_path:
            primary_pdf_path = out_comp_pdf

    if not primary_pdf_path:
        primary_pdf_path = os.path.join(output_dir, f"{base_name}.pdf")

    # Export Data & Document Formats (DOCX, TXT, hOCR, JSON, CSV, XLSX, TXT/DOCX from PDF)
    if pdf_text_strategy == "force_ocr" and has_orig_text_layer:
        export_file_data(data_dir or output_dir, f"{base_name}_original", orig_pages_data, orig_plain_text, orig_hocr_pages, options, docs_dir=output_dir, has_orig_text_layer=has_orig_text_layer, orig_plain_text=orig_plain_text, orig_pages_data=orig_pages_data)
    export_file_data(data_dir or output_dir, base_name, pages_data, full_plain_text, full_hocr_pages, options, docs_dir=output_dir, has_orig_text_layer=has_orig_text_layer, orig_plain_text=orig_plain_text, orig_pages_data=orig_pages_data)

    duration = time.time() - start_time
    orig_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    pdf_size_mb = os.path.getsize(primary_pdf_path) / (1024 * 1024) if os.path.exists(primary_pdf_path) else 0.0
    reduction_pct = ((1 - pdf_size_mb / orig_size_mb) * 100) if orig_size_mb > 0 else 0

    record = {
        "filename": base_name,
        "pages": total_pages,
        "tiff_size_mb": orig_size_mb,
        "pdf_size_mb": pdf_size_mb,
        "reduction_pct": reduction_pct,
        "total_words": total_words_count,
        "total_chars": total_chars_count,
        "duration_sec": duration,
        "status": "COMPLETED"
    }

    return primary_pdf_path, record

process_single_tiff = process_single_file

def batch_process_files(file_list, output_dir, data_dir=None, export_options=None, enable_auto_deskew=True, pdf_text_strategy="reuse", target_dpi=300, jpeg_quality=70, progress_callback=None, file_complete_callback=None, abort_check_fn=None):
    os.makedirs(output_dir, exist_ok=True)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)

    ocr_engine = OCREngine(target_dpi=target_dpi)
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

