import cv2
import numpy as np

def enhance_contrast_lab(bgr, clip_limit=2.0, bg_radius=31, dpi=300):
    """
    Selective Ink Contrast Enhancement with DPI-Adaptive Paper Background Normalization.
    - Estimates smooth paper background illumination map using DPI-scaled morphological closing.
    - Suppresses high-frequency 8x8 JPEG block grain in paper background while preserving historical paper tone.
    - Selectively boosts ink stroke contrast and edge sharpness without amplifying paper spots or scanner noise.
    """
    if bgr is None or bgr.size == 0:
        return bgr

    is_gray = (
        bgr.ndim == 2
        or (bgr.ndim == 3 and np.array_equal(bgr[:, :, 0], bgr[:, :, 1]) and np.array_equal(bgr[:, :, 0], bgr[:, :, 2]))
    )

    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    L, a, b = cv2.split(lab)

    # Scale morphological and smoothing radii proportionally to page DPI
    scale_f = max(0.4, min(2.5, float(dpi or 300) / 300.0))
    eff_bg_radius = max(15, int(round(bg_radius * scale_f)) | 1)
    blur_k = max(7, int(round(15 * scale_f)) | 1)

    # 1. Background Illumination Map (local paper tone envelope)
    kernel_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (eff_bg_radius, eff_bg_radius))
    L_bg_map = cv2.morphologyEx(L, cv2.MORPH_CLOSE, kernel_bg)
    L_bg_map = cv2.GaussianBlur(L_bg_map, (blur_k, blur_k), 0)

    # 2. Smooth paper background texture (suppresses high-frequency 8x8 DCT grain while keeping natural paper shading)
    L_bilat = cv2.bilateralFilter(L, d=9, sigmaColor=42, sigmaSpace=11)
    L_smooth_bg = cv2.addWeighted(L_bilat, 0.55, L_bg_map, 0.45, 0)
    L_smooth_bg = cv2.GaussianBlur(L_smooth_bg, (3, 3), 0)

    # 3. Soft Ink / Foreground Mask Weight (high sensitivity on strokes & illustrations, 0 on paper noise)
    ink_diff = np.maximum(0.0, L_bg_map.astype(np.float32) - L.astype(np.float32))
    noise_floor = 4.0
    ink_diff = np.maximum(0.0, ink_diff - noise_floor)
    ink_weight = cv2.normalize(ink_diff, None, 0.0, 1.0, cv2.NORM_MINMAX)
    ink_weight = np.clip(ink_weight * 2.8, 0.0, 1.0)

    # 4. CLAHE contrast boost on luminance for crisp ink strokes
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    L_clahe = clahe.apply(L)

    # 5. Soft-blend: Ink gets sharp contrast boost, paper background gets low-variance smooth texture
    L_final = (
        ink_weight * L_clahe.astype(np.float32)
        + (1.0 - ink_weight) * L_smooth_bg.astype(np.float32)
    ).astype(np.uint8)

    if is_gray:
        return cv2.cvtColor(L_final, cv2.COLOR_GRAY2BGR)

    # Smooth chromaticity channels in paper background to eliminate color speckle in 8x8 JPEG blocks
    a_bilat = cv2.bilateralFilter(a, d=7, sigmaColor=25, sigmaSpace=9)
    b_bilat = cv2.bilateralFilter(b, d=7, sigmaColor=25, sigmaSpace=9)
    a_env = cv2.GaussianBlur(a_bilat, (blur_k, blur_k), 0)
    b_env = cv2.GaussianBlur(b_bilat, (blur_k, blur_k), 0)

    a_final = (ink_weight * a_bilat.astype(np.float32) + (1.0 - ink_weight) * a_env.astype(np.float32)).astype(np.uint8)
    b_final = (ink_weight * b_bilat.astype(np.float32) + (1.0 - ink_weight) * b_env.astype(np.float32)).astype(np.uint8)

    lab_final = cv2.merge([L_final, a_final, b_final])
    return cv2.cvtColor(lab_final, cv2.COLOR_LAB2BGR)
