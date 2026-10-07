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
    def __init__(self, preferred_lang="eng", target_dpi=300):
        self.preferred_lang = preferred_lang
        self.target_dpi = target_dpi

    def run_ocr_full(self, bgr_image, page_num=1, target_dpi=None):
        """
        Runs pure ONNX OCR on BGR image and returns dict:
        {
          "ocr_results": [{"text": "word", "bbox": (x0, y0, x1, y1), "font_size_pt": 12.0, "font_weight": "Normal", "font_family": "Sans-Serif"}, ...],
          "plain_text": "extracted plain text string",
          "hocr_html": "hOCR XML/HTML format string",
          "illustrations": [crop_bgr_1, crop_bgr_2, ...],
          "tables": [[row1_cells], [row2_cells], ...]
        }
        """
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
        Extract non-text graphical illustration crops and detect tabular grid structures.
        """
        illustrations = []
        tables = []

        if bgr_image is None or bgr_image.size == 0:
            return illustrations, tables

        h_img, w_img = bgr_image.shape[:2]
        mask = np.ones((h_img, w_img), dtype=np.uint8) * 255

        for item in ocr_results:
            x0, y0, x1, y1 = item["bbox"]
            margin = 2
            x0_m, y0_m = max(0, x0 - margin), max(0, y0 - margin)
            x1_m, y1_m = min(w_img, x1 + margin), min(h_img, y1 + margin)
            mask[y0_m:y1_m, x0_m:x1_m] = 0

        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY_INV)
        graphic_mask = cv2.bitwise_and(thresh, mask)

        contours, _ = cv2.findContours(graphic_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_illustration_area = (h_img * w_img) * 0.005

        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            if area > min_illustration_area and w > 40 and h > 40 and w < w_img * 0.95 and h < h_img * 0.95:
                crop = bgr_image[y:y+h, x:x+w].copy()
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
