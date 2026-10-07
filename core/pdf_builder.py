import os
import cv2
import numpy as np
import pymupdf as fitz

def create_searchable_pdf(pages_data, output_pdf_path, compress_background=False, jpeg_quality=70):
    """
    Assembles a searchable PDF document from page data with sub-pixel exact text alignment.
    Uses precise font ascender/descender metrics to align invisible text boxes with underlying images.
    """
    doc = fitz.open()
    font = fitz.Font("helv")
    
    # Standard Helvetica metrics
    ascender = font.ascender if font.ascender > 0 else 0.718
    descender = abs(font.descender) if font.descender < 0 else 0.207
    font_height_factor = ascender + descender # ~0.925

    for page_info in pages_data:
        w_px = page_info["width_px"]
        h_px = page_info["height_px"]
        dpi = page_info.get("dpi", 300)

        pt_w = w_px * 72.0 / float(dpi)
        pt_h = h_px * 72.0 / float(dpi)

        page = doc.new_page(width=pt_w, height=pt_h)
        rect = fitz.Rect(0, 0, pt_w, pt_h)

        image_bytes = page_info.get("jpeg_bytes")

        if compress_background and "bgr_image" in page_info:
            bgr = page_info["bgr_image"]
            if bgr is not None and bgr.size > 0:
                if max(bgr.shape[:2]) > 2000:
                    scale = 2000.0 / float(max(bgr.shape[:2]))
                    bgr = cv2.resize(bgr, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), max(30, min(95, jpeg_quality))]
                _, enc = cv2.imencode('.jpg', bgr, encode_param)
                image_bytes = enc.tobytes()

        if image_bytes:
            page.insert_image(rect, stream=image_bytes)

        scale_x = pt_w / float(w_px)
        scale_y = pt_h / float(h_px)

        for item in page_info.get("ocr_results", []):
            text = item.get("text", "").strip()
            if not text:
                continue

            x0, y0, x1, y1 = item["bbox"]

            box_rect = fitz.Rect(
                x0 * scale_x,
                y0 * scale_y,
                x1 * scale_x,
                y1 * scale_y
            )

            box_w = box_rect.width
            box_h = box_rect.height

            if box_w <= 0 or box_h <= 0:
                continue

            # Exact font size & baseline origin math to match box_h and box_w precisely
            font_size = max(4.0, box_h / font_height_factor)
            origin_y = box_rect.y0 + (ascender * font_size)
            origin_pt = fitz.Point(box_rect.x0, origin_y)

            text_len = font.text_length(text, fontsize=font_size)
            scale_h = (box_w / text_len) if text_len > 0 else 1.0

            page.insert_text(
                origin_pt,
                text,
                fontsize=font_size,
                fontname="helv",
                render_mode=3, # Invisible text overlay
                morph=(origin_pt, fitz.Matrix(scale_h, 1.0)),
                overlay=True
            )

    doc.save(output_pdf_path, garbage=4, deflate=True)
    doc.close()
    return output_pdf_path
