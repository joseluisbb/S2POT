"""
HTR Engine (Handwritten Text Recognition) using Microsoft TrOCR via ONNX Runtime.
100% offline, local, fast inference on CPU without requiring PyTorch or heavy frameworks.
Specialized for continuous cursive handwriting.
"""

import os
import sys
import numpy as np
from PIL import Image
import cv2

try:
    import onnxruntime as ort
    from tokenizers import Tokenizer
    HTR_AVAILABLE = True
except Exception:
    HTR_AVAILABLE = False


class TrOCREngine:
    """
    TrOCR Engine specialized for continuous cursive handwriting.
    Uses quantized INT8 ONNX models for high accuracy and low CPU footprint.
    Prioritizes trocr_base if available, falling back to trocr_small.
    """
    _instance = None

    def __init__(self, model_dir=None):
        if not HTR_AVAILABLE:
            raise RuntimeError("TrOCR dependencies (onnxruntime, tokenizers) not available.")

        if model_dir is None:
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
                base_dir = os.path.join(sys._MEIPASS, "models")
            else:
                base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

            base_model_dir = os.path.join(base_dir, "trocr_base")
            small_model_dir = os.path.join(base_dir, "trocr_small")

            # Check if trocr_base is complete
            if os.path.exists(os.path.join(base_model_dir, "onnx", "decoder_model_quantized.onnx")):
                model_dir = base_model_dir
            else:
                model_dir = small_model_dir

        self.model_dir = model_dir
        self.enc_path = os.path.join(model_dir, "onnx", "encoder_model_quantized.onnx")
        self.dec_path = os.path.join(model_dir, "onnx", "decoder_model_quantized.onnx")
        self.tok_path = os.path.join(model_dir, "tokenizer.json")

        self.enc_session = None
        self.dec_session = None
        self.tokenizer = None
        self._is_loaded = False
        self._rapid_ocr = None

    @classmethod
    def get_instance(cls, model_dir=None):
        if cls._instance is None:
            cls._instance = cls(model_dir=model_dir)
        return cls._instance

    def load(self):
        """Loads ONNX models and tokenizer into memory if not already loaded."""
        if self._is_loaded:
            return

        if not (os.path.exists(self.enc_path) and os.path.exists(self.dec_path) and os.path.exists(self.tok_path)):
            raise FileNotFoundError(
                f"TrOCR model files not found in {self.model_dir}. "
                "Ensure encoder, decoder, and tokenizer.json are present."
            )

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = max(1, os.cpu_count() - 1)

        self.tokenizer = Tokenizer.from_file(self.tok_path)
        self.enc_session = ort.InferenceSession(self.enc_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.dec_session = ort.InferenceSession(self.dec_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self._is_loaded = True

    def _get_detector(self):
        """Lazily loads RapidOCR DBNet detector for state-of-the-art text line segmentation."""
        if self._rapid_ocr is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._rapid_ocr = RapidOCR()
            except Exception:
                self._rapid_ocr = False
        return self._rapid_ocr if self._rapid_ocr is not False else None

    def _preprocess_image(self, img_input):
        """
        Accepts PIL.Image or numpy BGR/RGB array and formats for TrOCR (384x384 float32).
        Applies subtle contrast normalization to enhance faint cursive ink strokes.
        """
        if isinstance(img_input, np.ndarray):
            if len(img_input.shape) == 2:
                img_rgb = cv2.cvtColor(img_input, cv2.COLOR_GRAY2RGB)
            elif img_input.shape[2] == 3:
                img_rgb = cv2.cvtColor(img_input, cv2.COLOR_BGR2RGB)
            elif img_input.shape[2] == 4:
                img_rgb = cv2.cvtColor(img_input, cv2.COLOR_BGRA2RGB)
            else:
                img_rgb = img_input

            # Optional local contrast enhancement (CLAHE) for ink visibility
            try:
                lab = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2LAB)
                l, a, b = cv2.split(lab)
                clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
                cl = clahe.apply(l)
                img_rgb = cv2.cvtColor(cv2.merge((cl, a, b)), cv2.COLOR_LAB2RGB)
            except Exception:
                pass

            pil_img = Image.fromarray(img_rgb)
        elif isinstance(img_input, Image.Image):
            pil_img = img_input.convert("RGB")
        else:
            raise ValueError(f"Unsupported image input type: {type(img_input)}")

        # TrOCR Vision Transformer was trained with direct 384x384 bilinear interpolation
        resized = pil_img.resize((384, 384), Image.Resampling.BILINEAR)
        arr = np.array(resized).astype(np.float32) / 255.0
        arr = (arr - 0.5) / 0.5
        arr = np.transpose(arr, (2, 0, 1))
        return np.expand_dims(arr, 0).astype(np.float32)

    def recognize_line(self, img_input, max_tokens=64, repetition_penalty=1.25):
        """
        Recognizes a single line of handwritten cursive text.
        Includes repetition penalty and repetition suppression to avoid infinite loops.
        """
        if not self._is_loaded:
            self.load()

        pixel_values = self._preprocess_image(img_input)

        enc_out = self.enc_session.run(None, {"pixel_values": pixel_values})
        encoder_hidden_states = enc_out[0]

        curr_ids = np.array([[2]], dtype=np.int64)
        token_confidences = []

        for _ in range(max_tokens):
            dec_out = self.dec_session.run(["logits"], {
                "input_ids": curr_ids,
                "encoder_hidden_states": encoder_hidden_states
            })
            logits = dec_out[0][0, -1, :].copy()

            # Apply repetition penalty to already produced tokens
            if repetition_penalty != 1.0:
                for prev_id in set(curr_ids[0].tolist()):
                    if logits[prev_id] > 0:
                        logits[prev_id] /= repetition_penalty
                    else:
                        logits[prev_id] *= repetition_penalty

            # Suppress infinite token repeats (prevent 2 consecutive identical tokens)
            if len(curr_ids[0]) >= 2 and curr_ids[0][-1] == curr_ids[0][-2]:
                logits[curr_ids[0][-1]] = -1e9

            exp_logits = np.exp(logits - np.max(logits))
            probs = exp_logits / np.sum(exp_logits)
            next_id = int(np.argmax(probs))
            prob = float(probs[next_id])

            if next_id == 2:  # EOS token
                break

            token_confidences.append(prob)
            curr_ids = np.concatenate([curr_ids, [[next_id]]], axis=1)

        raw_text = self.tokenizer.decode(curr_ids[0].tolist(), skip_special_tokens=True)
        text = raw_text.replace(" .", ".").replace(" ,", ",").replace(" !", "!").replace(" ?", "?").strip()
        avg_conf = float(np.mean(token_confidences)) if token_confidences else 0.85
        return text, avg_conf

    def detect_lines(self, bgr_image):
        """
        Segments cursive handwritten lines.
        Uses RapidOCR DBNet as primary line detector, falling back to morphology if needed.
        Returns a list of bounding boxes: [(x, y, w, h), ...] sorted top-to-bottom.
        """
        if bgr_image is None or bgr_image.size == 0:
            return []

        detector = self._get_detector()
        if detector is not None:
            try:
                dt_boxes, _ = detector.text_det(bgr_image)
                if dt_boxes is not None and len(dt_boxes) > 0:
                    sorted_boxes = detector.sorted_boxes(dt_boxes)
                    line_boxes = []
                    for box in sorted_boxes:
                        xs = [pt[0] for pt in box]
                        ys = [pt[1] for pt in box]
                        x0 = max(0, int(min(xs)))
                        y0 = max(0, int(min(ys)))
                        x1 = min(bgr_image.shape[1], int(max(xs)))
                        y1 = min(bgr_image.shape[0], int(max(ys)))
                        bw = x1 - x0
                        bh = y1 - y0
                        if bw >= 15 and bh >= 8:
                            line_boxes.append((x0, y0, bw, bh))
                    if line_boxes:
                        return line_boxes
            except Exception as e:
                print(f"DBNet text_det fallback notice: {e}")

        # Fallback: Morphological line detection with safety bounds
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape

        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        kernel_w = max(20, int(w * 0.025))
        kernel_h = max(2, int(h * 0.003))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, kernel_h))
        dilated = cv2.dilate(binary, kernel, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        line_boxes = []

        min_w = max(20, int(w * 0.03))
        min_h = max(8, int(h * 0.006))
        max_h = int(h * 0.40)  # Never allow a single box to span more than 40% of the page height

        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            if bw >= min_w and min_h <= bh <= max_h:
                pad_x = int(bw * 0.02)
                pad_y = int(bh * 0.10)
                x0 = max(0, x - pad_x)
                y0 = max(0, y - pad_y)
                x1 = min(w, x + bw + pad_x)
                y1 = min(h, y + bh + pad_y)
                line_boxes.append((x0, y0, x1 - x0, y1 - y0))

        line_boxes.sort(key=lambda b: (b[1], b[0]))
        return line_boxes

    def process_handwritten_page(self, bgr_image, page_num=1, target_dpi=300):
        """
        Full page processing for cursive handwriting.
        Detects lines with DBNet deep learning segmentation and transcribes them with TrOCR.
        Returns a dict matching OCREngine.run_ocr_full structure.
        """
        if bgr_image is None or bgr_image.size == 0:
            return {
                "ocr_results": [],
                "plain_text": "",
                "hocr_html": "",
                "illustrations": [],
                "tables": []
            }

        img_h, img_w = bgr_image.shape[:2]
        detector = self._get_detector()

        crops_and_boxes = []
        if detector is not None:
            try:
                dt_boxes, _ = detector.text_det(bgr_image)
                if dt_boxes is not None and len(dt_boxes) > 0:
                    sorted_boxes = detector.sorted_boxes(dt_boxes)
                    crops = detector.get_crop_img_list(bgr_image, sorted_boxes)
                    for box, crop in zip(sorted_boxes, crops):
                        xs = [pt[0] for pt in box]
                        ys = [pt[1] for pt in box]
                        x0 = max(0, int(min(xs)))
                        y0 = max(0, int(min(ys)))
                        x1 = min(img_w, int(max(xs)))
                        y1 = min(img_h, int(max(ys)))
                        bw = x1 - x0
                        bh = y1 - y0
                        if bw >= 10 and bh >= 6 and crop.size > 0:
                            crops_and_boxes.append((crop, (x0, y0, bw, bh)))
            except Exception as e:
                print(f"TrOCR DBNet line extraction notice: {e}")

        # Fallback if DBNet returned nothing
        if not crops_and_boxes:
            boxes = self.detect_lines(bgr_image)
            for x, y, bw, bh in boxes:
                crop = bgr_image[y:y+bh, x:x+bw]
                if crop.size > 0:
                    crops_and_boxes.append((crop, (x, y, bw, bh)))

        results = []
        lines_text = []

        for crop, (x, y, bw, bh) in crops_and_boxes:
            try:
                text, conf = self.recognize_line(crop)
                if text and conf > 0.25:
                    raw_pt = (bh * 72.0) / float(target_dpi)
                    font_size_pt = round(max(9.0, min(28.0, raw_pt * 0.70)), 1)

                    results.append({
                        "text": text,
                        "bbox": (int(x), int(y), int(x + bw), int(y + bh)),
                        "font_size_pt": font_size_pt,
                        "font_weight": "Normal",
                        "font_family": "Handwriting",
                        "score": conf
                    })
                    lines_text.append(text)
            except Exception as e:
                print(f"TrOCR line recognition notice: {e}")

        plain_text = "\n".join(lines_text)

        # Generate minimal hOCR for compatibility
        hocr_lines = []
        for idx, item in enumerate(results):
            x0, y0, x1, y1 = item["bbox"]
            txt = item["text"]
            hocr_lines.append(
                f'<span class="ocr_line" id="line_{page_num}_{idx}" title="bbox {x0} {y0} {x1} {y1}; baseline 0 0">'
                f'{txt}</span>'
            )
        hocr_html = (
            f'<div class="ocr_page" id="page_{page_num}" title="bbox 0 0 {img_w} {img_h}">'
            + "\n".join(hocr_lines)
            + '</div>'
        )

        return {
            "ocr_results": results,
            "plain_text": plain_text,
            "hocr_html": hocr_html,
            "illustrations": [],
            "tables": []
        }

    def is_ready(self):
        """Checks if TrOCR weights are downloaded and ready to use."""
        return os.path.exists(self.enc_path) and os.path.exists(self.dec_path) and os.path.exists(self.tok_path)
