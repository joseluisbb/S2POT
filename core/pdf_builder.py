import os
import cv2
import numpy as np
import pymupdf as fitz

def encode_page_jpeg_bytes(bgr, jpeg_quality=70, color_mode=None):
    """
    Encodes a page image into compact JPEG bytes.
    Uses true 1-channel Grayscale JPEG when the page is grayscale/monochrome (or has identical RGB channels),
    eliminating redundant chroma channels and significantly reducing PDF size without quality loss.
    """
    if bgr is None or bgr.size == 0:
        return b""

    cm = str(color_mode or "").lower()
    is_gray = (
        cm in ("grayscale", "monochrome", "bw", "binarized")
        or bgr.ndim == 2
        or (bgr.ndim == 3 and np.array_equal(bgr[:, :, 0], bgr[:, :, 1]) and np.array_equal(bgr[:, :, 0], bgr[:, :, 2]))
    )
    img_to_encode = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if (is_gray and bgr.ndim == 3) else bgr
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), max(30, min(95, int(jpeg_quality)))]
    ok, enc = cv2.imencode(".jpg", img_to_encode, encode_param)
    return enc.tobytes() if ok else b""


def create_searchable_pdf(pages_data, output_pdf_path, compress_background=False, jpeg_quality=70, max_downsample_dim=None):
    """
    Assembles a searchable PDF document from page data with sub-pixel exact text alignment.
    Uses precise font ascender/descender metrics to align invisible text boxes with underlying images.
    Preserves the exact page Target DPI configured by the user.
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
        cmode = page_info.get("color_mode")

        pt_w = w_px * 72.0 / float(dpi)
        pt_h = h_px * 72.0 / float(dpi)

        page = doc.new_page(width=pt_w, height=pt_h)
        rect = fitz.Rect(0, 0, pt_w, pt_h)

        image_bytes = page_info.get("jpeg_bytes")

        bgr = page_info.get("bgr_image") if "bgr_image" in page_info else page_info.get("image_bgr")
        if compress_background and bgr is not None and bgr.size > 0:
            image_bytes = encode_page_jpeg_bytes(bgr, jpeg_quality=jpeg_quality, color_mode=cmode)

        if image_bytes:
            page.insert_image(rect, stream=image_bytes)

        scale_x = pt_w / float(w_px)
        scale_y = pt_h / float(h_px)

        insert_invisible_text_items(page, page_info.get("ocr_results", []), scale_x, scale_y, font=font)

    doc.save(output_pdf_path, garbage=4, deflate=True)
    doc.close()
    return output_pdf_path


def insert_invisible_text_items(page, items, scale_x=1.0, scale_y=1.0, font=None):
    """
    Inserts invisible (render_mode=3) text items on a PDF page with sub-pixel exact alignment.
    Each item is a dict with "text" and "bbox" (x0, y0, x1, y1) in source units; the bbox is
    multiplied by (scale_x, scale_y) to obtain PDF point coordinates.
    """
    font = font or fitz.Font("helv")
    ascender = font.ascender if font.ascender > 0 else 0.718
    descender = abs(font.descender) if font.descender < 0 else 0.207
    font_height_factor = ascender + descender  # ~0.925

    for item in items or []:
        text = str(item.get("text", "")).strip()
        if not text:
            continue

        x0, y0, x1, y1 = item["bbox"]
        box_rect = fitz.Rect(x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y)
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
            render_mode=3,  # Invisible text overlay
            morph=(origin_pt, fitz.Matrix(scale_h, 1.0)),
            overlay=True
        )
