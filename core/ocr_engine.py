import os
import cv2
import numpy as np
from PIL import Image

RAPID_OCR_ENGINE = None
try:
    from rapidocr_onnxruntime import RapidOCR
    RAPID_OCR_ENGINE = RapidOCR()
except Exception as e:
    print(f"RapidOCR initialization warning: {e}")

class OCREngine:
    def __init__(self, preferred_lang="eng", target_dpi=300, doc_type="printed"):
        self.preferred_lang = preferred_lang
        self.target_dpi = target_dpi
        self.doc_type = doc_type

    def run_ocr_full(self, bgr_image, page_num=1, target_dpi=None, doc_type=None):
        """
        Runs pure ONNX OCR on BGR image and returns dict:
        {
          "ocr_results": [{"text": "word", "bbox": (x0, y0, x1, y1), "font_size_pt": 12.0, "font_weight": "Normal", "font_family": "Sans-Serif"}, ...],
          "plain_text": "extracted plain text string",
          "hocr_html": "hOCR XML/HTML format string",
          "illustrations": [crop_bgr_1, crop_bgr_2, ...],
          "tables": [[row1_cells], [row2_cells], ...]
        }
        Supports doc_type="printed" (standard RapidOCR) or "handwritten" (Microsoft TrOCR cursive HTR).
        """
        effective_doc_type = doc_type or self.doc_type
        if effective_doc_type == "handwritten":
            try:
                from core.htr_engine import TrOCREngine
                htr = TrOCREngine.get_instance()
                if htr.is_ready():
                    return htr.process_handwritten_page(bgr_image, page_num=page_num, target_dpi=target_dpi or self.target_dpi)
            except Exception as e:
                print(f"TrOCR engine notice, falling back to standard OCR: {e}")

        dpi = target_dpi or self.target_dpi
        results = []
        lines_text = []

        if RAPID_OCR_ENGINE and bgr_image is not None:
            try:
                ocr_res, _ = RAPID_OCR_ENGINE(bgr_image)
                if ocr_res:
                    for item in ocr_res:
                        pts = np.array(item[0])
                        x0, y0 = np.min(pts, axis=0)
                        x1, y1 = np.max(pts, axis=0)
                        text = str(item[1]).strip()
                        score = float(item[2])

                        if text and score > 0.3:
                            box_x0, box_y0 = int(max(0, x0)), int(max(0, y0))
                            box_x1, box_y1 = int(min(bgr_image.shape[1], x1)), int(min(bgr_image.shape[0], y1))
                            h = box_y1 - box_y0
                            w = box_x1 - box_x0

                            crop_bgr = bgr_image[box_y0:box_y1, box_x0:box_x1] if h > 2 and w > 2 else None
                            font_size_pt, font_weight, font_family = self._estimate_font_properties(crop_bgr, h, dpi)

                            results.append({
                                "text": text,
                                "bbox": (box_x0, box_y0, box_x1, box_y1),
                                "font_size_pt": font_size_pt,
                                "font_weight": font_weight,
                                "font_family": font_family,
                                "score": score
                            })
                            lines_text.append(text)
            except Exception as e:
                print(f"RapidOCR execution error: {e}")

        plain_text = "\n".join(lines_text)
        hocr_html = self._build_hocr_from_boxes(results, bgr_image.shape[1], bgr_image.shape[0], page_num)
        illustrations, tables = self._detect_tables_and_illustrations(bgr_image, results)

        return {
            "ocr_results": results,
            "plain_text": plain_text,
            "hocr_html": hocr_html,
            "illustrations": illustrations,
            "tables": tables
        }

    def run_ocr(self, bgr_image):
        """Legacy helper returning word bounding boxes list."""
        full_res = self.run_ocr_full(bgr_image)
        return full_res["ocr_results"]

    def _estimate_font_properties(self, crop_bgr, box_h_px, dpi):
        """Estimate font size (pt), font weight (Bold/Normal), and font family (Serif/Sans-Serif/Monospace)."""
        raw_pt = (box_h_px * 72.0) / float(dpi)
        font_size_pt = round(max(8.0, min(32.0, raw_pt * 0.72)), 1)
        font_weight = "Normal"
        font_family = "Sans-Serif"

        if crop_bgr is not None and crop_bgr.size > 0:
            gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            ink_density = np.sum(binary > 0) / float(binary.size)
            if ink_density > 0.33:
                font_weight = "Bold"

            h, w = gray.shape
            if w > 0 and h > 0:
                aspect = float(w) / float(h)
                if aspect > 4.5 and ink_density < 0.22:
                    font_family = "Monospace"
                else:
                    sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
                    edge_ratio = np.sum(np.abs(sobel_x) > 50) / float(gray.size)
                    if edge_ratio > 0.18:
                        font_family = "Serif"

        return font_size_pt, font_weight, font_family

    def _detect_tables_and_illustrations(self, bgr_image, ocr_results):
        """
        Extract only genuine non-text graphical illustrations/iconography crops
        (excluding any regions that overlap with extracted OCR text boxes, frames, or noise)
        and detect tabular grid structures.
        """
        illustrations = []
        tables = []

        if bgr_image is None or bgr_image.size == 0:
            return illustrations, tables

        h_img, w_img = bgr_image.shape[:2]
        page_area = float(h_img * w_img)
        if page_area <= 0:
            return illustrations, tables

        # 1. Build generous text mask so text strokes, ascenders/descenders, and fringes are zeroed out
        text_mask = np.ones((h_img, w_img), dtype=np.uint8) * 255
        text_boxes = []
        for item in ocr_results:
            x0, y0, x1, y1 = item["bbox"]
            bw = max(1, x1 - x0)
            bh = max(1, y1 - y0)
            txt = str(item.get("text", "")).strip()
            text_boxes.append((x0, y0, x1, y1, bw * bh, len(txt)))

            pad_x = max(12, int(bh * 0.50))
            pad_y = max(10, int(bh * 0.40))
            x0_m, y0_m = max(0, x0 - pad_x), max(0, y0 - pad_y)
            x1_m, y1_m = min(w_img, x1 + pad_x), min(h_img, y1 + pad_y)
            text_mask[y0_m:y1_m, x0_m:x1_m] = 0

        # 2. Adaptive foreground thresholding (avoids treating paper shading/tint as ink)
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        otsu_val, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        eff_thresh = min(195, max(115, int(otsu_val)))
        _, ink_mask = cv2.threshold(gray, eff_thresh, 255, cv2.THRESH_BINARY_INV)

        # Remove ONLY thin long horizontal/vertical table/frame lines (preserving solid/textured graphics!)
        horiz_k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(45, w_img // 12), 1))
        vert_k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(45, h_img // 12)))
        horiz_lines = cv2.morphologyEx(ink_mask, cv2.MORPH_OPEN, horiz_k)
        vert_lines = cv2.morphologyEx(ink_mask, cv2.MORPH_OPEN, vert_k)
        thick_k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        thick_graphics = cv2.morphologyEx(ink_mask, cv2.MORPH_OPEN, thick_k)
        thin_frame_lines = cv2.bitwise_and(cv2.bitwise_or(horiz_lines, vert_lines), cv2.bitwise_not(thick_graphics))

        graphic_mask = cv2.bitwise_and(ink_mask, cv2.bitwise_not(thin_frame_lines))
        graphic_mask = cv2.bitwise_and(graphic_mask, text_mask)

        # Clean small isolated noise speckles and consolidate coherent graphic elements
        open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        graphic_mask = cv2.morphologyEx(graphic_mask, cv2.MORPH_OPEN, open_k)
        close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        closed_mask = cv2.morphologyEx(graphic_mask, cv2.MORPH_CLOSE, close_k)
        closed_mask = cv2.bitwise_and(closed_mask, text_mask)

        contours, _ = cv2.findContours(closed_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_illustration_area = page_area * 0.012
        min_side = max(65, int(min(h_img, w_img) * 0.055))

        candidate_rects = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = float(w * h)

            # Size & aspect ratio bounds for real iconography / illustrations
            if area < min_illustration_area or w < min_side or h < min_side:
                continue
            if w > w_img * 0.88 or h > h_img * 0.85 or area > page_area * 0.72:
                continue

            aspect = float(w) / float(h)
            if aspect < 0.22 or aspect > 4.2:
                continue

            # Ignore scanner border strips touching outer edges of the page
            if x <= 4 or y <= 4 or (x + w) >= (w_img - 4) or (y + h) >= (h_img - 4):
                if area < page_area * 0.05:
                    continue

            # Strict check: reject if this rectangle overlaps with extracted OCR text boxes
            overlapping_boxes = 0
            overlapping_chars = 0
            total_text_inter = 0.0
            for bx0, by0, bx1, by1, b_area, b_chars in text_boxes:
                ix0 = max(x, bx0)
                iy0 = max(y, by0)
                ix1 = min(x + w, bx1)
                iy1 = min(y + h, by1)
                if ix1 > ix0 and iy1 > iy0:
                    inter = float((ix1 - ix0) * (iy1 - iy0))
                    total_text_inter += inter
                    if inter / float(max(1, b_area)) >= 0.15:
                        overlapping_boxes += 1
                        overlapping_chars += b_chars

            if overlapping_boxes >= 1 and (overlapping_chars >= 2 or (total_text_inter / area) >= 0.015):
                continue
            if (total_text_inter / area) >= 0.02:
                continue

            # Verify genuine graphical/iconographic content inside the core (reject hollow frames & faint stains)
            roi_raw = graphic_mask[y:y+h, x:x+w]
            overall_density = np.count_nonzero(roi_raw) / area
            if overall_density < 0.10 or overall_density > 0.85:
                continue

            ix0, iy0 = x + int(w * 0.12), y + int(h * 0.12)
            ix1, iy1 = x + int(w * 0.88), y + int(h * 0.88)
            core_raw = graphic_mask[iy0:iy1, ix0:ix1]
            if core_raw.size == 0:
                continue
            core_density = np.count_nonzero(core_raw) / float(core_raw.size)
            if core_density < 0.12 or core_density > 0.85:
                continue

            # Check grayscale contrast/variation and connected graphical coherence
            roi_gray = gray[y:y+h, x:x+w]
            if float(np.std(roi_gray)) < 20.0:
                continue

            roi_closed = closed_mask[y:y+h, x:x+w]
            num_labels, _, stats, _ = cv2.connectedComponentsWithStats(roi_closed, connectivity=8)
            if num_labels <= 1:
                continue
            max_cc_area = float(np.max(stats[1:, cv2.CC_STAT_AREA]))
            if (max_cc_area / area) < 0.06:
                continue

            candidate_rects.append((x, y, w, h, area))

        # Deduplicate overlapping / nested illustration rectangles
        candidate_rects.sort(key=lambda r: r[4], reverse=True)
        accepted_rects = []
        for rx, ry, rw, rh, rarea in candidate_rects:
            is_dup = False
            for ax, ay, aw, ah, _ in accepted_rects:
                ix0, iy0 = max(rx, ax), max(ry, ay)
                ix1, iy1 = min(rx + rw, ax + aw), min(ry + rh, ay + ah)
                if ix1 > ix0 and iy1 > iy0:
                    inter = float((ix1 - ix0) * (iy1 - iy0))
                    if inter / rarea > 0.30:
                        is_dup = True
                        break
            if not is_dup:
                accepted_rects.append((rx, ry, rw, rh, rarea))

        # Sort top-to-bottom in reading order
        accepted_rects.sort(key=lambda r: (r[1], r[0]))
        for rx, ry, rw, rh, _ in accepted_rects:
            crop = bgr_image[ry:ry+rh, rx:rx+rw].copy()
            illustrations.append(crop)

        if len(ocr_results) >= 4:
            sorted_boxes = sorted(ocr_results, key=lambda b: (b["bbox"][1], b["bbox"][0]))
            rows = []
            curr_row = []
            last_y0 = None

            for box in sorted_boxes:
                x0, y0, x1, y1 = box["bbox"]
                if last_y0 is None or abs(y0 - last_y0) < 12:
                    curr_row.append(box)
                else:
                    if len(curr_row) >= 2:
                        rows.append(curr_row)
                    curr_row = [box]
                last_y0 = y0
            if len(curr_row) >= 2:
                rows.append(curr_row)

            if len(rows) >= 2:
                table_matrix = []
                for r in rows:
                    r_sorted = sorted(r, key=lambda b: b["bbox"][0])
                    table_matrix.append([b["text"] for b in r_sorted])
                tables.append(table_matrix)

        return illustrations, tables

    def _build_hocr_from_boxes(self, word_boxes, width, height, page_num=1):
        """Constructs standard hOCR XML/HTML string from word bounding boxes."""
        html_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">',
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">',
            ' <head>',
            '  <title></title>',
            '  <meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>',
            '  <meta name="ocr-system" content="S2POT_Smart_Slim_PDF_OCR_Tool" />',
            ' </head>',
            ' <body>',
            f'  <div class="ocr_page" id="page_{page_num}" title="image page; bbox 0 0 {width} {height}">',
            '   <div class="ocr_carea" id="block_1">',
            '    <p class="ocr_par" id="par_1">'
        ]

        for idx, wdict in enumerate(word_boxes):
            wtext = wdict["text"]
            x0, y0, x1, y1 = wdict["bbox"]
            html_lines.append(f'     <span class="ocrx_word" id="word_{idx+1}" title="bbox {x0} {y0} {x1} {y1}">{wtext}</span>')

        html_lines.extend([
            '    </p>',
            '   </div>',
            '  </div>',
            ' </body>',
            '</html>'
        ])
        return "\n".join(html_lines)
