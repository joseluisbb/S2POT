import os
import csv
import json
import cv2
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

try:
    import docx
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except Exception as e:
    docx = None
    print(f"python-docx initialization notice: {e}")

def export_file_data(data_dir, base_name, pages_data, full_plain_text, full_hocr_pages, options, docs_dir=None, has_orig_text_layer=False, orig_plain_text=None, orig_pages_data=None):
    """
    Exports single file data according to selected options.
    - Word (.docx) documents are stored in docs_dir (Documents Folder), using 'Word' subfolder if organize_subfolders is True.
    - Data exports (.txt, .hocr, .json, .csv, .xlsx, TXT/DOCX from PDF) are stored in data_dir (Data Folder).
    """
    target_docs_dir = docs_dir or data_dir
    if not target_docs_dir:
        target_docs_dir = data_dir

    if data_dir and not os.path.exists(data_dir):
        os.makedirs(data_dir, exist_ok=True)
    if target_docs_dir and not os.path.exists(target_docs_dir):
        os.makedirs(target_docs_dir, exist_ok=True)

    use_subfolders = options.get("organize_subfolders", False)

    # 1. Plain Text Export (.txt)
    if options.get("export_txt") and data_dir:
        target_dir = os.path.join(data_dir, "Text") if use_subfolders else data_dir
        os.makedirs(target_dir, exist_ok=True)
        txt_path = os.path.join(target_dir, f"{base_name}.txt")
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(full_plain_text))
        except Exception as e:
            print(f"Error saving TXT file: {e}")

    # 1b. TXT from Native PDF Text Layer (.txt)
    if options.get("export_txt_from_pdf") and has_orig_text_layer and orig_plain_text and data_dir:
        target_dir = os.path.join(data_dir, "TXT from PDF") if use_subfolders else data_dir
        os.makedirs(target_dir, exist_ok=True)
        txt_pdf_name = f"{base_name}.txt" if use_subfolders else f"{base_name}_from_pdf.txt"
        txt_pdf_path = os.path.join(target_dir, txt_pdf_name)
        try:
            with open(txt_pdf_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(orig_plain_text))
        except Exception as e:
            print(f"Error saving TXT from PDF file: {e}")

    # 2. hOCR Document Export (.hocr)
    if options.get("export_hocr") and data_dir:
        target_dir = os.path.join(data_dir, "hOCR") if use_subfolders else data_dir
        os.makedirs(target_dir, exist_ok=True)
        hocr_path = os.path.join(target_dir, f"{base_name}.hocr")
        try:
            with open(hocr_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(full_hocr_pages))
        except Exception as e:
            print(f"Error saving hOCR file: {e}")

    # 3. Microsoft Word Export (.docx) -> SAVED IN DOCUMENTS FOLDER (docs_dir)
    if options.get("export_docx") and docx is not None and target_docs_dir:
        organize_docs = options.get("organize_doc_subfolders", False) or use_subfolders
        target_docx_dir = os.path.join(target_docs_dir, "DOCX") if organize_docs else target_docs_dir
        os.makedirs(target_docx_dir, exist_ok=True)
        docx_path = os.path.join(target_docx_dir, f"{base_name}.docx")
        try:
            _export_docx_file(docx_path, base_name, pages_data)
        except Exception as e:
            print(f"Error saving DOCX file: {e}")

    # 3b. DOCX from Native PDF Text Layer (.docx) -> SAVED IN DATA FOLDER
    if options.get("export_docx_from_pdf") and has_orig_text_layer and orig_pages_data and docx is not None and data_dir:
        target_docx_pdf_dir = os.path.join(data_dir, "DOCX from PDF") if use_subfolders else data_dir
        os.makedirs(target_docx_pdf_dir, exist_ok=True)
        docx_pdf_name = f"{base_name}.docx" if use_subfolders else f"{base_name}_from_pdf.docx"
        docx_pdf_path = os.path.join(target_docx_pdf_dir, docx_pdf_name)
        try:
            _export_docx_file(docx_pdf_path, base_name if use_subfolders else f"{base_name}_from_pdf", orig_pages_data)
        except Exception as e:
            print(f"Error saving DOCX from PDF file: {e}")

    # Build word list for word bounding box exports
    words_list = []
    word_counter = 1
    for p_info in pages_data:
        p_num = p_info.get("page_num", 1)
        for item in p_info.get("ocr_results", []):
            x0, y0, x1, y1 = item["bbox"]
            words_list.append({
                "filename": base_name,
                "page": p_num,
                "word_num": word_counter,
                "text": item["text"],
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "width": x1 - x0,
                "height": y1 - y0,
                "font_size_pt": item.get("font_size_pt", 12.0),
                "font_weight": item.get("font_weight", "Normal"),
                "font_family": item.get("font_family", "Sans-Serif")
            })
            word_counter += 1

    target_words_dir = os.path.join(data_dir, "Word_Coordinates") if (data_dir and use_subfolders) else data_dir

    # 4. Bounding Boxes JSON (.json)
    if options.get("export_words_json") and data_dir:
        os.makedirs(target_words_dir, exist_ok=True)
        json_path = os.path.join(target_words_dir, f"{base_name}_words.json")
        try:
            payload = {
                "filename": base_name,
                "total_words": len(words_list),
                "total_pages": len(pages_data),
                "words": words_list
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"Error saving Words JSON file: {e}")

    # 5. Bounding Boxes CSV (.csv)
    if options.get("export_words_csv") and data_dir:
        os.makedirs(target_words_dir, exist_ok=True)
        csv_path = os.path.join(target_words_dir, f"{base_name}_words.csv")
        try:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Filename", "Page", "Word_Number", "Word_Text", "X0_px", "Y0_px", "X1_px", "Y1_px", "Width_px", "Height_px", "Font_Size_pt", "Font_Weight", "Font_Family"])
                for w in words_list:
                    writer.writerow([w["filename"], w["page"], w["word_num"], w["text"], w["x0"], w["y0"], w["x1"], w["y1"], w["width"], w["height"], w["font_size_pt"], w["font_weight"], w["font_family"]])
        except Exception as e:
            print(f"Error saving Words CSV file: {e}")

    # 6. Bounding Boxes Excel (.xlsx)
    if options.get("export_words_xlsx") and data_dir:
        os.makedirs(target_words_dir, exist_ok=True)
        xlsx_path = os.path.join(target_words_dir, f"{base_name}_words.xlsx")
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "OCR Word Coordinates"

            headers = ["Filename", "Page", "Word Number", "Word Text", "X0 (px)", "Y0 (px)", "X1 (px)", "Y1 (px)", "Width (px)", "Height (px)", "Font Size (pt)", "Weight", "Family"]
            ws.append(headers)

            header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
            header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col_idx)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

            for w in words_list:
                ws.append([w["filename"], w["page"], w["word_num"], w["text"], w["x0"], w["y0"], w["x1"], w["y1"], w["width"], w["height"], w["font_size_pt"], w["font_weight"], w["font_family"]])

            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                col_letter = get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

            wb.save(xlsx_path)
        except Exception as e:
            print(f"Error saving Words Excel file: {e}")


import statistics

def _export_docx_file(docx_path, base_name, pages_data):
    """Build formatted Microsoft Word (.docx) document with optimized font properties, line normalization, and tight pagination."""
    if docx is None:
        return

    doc = docx.Document()

    sections = doc.sections
    for s in sections:
        s.top_margin = Inches(0.5)
        s.bottom_margin = Inches(0.5)
        s.left_margin = Inches(0.6)
        s.right_margin = Inches(0.6)

    temp_img_files = []

    for p_idx, p_info in enumerate(pages_data):
        if p_idx > 0:
            doc.add_page_break()

        illustrations = p_info.get("illustrations", [])
        for ill_idx, crop_bgr in enumerate(illustrations):
            if crop_bgr is not None and crop_bgr.size > 0:
                temp_path = f"_temp_ill_{p_idx+1}_{ill_idx+1}.png"
                cv2.imwrite(temp_path, crop_bgr)
                temp_img_files.append(temp_path)
                try:
                    p_img = doc.add_paragraph()
                    p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p_img.paragraph_format.space_before = Pt(0)
                    p_img.paragraph_format.space_after = Pt(1)
                    run = p_img.add_run()
                    run.add_picture(temp_path, width=Inches(4.5))
                except Exception as e:
                    print(f"DOCX illustration insert error: {e}")

        tables = p_info.get("tables", [])
        for tbl_matrix in tables:
            if tbl_matrix and len(tbl_matrix) > 0:
                n_rows = len(tbl_matrix)
                n_cols = max(len(r) for r in tbl_matrix)
                if n_cols > 0:
                    tbl = doc.add_table(rows=n_rows, cols=n_cols)
                    tbl.style = 'Table Grid'
                    for r_i, r_data in enumerate(tbl_matrix):
                        for c_i, val in enumerate(r_data):
                            if c_i < n_cols:
                                tbl.cell(r_i, c_i).text = val
                    p_tbl_space = doc.add_paragraph()
                    p_tbl_space.paragraph_format.space_before = Pt(0)
                    p_tbl_space.paragraph_format.space_after = Pt(1)

        ocr_results = p_info.get("ocr_results", [])
        if ocr_results:
            # Sort bounding boxes top-to-bottom, left-to-right
            sorted_boxes = sorted(ocr_results, key=lambda b: (b["bbox"][1], b["bbox"][0]))
            lines = []
            curr_line = []
            curr_y_mid = None

            for box in sorted_boxes:
                x0, y0, x1, y1 = box["bbox"]
                box_y_mid = (y0 + y1) / 2.0

                # Robust line grouping threshold based on midpoint vertical proximity
                if curr_y_mid is None or abs(box_y_mid - curr_y_mid) < 18:
                    curr_line.append(box)
                    curr_y_mid = sum((b["bbox"][1] + b["bbox"][3]) / 2.0 for b in curr_line) / float(len(curr_line))
                else:
                    if curr_line:
                        lines.append(curr_line)
                    curr_line = [box]
                    curr_y_mid = box_y_mid
            if curr_line:
                lines.append(curr_line)

            # Adaptive vertical height budget scaling factor per page
            scale_budget = 1.0
            if len(lines) > 35:
                scale_budget = max(0.78, min(1.0, 36.0 / float(len(lines))))

            for line in lines:
                line_words = sorted(line, key=lambda b: b["bbox"][0])
                p = doc.add_paragraph()
                p.paragraph_format.line_spacing = 1.0
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(1.0)

                # Compute line-level median font size to prevent word-level font size jittering
                line_sizes = [b.get("font_size_pt", 10.0) for b in line_words]
                med_size = statistics.median(line_sizes) if line_sizes else 10.0

                # Standardize body text font sizes to realistic document typography
                if med_size <= 11.5:
                    norm_line_size = 9.5
                elif med_size <= 14.0:
                    norm_line_size = 11.0
                elif med_size <= 18.0:
                    norm_line_size = 13.0
                else:
                    norm_line_size = min(18.0, med_size * 0.70)

                # Apply adaptive page height budget scale
                norm_line_size = max(7.5, round(norm_line_size * scale_budget, 1))

                for w_idx, wdict in enumerate(line_words):
                    wtext = wdict["text"] + (" " if w_idx < len(line_words) - 1 else "")
                    run = p.add_run(wtext)

                    w_raw_size = wdict.get("font_size_pt", 10.0)
                    if abs(w_raw_size - med_size) < 4.0:
                        final_size = norm_line_size
                    else:
                        ratio = w_raw_size / max(1.0, med_size)
                        final_size = max(7.5, min(22.0, norm_line_size * ratio))

                    run.font.size = Pt(final_size)

                    if wdict.get("font_weight") == "Bold":
                        run.font.bold = True

                    f_fam = wdict.get("font_family", "Sans-Serif")
                    if f_fam == "Serif":
                        run.font.name = "Times New Roman"
                    elif f_fam == "Monospace":
                        run.font.name = "Courier New"
                    else:
                        run.font.name = "Calibri"

    doc.save(docx_path)

    for tmp_p in temp_img_files:
        if os.path.exists(tmp_p):
            try:
                os.remove(tmp_p)
            except Exception:
                pass


def export_batch_reports(data_dir, report_records, options):
    """Exports aggregate batch execution reports according to selected report options."""
    if not os.path.exists(data_dir) or not report_records or not options:
        return

    # Check if Audit Report export option is enabled
    has_report_opt = options.get("export_audit_report", False) or any([
        options.get("export_report_json", False),
        options.get("export_report_csv", False),
        options.get("export_report_xlsx", False)
    ])

    if not has_report_opt:
        return  # Do NOT create Audit_Reports directory or produce report files if not selected

    use_subfolders = options.get("organize_subfolders", False)
    target_reports_dir = os.path.join(data_dir, "Audit_Reports") if use_subfolders else data_dir
    os.makedirs(target_reports_dir, exist_ok=True)

    headers = ["Filename", "Pages", "Original TIFF (MiB)", "Final PDF (MiB)", "Reduction %", "Total Words", "Total Chars", "Duration (sec)", "Status"]

    export_json = options.get("export_report_json", False)
    export_csv = options.get("export_report_csv") or options.get("export_audit_report", False)
    export_xlsx = options.get("export_report_xlsx") or options.get("export_audit_report", False)

    if export_json:
        json_path = os.path.join(target_reports_dir, "Batch_Processing_Report.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({"total_files": len(report_records), "records": report_records}, f, indent=2)
        except Exception as e:
            print(f"Error saving Batch Report JSON: {e}")

    if export_csv:
        csv_path = os.path.join(target_reports_dir, "Batch_Processing_Report.csv")
        try:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for r in report_records:
                    writer.writerow([
                        r["filename"], r["pages"], f"{r['tiff_size_mb']:.2f}",
                        f"{r['pdf_size_mb']:.2f}", f"{r['reduction_pct']:.1f}%",
                        r["total_words"], r.get("total_chars", 0), f"{r['duration_sec']:.1f}", r["status"]
                    ])
        except Exception as e:
            print(f"Error saving Batch Report CSV: {e}")

    if export_xlsx:
        xlsx_path = os.path.join(target_reports_dir, "Batch_Processing_Report.xlsx")
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Batch Summary Report"

            ws.append(headers)
            header_fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
            header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col_idx)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

            tot_tiff = 0
            tot_pdf = 0
            tot_words = 0
            tot_chars = 0
            tot_pages = 0

            for r in report_records:
                tot_tiff += r['tiff_size_mb']
                tot_pdf += r['pdf_size_mb']
                tot_words += r['total_words']
                tot_chars += r.get('total_chars', 0)
                tot_pages += r['pages']

                ws.append([
                    r["filename"], r["pages"], round(r['tiff_size_mb'], 2),
                    round(r['pdf_size_mb'], 2), round(r['reduction_pct'], 1),
                    r["total_words"], r.get("total_chars", 0), round(r['duration_sec'], 1), r["status"]
                ])

            tot_saving = ((1 - tot_pdf / tot_tiff) * 100) if tot_tiff > 0 else 0
            total_row = ["TOTALS / SUMMARY", tot_pages, round(tot_tiff, 2), round(tot_pdf, 2), round(tot_saving, 1), tot_words, tot_chars, "-", "COMPLETED"]
            ws.append(total_row)

            tot_row_idx = len(report_records) + 2
            tot_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
            tot_font = Font(name="Calibri", size=11, bold=True)
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=tot_row_idx, column=col_idx)
                cell.fill = tot_fill
                cell.font = tot_font

            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                col_letter = get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

            wb.save(xlsx_path)
        except Exception as e:
            print(f"Error saving Batch Report Excel: {e}")
