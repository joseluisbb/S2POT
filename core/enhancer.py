import cv2
import numpy as np

def enhance_contrast_lab(bgr, clip_limit=2.0, bg_radius=31):
    """
    Selective Ink Contrast Enhancement with Smooth Paper Background Normalization.
    - Estimates smooth paper background illumination map using morphological closing.
    - Smooths paper background texture using bilateral filtering (removes scanner noise & grain).
    - Selectively boosts ink stroke contrast without highlighting paper noise or spots.
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    L, a, b = cv2.split(lab)
    
    # 1. Background Illumination Map
    kernel_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (bg_radius, bg_radius))
    L_bg_map = cv2.morphologyEx(L, cv2.MORPH_CLOSE, kernel_bg)
    L_bg_map = cv2.GaussianBlur(L_bg_map, (15, 15), 0)
    
    # 2. Smooth paper background texture (removes paper grain noise)
    L_smooth_bg = cv2.bilateralFilter(L, d=9, sigmaColor=35, sigmaSpace=9)
    
    # 3. Soft Ink Mask Weight
    ink_diff = np.maximum(0, L_bg_map.astype(np.float32) - L.astype(np.float32))
    ink_weight = cv2.normalize(ink_diff, None, 0, 1.0, cv2.NORM_MINMAX)
    ink_weight = np.clip(ink_weight * 2.5, 0, 1.0)
    
    # 4. CLAHE contrast boost on luminance
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8,8))
    L_clahe = clahe.apply(L)
    
    # 5. Soft-blend: Ink gets contrast boost, paper background gets smooth texture
    L_final = (ink_weight * L_clahe.astype(np.float32) + (1.0 - ink_weight) * L_smooth_bg.astype(np.float32)).astype(np.uint8)
    
    # Smooth chromaticity channels slightly to eliminate paper color noise
    a_smooth = cv2.bilateralFilter(a, d=7, sigmaColor=20, sigmaSpace=7)
    b_smooth = cv2.bilateralFilter(b, d=7, sigmaColor=20, sigmaSpace=7)
    
    lab_final = cv2.merge([L_final, a_smooth, b_smooth])
    return cv2.cvtColor(lab_final, cv2.COLOR_LAB2BGR)
