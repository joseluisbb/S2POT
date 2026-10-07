import cv2
import numpy as np
from PIL import Image

def detect_orientation(bgr):
    """Detects 90, 180, 270 degree rotation using Tesseract OSD."""
    try:
        import pytesseract
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        osd = pytesseract.image_to_osd(pil_img, output_type=pytesseract.Output.DICT)
        rotate_deg = osd.get("rotate", 0)
        conf = osd.get("orientation_conf", 0.0)
        if conf > 1.5 and rotate_deg in (90, 180, 270):
            return rotate_deg
    except Exception:
        pass
    return 0

def rotate_image(bgr, angle_deg):
    """Rotates image by 90, 180, or 270 degrees."""
    if angle_deg == 90:
        return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    elif angle_deg == 180:
        return cv2.rotate(bgr, cv2.ROTATE_180)
    elif angle_deg == 270:
        return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return bgr

def detect_skew_angle(bgr):
    """
    Detects fine skew angle (degrees) using Minimum Area Bounding Box of text line contours.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    
    h, w = gray.shape[:2]
    if w > 1600:
        scale = 1600.0 / w
        gray_small = cv2.resize(gray, (1600, int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        gray_small = gray
        
    blur = cv2.GaussianBlur(gray_small, (5, 5), 0)
    thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    
    # Morphological dilation along horizontal text lines
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 3))
    dilated = cv2.dilate(thresh, kernel, iterations=2)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    angles = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area > 800:
            rect = cv2.minAreaRect(cnt)
            angle = rect[-1]
            if angle < -45:
                angle = 90 + angle
            elif angle > 45:
                angle = angle - 90
            
            if -15.0 < angle < 15.0:
                angles.append(angle)
                
    if len(angles) > 4:
        return float(np.median(angles))
    return 0.0

def deskew_image(bgr, angle_deg):
    """Rotates image by fine angle_deg around center with edge replication."""
    if abs(angle_deg) < 0.3:
        return bgr
        
    h, w = bgr.shape[:2]
    center = (w // 2, h // 2)
    
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    
    deskewed = cv2.warpAffine(
        bgr, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE
    )
    return deskewed

def auto_align_page(bgr, enable_deskew=True):
    """
    Performs full automatic orientation and fine skew angle correction.
    Returns (aligned_bgr, rotation_deg, skew_angle).
    """
    if not enable_deskew:
        return bgr, 0, 0.0

    rot_deg = detect_orientation(bgr)
    bgr_oriented = rotate_image(bgr, rot_deg)
    
    skew_angle = detect_skew_angle(bgr_oriented)
    bgr_aligned = deskew_image(bgr_oriented, skew_angle)
    
    return bgr_aligned, rot_deg, skew_angle
