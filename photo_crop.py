# -*- coding: utf-8 -*-
"""
Photo Crop
Batch crop of analog photo scans on Raspberry Pi.
"""

import os
import sys
import traceback
import copy
import json
import shutil
import subprocess
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import messagebox, simpledialog
import cv2
import numpy as np
from PIL import Image, ImageTk, JpegImagePlugin

CONFIG_FILE = os.path.expanduser("~/.photo_crop_config.json")

APP_NAME = "Photo Crop"
APP_VERSION = "1.6.7"
APP_AUTHOR = "Flavio Scutti"
APP_YEAR = "2026"
APP_TAGLINE = "Batch crop of analog photo scans"
APP_TECH = "Python, Tkinter, OpenCV, Pillow, exiftool"
if getattr(sys, "frozen", False):
    # PyInstaller bundle: assets are next to the executable (--onedir) or in sys._MEIPASS (--onefile)
    APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(APP_DIR, ".assets", "logo.png")


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_dir": os.path.expanduser("~"), "output_dir": ""}


def save_config(config_data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Output settings (formats, compression, metadata) - stored in the config file
# ---------------------------------------------------------------------------

MAX_PENDING_SAVES = 3          # cropped images allowed to wait for the disk (caps RAM usage)
JPEG_MIN_QUALITY = 60          # lowest value of the JPEG quality slider
PNG_MIN_LEVEL, PNG_MAX_LEVEL = 1, 9

INTERPOLATIONS = {"linear": cv2.INTER_LINEAR, "cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4}

# (key, label, TIFF compression code), ordered from weakest to strongest compression
TIFF_COMPRESSIONS = [("none", "None", 1), ("packbits", "PackBits", 32773),
                     ("lzw", "LZW", 5), ("deflate", "Deflate", 8)]
TIFF_KEYS = [k for k, _l, _c in TIFF_COMPRESSIONS]
TIFF_LABELS = {k: l for k, l, _c in TIFF_COMPRESSIONS}
TIFF_CODES = {k: c for k, _l, c in TIFF_COMPRESSIONS}
TIFF_CODE_TO_KEY = {1: "none", 5: "lzw", 32773: "packbits", 8: "deflate", 32946: "deflate"}

SUBSAMPLINGS = ("444", "422", "420")  # written through PIL (see _save_jpeg_pil): works with any OpenCV version
HAS_TIFF_COMPRESSION = hasattr(cv2, "IMWRITE_TIFF_COMPRESSION")

FAMILY_BY_EXT = {".jpg": "jpeg", ".jpeg": "jpeg", ".tif": "tiff", ".tiff": "tiff",
                 ".png": "png", ".bmp": "bmp"}

# Metadata (EXIF, DPI, ICC profile, XMP/IPTC) is copied from the original scan with exiftool.
# Not copied: Orientation (pixels are already upright), thumbnails and size tags (they describe the uncropped scan).
def _find_exiftool():
    """Look for exiftool in the PATH, then next to the executable (PyInstaller bundle)."""
    exe = shutil.which("exiftool")
    if exe:
        return exe
    candidates = []
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        candidates.append(os.path.join(base, "exiftool"))
        candidates.append(os.path.join(base, "exiftool.exe"))
        if hasattr(sys, "_MEIPASS"):
            candidates.append(os.path.join(sys._MEIPASS, "exiftool"))
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None

EXIFTOOL = _find_exiftool()
EXIFTOOL_SKIP = ["Orientation", "IFD1:all", "ThumbnailImage", "PreviewImage",
                 "ExifImageWidth", "ExifImageHeight", "ImageWidth", "ImageHeight"]

# Defaults: every output keeps the characteristics of the input file.
DPI_UNITS = [("in", "px/in"), ("cm", "px/cm")]
DPI_PRESETS = [72, 96, 150, 300, 600, 1200, 2400]

# Empty strings mean "do not write this field": only filled fields are written.
MANUAL_FIELDS = ["make", "model", "datetime_original", "datetime_digitized",
                 "iso", "aperture", "shutter", "focal", "film_type", "paper_type",
                 "software",
                 "artist", "copyright",
                 "gps_lat", "gps_lon", "gps_alt"]

DEFAULT_SETTINGS = {
    "same_as_original": True,
    "outputs": {"jpeg": False, "tiff": False, "png": False},
    "interpolation": "cubic",
    "crop_mode": "manual",  # "manual" = open the crop window; "auto" = skip it
    "jpeg": {"as_original": True, "quality": 95, "subsampling": "420",
             "progressive": False, "optimize": False,
             "metadata_exif": True, "metadata_xmp": True,
             "dpi_force": False, "dpi_auto": True, "dpi_value": 300, "dpi_unit": "in"},
    "tiff": {"as_original": True, "compression": "lzw", "bits": "keep",
             "metadata_exif": True, "metadata_xmp": True,
             "dpi_force": False, "dpi_auto": True, "dpi_value": 300, "dpi_unit": "in"},
    "png": {"as_original": True, "level": 1, "bits": "keep",
            "metadata_exif": True, "metadata_xmp": True,
            "dpi_force": False, "dpi_auto": True, "dpi_value": 300, "dpi_unit": "in"},
    "manual": {k: "" for k in MANUAL_FIELDS},
}


def _as_bool(v, default):
    return v if isinstance(v, bool) else default


def _as_int(v, lo, hi, default):
    try:
        return min(max(int(v), lo), hi)
    except (TypeError, ValueError):
        return default


def _as_choice(v, choices, default):
    return v if v in choices else default


def sanitize_settings(raw):
    """Merge a (possibly partial or damaged) settings dict with the defaults."""
    d = DEFAULT_SETTINGS
    raw = raw if isinstance(raw, dict) else {}

    def sub(name):
        return raw.get(name) if isinstance(raw.get(name), dict) else {}

    outs, j, t, p = sub("outputs"), sub("jpeg"), sub("tiff"), sub("png")
    man = sub("manual")

    def dpi_block(sub_dict, defaults):
        return {
            "dpi_force": _as_bool(sub_dict.get("dpi_force"), defaults["dpi_force"]),
            "dpi_auto": _as_bool(sub_dict.get("dpi_auto"), defaults["dpi_auto"]),
            "dpi_value": _as_int(sub_dict.get("dpi_value"), 1, 100000, defaults["dpi_value"]),
            "dpi_unit": _as_choice(sub_dict.get("dpi_unit"), ("in", "cm"), defaults["dpi_unit"]),
        }

    # Old single "metadata" flag is upgraded to the two new toggles.
    def meta_pair(sub_dict, defaults):
        legacy = sub_dict.get("metadata", None)
        exif_default = defaults["metadata_exif"] if legacy is None else bool(legacy)
        xmp_default = defaults["metadata_xmp"] if legacy is None else bool(legacy)
        return {
            "metadata_exif": _as_bool(sub_dict.get("metadata_exif"), exif_default),
            "metadata_xmp": _as_bool(sub_dict.get("metadata_xmp"), xmp_default),
        }

    manual = {k: (man.get(k) if isinstance(man.get(k), str) else "") for k in MANUAL_FIELDS}

    return {
        "same_as_original": _as_bool(raw.get("same_as_original"), d["same_as_original"]),
        "outputs": {f: _as_bool(outs.get(f), d["outputs"][f]) for f in ("jpeg", "tiff", "png")},
        "interpolation": _as_choice(raw.get("interpolation"), INTERPOLATIONS, d["interpolation"]),
        "crop_mode": _as_choice(raw.get("crop_mode"), ("manual", "auto"), d["crop_mode"]),
        "jpeg": {
            "as_original": _as_bool(j.get("as_original"), d["jpeg"]["as_original"]),
            "quality": _as_int(j.get("quality"), JPEG_MIN_QUALITY, 100, d["jpeg"]["quality"]),
            "subsampling": _as_choice(j.get("subsampling"), SUBSAMPLINGS, d["jpeg"]["subsampling"]),
            "progressive": _as_bool(j.get("progressive"), d["jpeg"]["progressive"]),
            "optimize": _as_bool(j.get("optimize"), d["jpeg"]["optimize"]),
            **meta_pair(j, d["jpeg"]),
            **dpi_block(j, d["jpeg"]),
        },
        "tiff": {
            "as_original": _as_bool(t.get("as_original"), d["tiff"]["as_original"]),
            "compression": _as_choice(t.get("compression"), TIFF_KEYS, d["tiff"]["compression"]),
            "bits": _as_choice(t.get("bits"), ("keep", "8"), d["tiff"]["bits"]),
            **meta_pair(t, d["tiff"]),
            **dpi_block(t, d["tiff"]),
        },
        "png": {
            "as_original": _as_bool(p.get("as_original"), d["png"]["as_original"]),
            "level": _as_int(p.get("level"), PNG_MIN_LEVEL, PNG_MAX_LEVEL, d["png"]["level"]),
            "bits": _as_choice(p.get("bits"), ("keep", "8"), d["png"]["bits"]),
            **meta_pair(p, d["png"]),
            **dpi_block(p, d["png"]),
        },
        "manual": manual,
    }


def get_settings():
    return sanitize_settings(load_config().get("save_settings"))


def save_settings(settings):
    cfg = load_config()
    cfg["save_settings"] = copy.deepcopy(settings)
    save_config(cfg)


def summarize_settings(s):
    """Short description shown on the main window button."""
    parts = []
    if s["same_as_original"]:
        parts.append("Original")
    if s["outputs"]["jpeg"]:
        parts.append("JPEG " + ("as orig." if s["jpeg"]["as_original"] else f'{s["jpeg"]["quality"]}%'))
    if s["outputs"]["tiff"]:
        parts.append("TIFF " + ("as orig." if s["tiff"]["as_original"] else TIFF_LABELS[s["tiff"]["compression"]]))
    if s["outputs"]["png"]:
        parts.append("PNG " + ("as orig." if s["png"]["as_original"] else f'L{s["png"]["level"]}'))
    return " + ".join(parts) if parts else "nothing selected"


# ---------------------------------------------------------------------------
#  Image I/O: native bit depth, orientation, source inspection, writing
# ---------------------------------------------------------------------------

def to_8bit(img):
    """8-bit version for display, detection and 8-bit-only formats. 8-bit images are returned as they are (no copy)."""
    if img.dtype == np.uint8:
        return img
    if img.dtype == np.uint16:
        return cv2.convertScaleAbs(img, alpha=1.0 / 257.0)
    return cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def read_orientation(path):
    """EXIF/TIFF orientation tag (1 = normal). Reads only the header, never the pixels."""
    try:
        with Image.open(path) as im:
            return int(im.getexif().get(274, 1))
    except Exception:
        return 1


def apply_orientation(img, orientation):
    """Make the pixels upright. Done here (not by OpenCV) so the result does not depend on the OpenCV version."""
    if orientation == 2:
        img = img[:, ::-1]
    elif orientation == 3:
        img = img[::-1, ::-1]
    elif orientation == 4:
        img = img[::-1]
    elif orientation == 5:
        img = img.transpose(1, 0, 2)
    elif orientation == 6:
        img = np.rot90(img, -1)
    elif orientation == 7:
        img = np.rot90(img.transpose(1, 0, 2), 2)
    elif orientation == 8:
        img = np.rot90(img, 1)
    else:
        return img
    return np.ascontiguousarray(img)


def display_name(name):
    """Printable form of a file name for Tk widgets. The REAL name is always kept elsewhere and this text is
    never parsed back: undecodable bytes and control characters are replaced, so any name can be listed."""
    safe = name.encode("utf-8", "replace").decode("utf-8")
    return "".join(c if c.isprintable() else "\u25a1" for c in safe)


def _tk_text(text):
    """Text for Tk widgets (log, status): only undecodable bytes (lone surrogates) are replaced."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _plain_name(path):
    """True if the path can be handed to OpenCV as normal text. A name with undecodable bytes makes
    cv2.imread / cv2.imwrite crash the whole program (segmentation fault, not an exception),
    so such names never reach OpenCV."""
    try:
        path.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _read_image_file(path, flags):
    """cv2.imread for normal names; names with undecodable bytes are read as bytes and decoded in memory."""
    img = None
    if _plain_name(path):
        try:
            img = cv2.imread(path, flags)
        except Exception:
            img = None
    if img is None:
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(data, flags) if data.size else None
        except Exception:
            img = None
    return img


def _write_image_file(path, img, params):
    """cv2.imwrite for normal names; the others are encoded in memory and the bytes written directly."""
    if _plain_name(path):
        try:
            if cv2.imwrite(path, img, params):
                return True
        except Exception:
            pass
    ok, buf = cv2.imencode(os.path.splitext(path)[1], img, params)
    if not ok:
        return False
    buf.tofile(path)
    return True


def _exiftool_charset_args(*paths):
    """Tell exiftool the names are UTF-8, unless one contains undecodable bytes (then it must get them raw)."""
    try:
        for p in paths:
            p.encode("utf-8")
    except UnicodeEncodeError:
        return []
    return ["-charset", "filename=utf8"]


def load_scan(path):
    """Read the scan ONCE at its native bit depth (8 or 16 bit).
    Returns (full-depth image, 8-bit view) or (None, None). The view feeds detection, preview and zoom;
    the full-depth image is the one that gets cropped and saved. For 8-bit scans both are the same array."""
    img = _read_image_file(path, cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        return None, None
    img = apply_orientation(img, read_orientation(path))
    return img, to_8bit(img)


_STD_LUMA = [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
             14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
             18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
             49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99]


def _luma_table_sum(quality):
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    return sum(min(255, max(1, (v * scale + 50) // 100)) for v in _STD_LUMA)


_JPEG_Q_SUMS = {q: _luma_table_sum(q) for q in range(1, 101)}


def estimate_jpeg_quality(im):
    """Quality (1-100) of a JPEG, estimated from its luminance quantisation table."""
    table = getattr(im, "quantization", None)
    if not table or 0 not in table:
        return None
    total = sum(table[0])
    return min(_JPEG_Q_SUMS, key=lambda q: (abs(_JPEG_Q_SUMS[q] - total), -q))


def inspect_source(path):
    """What can be read from the source file: format, JPEG quality/subsampling/progressive, TIFF compression."""
    info = {"family": FAMILY_BY_EXT.get(os.path.splitext(path)[1].lower()), "jpeg_quality": None,
            "jpeg_sampling": None, "jpeg_progressive": None, "tiff_compression": None}
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            if fmt == "JPEG":
                info["family"] = "jpeg"
                info["jpeg_quality"] = estimate_jpeg_quality(im)
                info["jpeg_sampling"] = {0: "444", 1: "422", 2: "420"}.get(JpegImagePlugin.get_sampling(im))
                info["jpeg_progressive"] = bool(im.info.get("progressive") or im.info.get("progression"))
            elif fmt == "TIFF":
                info["family"] = "tiff"
                info["tiff_compression"] = TIFF_CODE_TO_KEY.get(im.tag_v2.get(259))
            elif fmt == "PNG":
                info["family"] = "png"
    except Exception:
        pass
    return info


def write_params(family, cfg, info):
    """cv2.imwrite parameters for one output. 'As original' follows the source file when it has the same
    format and the value can be read from it; otherwise the values shown in the settings window are used."""
    same = info.get("family") == family
    if family == "jpeg":
        quality, sub = cfg["quality"], cfg["subsampling"]
        progressive, optimize = cfg["progressive"], cfg["optimize"]
        if cfg["as_original"] and same:
            quality = info["jpeg_quality"] or quality
            sub = info["jpeg_sampling"] or sub
            if info["jpeg_progressive"] is not None:
                progressive = info["jpeg_progressive"]
        params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
        params += [cv2.IMWRITE_JPEG_PROGRESSIVE, int(bool(progressive)),
                   cv2.IMWRITE_JPEG_OPTIMIZE, int(bool(optimize))]
        return params
    if family == "tiff":
        if not HAS_TIFF_COMPRESSION:
            return []
        key = cfg["compression"]
        if cfg["as_original"] and same and info["tiff_compression"]:
            key = info["tiff_compression"]
        return [cv2.IMWRITE_TIFF_COMPRESSION, TIFF_CODES[key]]
    if family == "png":
        return [cv2.IMWRITE_PNG_COMPRESSION, int(cfg["level"])]
    return []


def _it_date_to_exif(text):
    """Convert 'GG/MM/AAAA HH:MM:SS' (or 'GG-MM-AAAA ...' / 'GG.MM.AAAA ...') to EXIF 'AAAA:MM:GG HH:MM:SS'.
    Returns the original string if it does not look like a date, so the user can still type raw EXIF."""
    t = (text or "").strip()
    if not t:
        return ""
    import re as _re
    m = _re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$", t)
    if not m:
        return t
    d, mo, y, hh, mm, ss = m.groups()
    if len(y) == 2:
        y = ("19" if int(y) >= 70 else "20") + y
    hh = hh or "00"
    mm = mm or "00"
    ss = ss or "00"
    return f"{int(y):04d}:{int(mo):02d}:{int(d):02d} {int(hh):02d}:{int(mm):02d}:{int(ss):02d}"


def _manual_to_exiftool_args(manual):
    """Build exiftool arguments from the manual values. Only non-empty fields are written."""
    if not manual:
        return []
    args = []

    def val(key):
        v = manual.get(key, "")
        return v.strip() if isinstance(v, str) else ""

    v = val("make")
    if v:
        args += [f"-EXIF:Make={v}"]
    v = val("model")
    if v:
        args += [f"-EXIF:Model={v}"]
    v = val("software")
    if v:
        args += [f"-EXIF:Software={v}", f"-XMP:CreatorTool={v}"]
    v = val("iso")
    if v:
        args += [f"-EXIF:ISO={v}"]
    v = val("aperture")
    if v:
        args += [f"-EXIF:FNumber={v}"]
    v = val("shutter")
    if v:
        args += [f"-EXIF:ExposureTime={v}"]
    v = val("focal")
    if v:
        args += [f"-EXIF:FocalLength={v}"]

    film = val("film_type")
    paper = val("paper_type")
    if film or paper:
        parts = []
        if film:
            parts.append(f"Film: {film}")
        if paper:
            parts.append(f"Paper: {paper}")
        text = " | ".join(parts)
        args += [f"-EXIF:UserComment={text}", f"-XMP:Description={text}"]

    v = val("datetime_original")
    if v:
        exif_dt = _it_date_to_exif(v)
        args += [f"-EXIF:DateTimeOriginal={exif_dt}",
                 f"-EXIF:CreateDate={exif_dt}",
                 f"-XMP:DateTimeOriginal={exif_dt}"]

    v = val("datetime_digitized")
    if v:
        exif_dt = _it_date_to_exif(v)
        args += [f"-EXIF:ModifyDate={exif_dt}",
                 f"-XMP:CreateDate={exif_dt}"]

    v = val("artist")
    if v:
        args += [f"-EXIF:Artist={v}", f"-XMP:Creator={v}"]

    v = val("copyright")
    if v:
        args += [f"-EXIF:Copyright={v}", f"-XMP:Rights={v}"]

    lat = val("gps_lat")
    lon = val("gps_lon")
    alt = val("gps_alt")
    if lat:
        try:
            lat_f = float(lat)
            ref = "N" if lat_f >= 0 else "S"
            args += [f"-EXIF:GPSLatitude={abs(lat_f)}", f"-EXIF:GPSLatitudeRef={ref}"]
        except ValueError:
            pass
    if lon:
        try:
            lon_f = float(lon)
            ref = "E" if lon_f >= 0 else "W"
            args += [f"-EXIF:GPSLongitude={abs(lon_f)}", f"-EXIF:GPSLongitudeRef={ref}"]
        except ValueError:
            pass
    if alt:
        try:
            alt_f = float(alt)
            args += [f"-EXIF:GPSAltitude={abs(alt_f)}",
                     f"-EXIF:GPSAltitudeRef={'0' if alt_f >= 0 else '1'}"]
        except ValueError:
            pass
    return args


def copy_metadata(src_path, dst_path, dpi_override=None,
                  copy_exif=True, copy_xmp=True, manual=None):
    """Copy metadata from the original scan to the cropped file, then write the manual values on top.
    The ICC profile is ALWAYS copied: it defines the colours, dropping it would change how the image looks.
    copy_exif / copy_xmp only control EXIF+GPS and XMP+IPTC.
    dpi_override: dict with keys "value" and "unit" ("in" or "cm"), or None to keep the source DPI.
    It is applied whatever the two toggles say.
    manual: dict of user-typed values; only filled fields are written (they override the pass-through).
    Returns None on success, or a short warning text (the image itself is saved anyway)."""
    manual_args = _manual_to_exiftool_args(manual)
    explicit = copy_exif or copy_xmp or bool(manual_args) or dpi_override is not None
    if not EXIFTOOL:
        # Complain only when the user asked for something; the ICC profile alone is a silent extra.
        return "exiftool not found: DPI, ICC profile and EXIF were not copied" if explicit else None

    cmd = ([EXIFTOOL, "-q", "-m", "-overwrite_original"] + _exiftool_charset_args(src_path, dst_path)
           + ["-TagsFromFile", src_path, "-icc_profile"])
    if copy_exif:
        cmd += ["-EXIF:All", "-GPS:All"]
    if copy_xmp:
        cmd += ["-XMP:All", "-IPTC:All"]
    for tag in EXIFTOOL_SKIP:
        cmd += ["-x", tag]
    if dpi_override is not None:
        unit_code = 2 if dpi_override["unit"] == "in" else 3  # 2=inches, 3=cm
        val = int(dpi_override["value"])
        cmd += [f"-XResolution={val}", f"-YResolution={val}", f"-ResolutionUnit={unit_code}"]
    cmd.append(dst_path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "metadata copy timed out"
    except OSError as e:
        return f"could not run exiftool: {e}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return "metadata copy failed" + (f": {detail[0]}" if detail else "")

    # Second pass: write the manual values (override whatever was copied)
    if manual_args:
        cmd2 = ([EXIFTOOL, "-q", "-m", "-overwrite_original"] + _exiftool_charset_args(dst_path)
                + manual_args + [dst_path])
        try:
            r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "manual metadata write timed out"
        except OSError as e:
            return f"could not run exiftool (manual): {e}"
        if r2.returncode != 0:
            detail = (r2.stderr or r2.stdout).strip().splitlines()
            return "manual metadata write failed" + (f": {detail[0]}" if detail else "")

    return None


def save_image(path, img, src_path, params, copy_exif, copy_xmp, manual=None,
               jpeg_cfg=None, dpi_override=None):
    """Write the image, then copy metadata from src_path and/or write manual values.
    Returns warning text or None.
    copy_exif / copy_xmp: independent pass-through toggles.
    manual: dict of user-typed values (only filled fields are written).
    jpeg_cfg: if provided and path is a JPEG, PIL is used (supports chroma subsampling).
    dpi_override: dict with "value" and "unit" or None to keep the source DPI."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        if jpeg_cfg is not None:
            return _save_jpeg_pil(path, img, jpeg_cfg["quality"], jpeg_cfg["subsampling"],
                                  jpeg_cfg["progressive"], jpeg_cfg["optimize"],
                                  copy_exif, copy_xmp, manual, src_path, dpi_override)
        img = to_8bit(img)
    if ext == ".bmp":
        img = to_8bit(img)
    tmp_path = os.path.join(os.path.dirname(path) or ".",
                            f".photocrop_{os.getpid()}_{threading.get_ident()}{ext}")
    try:
        if not _write_image_file(tmp_path, img, params):
            raise IOError(f"could not write {os.path.basename(path)}")
        warning = None
        if src_path and ext != ".bmp":
            warning = copy_metadata(src_path, tmp_path, dpi_override=dpi_override,
                                    copy_exif=copy_exif, copy_xmp=copy_xmp, manual=manual)
        os.replace(tmp_path, path)
        return warning
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _save_jpeg_pil(path, img_bgr, quality, subsampling_key, progressive, optimize,
                   copy_exif, copy_xmp, manual, src_path, dpi_override=None):
    """Save a JPEG using PIL (supports explicit chroma subsampling)."""
    from PIL import Image
    if img_bgr.dtype != np.uint8:
        img_bgr = to_8bit(img_bgr)
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)

    subsampling_map = {"444": 0, "422": 1, "420": 2}
    sub_val = subsampling_map.get(subsampling_key, 2)

    tmp_path = os.path.join(os.path.dirname(path) or ".",
                            f".photocrop_{os.getpid()}_{threading.get_ident()}.jpg")
    try:
        pil_img.save(tmp_path, format="JPEG", quality=int(quality),
                     subsampling=sub_val, progressive=bool(progressive),
                     optimize=bool(optimize))
        warning = None
        if src_path:
            warning = copy_metadata(src_path, tmp_path, dpi_override=dpi_override,
                                    copy_exif=copy_exif, copy_xmp=copy_xmp, manual=manual)
        os.replace(tmp_path, path)
        return warning
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def make_output_path(out_dir, base_name, suffix, ext, src_path, reserved):
    """Same naming rules as before (_1, _2 ... for existing files); 'reserved' also covers files still being written."""
    out_path = os.path.join(out_dir, f"{base_name}{suffix}{ext}")
    c = 1
    while (os.path.exists(out_path) or out_path in reserved) and out_path != src_path:
        out_path = os.path.join(out_dir, f"{base_name}{suffix}_{c}{ext}")
        c += 1
    reserved.add(out_path)
    return out_path


def render_native(full, matrix, angle, box, interp):
    """The crop box of the rotated image, computed from the full-depth original. Same matrix and interpolation
    as the preview, but only the pixels of the crop are calculated. At exactly 0 degrees nothing is resampled."""
    x0, y0, x1, y1 = box
    if abs(angle) < 1e-9:
        return np.ascontiguousarray(full[y0:y1, x0:x1])
    m = np.array(matrix, dtype=np.float64)
    m[0, 2] -= x0
    m[1, 2] -= y0
    return cv2.warpAffine(full, m, (x1 - x0, y1 - y0), flags=interp, borderMode=cv2.BORDER_REPLICATE)


def save_outputs(job, log):
    """Runs in the writer thread: renders the crop and writes every requested format.
    Returns True if at least one file was saved."""
    settings, plans, src_path = job["settings"], job["plans"], job["src_path"]
    saved = 0
    try:
        full, rotated = job["full"], job["rotated"]
        x0, y0, x1, y1 = job["box"]
        info = inspect_source(src_path)

        def keeps_depth(plan):
            fam = plan["family"]
            return fam in ("tiff", "png") and settings[fam]["bits"] == "keep"

        if full is None:  # 8-bit scan: the very pixels shown in the preview
            native = crop8 = np.ascontiguousarray(rotated[y0:y1, x0:x1])
        else:
            crop8 = None
            native = render_native(full, job["matrix"], job["angle"], job["box"], job["interp"]) \
                if any(keeps_depth(p) for p in plans) else None

        def array_for(plan):
            nonlocal crop8
            if keeps_depth(plan):
                return native
            if crop8 is None:
                crop8 = to_8bit(native) if native is not None else np.ascontiguousarray(rotated[y0:y1, x0:x1])
            return crop8

        for plan in plans:
            fam = plan["family"]
            name = os.path.basename(plan["path"])
            cfg = settings.get(fam, {})
            try:
                jpeg_cfg = None
                if fam == "jpeg":
                    same = info.get("family") == "jpeg"
                    q = cfg["quality"]
                    sub = cfg["subsampling"]
                    prog = bool(cfg["progressive"])
                    opt = bool(cfg["optimize"])
                    if cfg.get("as_original") and same:
                        q = info.get("jpeg_quality") or q
                        sub = info.get("jpeg_sampling") or sub
                        if info.get("jpeg_progressive") is not None:
                            prog = bool(info["jpeg_progressive"])
                    jpeg_cfg = {"quality": q, "subsampling": sub,
                                "progressive": prog, "optimize": opt}
                # DPI override: written only when "Force DPI" is ON and the DPI is not "Auto".
                # Auto = keep the original scan's DPI, whatever "As original" says.
                dpi_override = None
                if cfg.get("dpi_force") and not cfg.get("dpi_auto"):
                    dpi_override = {"value": int(cfg.get("dpi_value", 300)),
                                    "unit": cfg.get("dpi_unit", "in")}
                warning = save_image(plan["path"], array_for(plan), src_path,
                                     write_params(fam, cfg, info),
                                     bool(cfg.get("metadata_exif", True)),
                                     bool(cfg.get("metadata_xmp", True)),
                                     manual=settings.get("manual") or None,
                                     jpeg_cfg=jpeg_cfg,
                                     dpi_override=dpi_override)
                log(f"  [OK] Successfully cropped: {name}")
                if warning:
                    log(f"  [WARNING] {name}: {warning}")
                saved += 1
            except Exception as e:
                log(f"  [EXCEPTION] {name}: {e}")
    except Exception as e:
        log(f"  [EXCEPTION] save: {e}")
    finally:
        job["release"]()
    return saved > 0


def _split_detection(result):
    """_detect_card_quad returns (quad, used_fallback); older versions returned just the quad."""
    if isinstance(result, tuple):
        return result[0], bool(result[1])
    return result, False


class PrefetchWorker(threading.Thread):
    """
    Background worker that preloads and pre-processes images.

    It reads each requested path with load_scan, runs the detection on the 8-bit view and pushes the result
    into an output queue as a tuple:
        (path, scan_or_None, quad_or_None, error_or_None, used_fallback)
    where scan is the pair (native-depth image, 8-bit view).

    The main thread collects results with poll_result(path), which never blocks.
    """

    def __init__(self, detect_func):
        super().__init__(daemon=True)
        self.detect_func = detect_func
        self._requests = queue.Queue()
        self._results = queue.Queue()
        self._stop_event = threading.Event()
        self._current_path = None
        self._pending = set()   # requested and not yet collected (main thread only)
        self._stash = {}        # results that arrived for a path nobody asked for yet

    def request(self, path):
        # Ask for a path unless it is already being processed, queued or waiting to be collected.
        if path == self._current_path or path in self._pending:
            return
        self._pending.add(path)
        self._requests.put(path)

    def stop(self):
        self._stop_event.set()
        # Unblock the request queue with a sentinel
        self._requests.put(None)

    def run(self):
        while not self._stop_event.is_set():
            try:
                path = self._requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if path is None:
                break
            self._current_path = path
            try:
                full, view = load_scan(path)
                if full is None:
                    self._results.put((path, None, None, "Cannot read image", False))
                    continue
                quad, used_fallback = _split_detection(self.detect_func(view))
                self._results.put((path, (full, view), quad, None, used_fallback))
            except Exception as e:
                self._results.put((path, None, None, traceback.format_exc(), False))
            finally:
                self._current_path = None

    def poll_result(self, path):
        """Non-blocking. Returns (scan, quad, error, used_fallback) if the result for `path` is ready, else None."""
        if path in self._stash:
            self._pending.discard(path)
            return self._stash.pop(path)
        while True:
            try:
                rpath, scan, quad, err, used_fallback = self._results.get_nowait()
            except queue.Empty:
                return None
            payload = (scan, quad, err, used_fallback)
            if rpath == path:
                self._pending.discard(path)
                return payload
            self._stash[rpath] = payload   # asked for earlier than needed: keep it, do not throw it away

    def get_result(self, path, timeout=30.0):
        """Blocking version (kept for compatibility): waits up to `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            result = self.poll_result(path)
            if result is not None:
                return result
            time.sleep(0.02)
        return None


class SegmentedButton(tk.Canvas):
    def __init__(self, parent, text, command, font=("Inter", 18, "bold"),
                 width=120, height=46,
                 bg_off="#21262d", fg_off="#ffffff",
                 bg_on="#ffffff", fg_on="#0d1116"):
        super().__init__(parent, bg="#161b22", highlightthickness=0, bd=0)
        self.command = command
        self.text = text
        self.font = font
        self.bg_off = bg_off
        self.fg_off = fg_off
        self.bg_on = bg_on
        self.fg_on = fg_on
        self.is_selected = False
        self.enabled = True   # controls whether the command is executed on click
        self.width = width
        self.height = height
        self.configure(width=self.width, height=self.height)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_text(self, text):
        self.text = text
        self.draw(self.is_selected)

    def _on_configure(self, event):
        if event.width > 1:
            self.width = event.width
            self.draw(self.is_selected)

    def draw(self, selected):
        self.is_selected = selected
        self.delete("all")
        bg_color = self.bg_on if selected else self.bg_off
        fg_color = self.fg_on if selected else self.fg_off
        border_color = "#30363d" if not selected else "#ffffff"
        self.create_rounded_rect(2, 2, self.width - 2, self.height - 2, radius=4,
                                 fill=bg_color, outline=border_color, width=1)
        self.create_text(self.width / 2, self.height / 2, text=self.text,
                         font=self.font, fill=fg_color, anchor="center")

    def create_rounded_rect(self, x1, y1, x2, y2, radius=4, **kwargs):
        points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
                  x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
                  x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
        return self.create_polygon(points, **kwargs, smooth=True)

    def _on_press(self, event):
        if not self.enabled:
            return
        self._pressed_style()

    def _on_release(self, event):
        # restore normal look, then run command
        self.draw(self.is_selected)
        if self.enabled and self.command:
            self.command()

    def _pressed_style(self):
        # slight visual feedback: thicker white border
        self.delete("all")
        bg_color = self.bg_on if self.is_selected else self.bg_off
        fg_color = self.fg_on if self.is_selected else self.fg_off
        self.create_rounded_rect(2, 2, self.width - 2, self.height - 2, radius=4,
                                 fill=bg_color, outline="#ffffff", width=2)
        self.create_text(self.width / 2, self.height / 2, text=self.text,
                         font=self.font, fill=fg_color, anchor="center")


class AutoHideScrollbar(tk.Scrollbar):
    def _cancel_timer(self):
        if self._after_id:
            self.after_cancel(self._after_id)
            self._after_id = None

    def _schedule_hide(self):
        self._cancel_timer()
        if not self._hover:
            self._after_id = self.after(self.hide_delay, self._hide)

    def _hide(self):
        self._cancel_timer()
        if self._visible:
            self.place_forget()
            self._visible = False

    def _show(self):
        if not self._visible:
            self.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
            self.tkraise()
            self._visible = True
        self._schedule_hide()

    def __init__(self, parent, target, hide_delay=1000, **kwargs):
        super().__init__(parent, orient="vertical", **kwargs)
        self.target = target
        self.hide_delay = hide_delay
        self._after_id = None
        self._visible = False
        self._hover = False
        self._needed = False
        self.configure(command=target.yview)
        target.configure(yscrollcommand=self.set)
        for seq in ("<Motion>", "<MouseWheel>", "<Button-4>", "<Button-5>", "<KeyPress>"):
            target.bind(seq, self._on_activity, add="+")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set(self, lo, hi):
        super().set(lo, hi)
        self._needed = not (float(lo) <= 0.0 and float(hi) >= 1.0)
        if self._needed:
            self._show()
        else:
            self._hide()

    def _on_activity(self, event=None):
        if self._needed:
            self._show()

    def _on_enter(self, event=None):
        self._hover = True
        self._cancel_timer()

    def _on_leave(self, event=None):
        self._hover = False
        self._schedule_hide()


class GreyButton(tk.Button):
    def __init__(self, parent, **kwargs):
        kwargs.setdefault("bg", "#21262d")
        kwargs.setdefault("fg", "#ffffff")
        normal_bg = kwargs["bg"]
        normal_fg = kwargs["fg"]
        kwargs["activebackground"] = normal_bg
        kwargs["activeforeground"] = normal_fg
        kwargs.setdefault("takefocus", 0)
        kwargs.setdefault("disabledforeground", "#8b949e")
        super().__init__(parent, **kwargs)
        self._normal_bg = normal_bg
        self._normal_fg = normal_fg
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event=None):
        if str(self.cget("state")) == "disabled":
            return
        self.configure(bg="#ffffff", fg="#0d1116",
                       activebackground="#ffffff", activeforeground="#0d1116")

    def _on_release(self, event=None):
        self.configure(bg=self._normal_bg, fg=self._normal_fg,
                       activebackground=self._normal_bg, activeforeground=self._normal_fg)


class LightButton(tk.Button):
    ON_BG, ON_FG = "#ffffff", "#0d1116"
    OFF_BG, OFF_FG = "#21262d", "#8b949e"
    PRESS_BG, PRESS_FG = "#21262d", "#ffffff"

    def __init__(self, parent, **kwargs):
        state = kwargs.pop("state", tk.NORMAL)
        kwargs.setdefault("takefocus", 0)
        super().__init__(parent, **kwargs)
        self._enabled = True
        self._apply_state(state)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _paint(self, bg, fg):
        tk.Button.configure(self, bg=bg, fg=fg, activebackground=bg,
                            activeforeground=fg, disabledforeground=fg)

    def _apply_state(self, state):
        self._enabled = str(state) != tk.DISABLED
        tk.Button.configure(self, state=tk.NORMAL if self._enabled else tk.DISABLED)
        if self._enabled:
            self._paint(self.ON_BG, self.ON_FG)
        else:
            self._paint(self.OFF_BG, self.OFF_FG)

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._apply_state(kw.pop("state"))
            if cnf is None and not kw:
                return None
        return super().configure(cnf, **kw)

    config = configure

    def _on_press(self, event=None):
        if self._enabled:
            self._paint(self.PRESS_BG, self.PRESS_FG)

    def _on_release(self, event=None):
        if self._enabled:
            self._paint(self.ON_BG, self.ON_FG)


class FileSelectorDialog(tk.Toplevel):
    def __init__(self, parent, initial_dir):
        super().__init__(parent)
        self.title("Select Image Scans")
        self.geometry("1024x768")
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#0d1116")
        self.selected_files = []
        self.current_dir = initial_dir if (initial_dir and os.path.exists(initial_dir)) else os.path.expanduser("~")
        self.is_closing = False
        self.entries = []   # [(is_dir, REAL name)] in the same order as the rows of the list
        main_frame = tk.Frame(self, bg="#0d1116", padx=20, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)
        self.lbl_path = tk.Label(main_frame, text="", font=("Inter", 22, "bold"),
                                 bg="#0d1116", fg="#ffffff", anchor="w")
        self.lbl_path.pack(fill=tk.X, pady=8)
        btn_up = tk.Button(main_frame, text="Parent Folder (Left Arrow)", command=self.go_parent_dir,
                           bg="#21262d", fg="#ffffff", font=("Inter", 22, "bold"), bd=1,
                           relief="solid", highlightthickness=0, padx=15, pady=15)
        btn_up.pack(anchor=tk.W, pady=8)
        list_frame = tk.Frame(main_frame, bg="#161b22", bd=1, relief="solid")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=8)
        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, font=("Inter", 26),
                                  bg="#161b22", fg="#ffffff", selectbackground="#30363d",
                                  selectforeground="#ffffff", bd=0, highlightthickness=0)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview, width=28,
                                 bg="#161b22", troughcolor="#161b22", activebackground="#30363d",
                                 bd=0, highlightthickness=0)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-1>", self.on_item_action)
        self.listbox.bind("<Return>", self.on_return_key)
        self.listbox.bind("<Right>", self.on_right_arrow)
        self.listbox.bind("<Left>", lambda e: self.go_parent_dir())
        self.listbox.bind("<Escape>", lambda e: self.safe_destroy())
        self.bind("<Control-w>", lambda e: self.safe_destroy())
        self.bind("<Control-q>", lambda e: self.safe_destroy())
        btn_frame = tk.Frame(main_frame, bg="#0d1116")
        btn_frame.pack(fill=tk.X, pady=12)
        btn_confirm = tk.Button(btn_frame, text="Confirm Selection", command=self.confirm_selection,
                                bg="#ffffff", fg="#0d1116", font=("Inter", 22, "bold"), bd=1,
                                relief="solid", highlightthickness=0, padx=20, pady=15)
        btn_confirm.pack(side=tk.LEFT, padx=5)
        btn_cancel = tk.Button(btn_frame, text="Cancel (Esc)", command=self.safe_destroy,
                               bg="#21262d", fg="#ffffff", font=("Inter", 22, "bold"), bd=1,
                               relief="solid", highlightthickness=0, padx=20, pady=15)
        btn_cancel.pack(side=tk.RIGHT, padx=5)
        self.load_directory(self.current_dir)
        self.listbox.focus_set()

    def load_directory(self, path):
        try:
            if os.path.exists(path):
                supported_exts = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
                dirs, files = [], []
                with os.scandir(path) as it:  # the file type comes with the name: no extra stat() per item
                    for entry in it:
                        try:
                            if entry.is_dir():
                                dirs.append(entry.name)
                            elif entry.name.lower().endswith(supported_exts):
                                files.append(entry.name)
                        except OSError:
                            continue
                dirs.sort(key=str.lower)
                files.sort(key=str.lower)
                self.current_dir = path
                self.lbl_path.config(text=f"Path: {display_name(path)}")
                # The REAL names are kept in self.entries; the text of a row is only for display and is never
                # parsed back (a file name can contain anything, including "]  " or symbols).
                self.entries = [(True, n) for n in dirs] + [(False, n) for n in files]
                self.listbox.delete(0, tk.END)
                for is_dir, n in self.entries:
                    prefix = "[DIR]  " if is_dir else "[IMG]  "
                    self.listbox.insert(tk.END, prefix + display_name(n))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def go_parent_dir(self):
        if self.is_closing:
            return
        parent = os.path.dirname(self.current_dir)
        if parent and parent != self.current_dir:
            self.load_directory(parent)

    def get_current_entry(self):
        """(is_dir, real name) of the selected row, or of the active row when nothing is selected."""
        sel = self.listbox.curselection()
        try:
            idx = sel[0] if sel else self.listbox.index(tk.ACTIVE)
        except Exception:
            return None
        if isinstance(idx, int) and 0 <= idx < len(self.entries):
            return self.entries[idx]
        return None

    def on_item_action(self, event):
        if self.is_closing:
            return "break"
        if len(self.listbox.curselection()) > 1:
            self.confirm_selection()
            return "break"
        entry = self.get_current_entry()
        if not entry:
            return "break"
        is_dir, name = entry
        if is_dir:
            self.load_directory(os.path.join(self.current_dir, name))
        else:
            self.confirm_selection()
        return "break"

    def on_return_key(self, event):
        if self.is_closing:
            return "break"
        return self.on_item_action(event)

    def on_right_arrow(self, event):
        if self.is_closing:
            return "break"
        entry = self.get_current_entry()
        if entry and entry[0]:
            self.load_directory(os.path.join(self.current_dir, entry[1]))

    def confirm_selection(self):
        if self.is_closing:
            return
        chosen = [os.path.join(self.current_dir, self.entries[i][1])
                  for i in self.listbox.curselection()
                  if 0 <= i < len(self.entries) and not self.entries[i][0]]
        if not chosen:
            entry = self.get_current_entry()
            if entry and not entry[0]:
                chosen.append(os.path.join(self.current_dir, entry[1]))
        if chosen:
            self.is_closing = True
            self.selected_files = chosen
            self.after(50, self._finalize_destroy)
        else:
            messagebox.showwarning("Warning", "Please select at least one image file.")

    def safe_destroy(self):
        if self.is_closing:
            return
        self.is_closing = True
        self.selected_files = []
        self.after(50, self._finalize_destroy)

    def _finalize_destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class FolderSelectorDialog(tk.Toplevel):
    def __init__(self, parent, initial_dir):
        super().__init__(parent)
        self.title("Select Output Folder")
        self.geometry("1024x768")
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#0d1116")
        self.result = None
        self.current_dir = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else os.path.expanduser("~")
        self.is_closing = False
        self.names = []   # REAL folder names, in the same order as the rows of the list
        main_frame = tk.Frame(self, bg="#0d1116", padx=20, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)
        self.lbl_path = tk.Label(main_frame, text="", font=("Inter", 22, "bold"),
                                 bg="#0d1116", fg="#ffffff", anchor="w")
        self.lbl_path.pack(fill=tk.X, pady=8)
        nav_frame = tk.Frame(main_frame, bg="#0d1116")
        nav_frame.pack(fill=tk.X, pady=8)
        btn_up = tk.Button(nav_frame, text="Parent Folder (Left Arrow)", command=self.go_parent_dir,
                           bg="#21262d", fg="#ffffff", font=("Inter", 22, "bold"), bd=1,
                           relief="solid", highlightthickness=0, padx=15, pady=15)
        btn_up.pack(side=tk.LEFT, anchor=tk.W)
        btn_new_folder = tk.Button(nav_frame, text="New Folder", command=self.create_new_folder,
                                   bg="#21262d", fg="#ffffff", font=("Inter", 22, "bold"), bd=1,
                                   relief="solid", highlightthickness=0, padx=15, pady=15)
        btn_new_folder.pack(side=tk.RIGHT, anchor=tk.E)
        list_frame = tk.Frame(main_frame, bg="#161b22", bd=1, relief="solid")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=8)
        self.listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE, font=("Inter", 26),
                                  bg="#161b22", fg="#ffffff", selectbackground="#30363d",
                                  selectforeground="#ffffff", bd=0, highlightthickness=0)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview, width=28,
                                 bg="#161b22", troughcolor="#161b22", activebackground="#30363d",
                                 bd=0, highlightthickness=0)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-1>", self.on_open_folder)
        self.listbox.bind("<Return>", self.on_open_folder)
        self.listbox.bind("<Right>", self.on_open_folder)
        self.listbox.bind("<Left>", lambda e: self.go_parent_dir())
        self.listbox.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Control-w>", lambda e: self.cancel())
        self.bind("<Control-q>", lambda e: self.cancel())
        btn_frame = tk.Frame(main_frame, bg="#0d1116")
        btn_frame.pack(fill=tk.X, pady=12)
        btn_use = tk.Button(btn_frame, text="Use This Folder", command=self.use_this_folder,
                            bg="#ffffff", fg="#0d1116", font=("Inter", 22, "bold"), bd=1,
                            relief="solid", highlightthickness=0, padx=20, pady=15)
        btn_use.pack(side=tk.LEFT, padx=5)
        btn_default = tk.Button(btn_frame, text="Same as Source", command=self.use_default,
                                bg="#21262d", fg="#ffffff", font=("Inter", 22, "bold"), bd=1,
                                relief="solid", highlightthickness=0, padx=20, pady=15)
        btn_default.pack(side=tk.LEFT, padx=5)
        btn_cancel = tk.Button(btn_frame, text="Cancel (Esc)", command=self.cancel,
                               bg="#21262d", fg="#ffffff", font=("Inter", 22, "bold"), bd=1,
                               relief="solid", highlightthickness=0, padx=20, pady=15)
        btn_cancel.pack(side=tk.RIGHT, padx=5)
        self.load_directory(self.current_dir)
        self.listbox.focus_set()

    def load_directory(self, path):
        try:
            if os.path.isdir(path):
                names = []
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            if not entry.name.startswith(".") and entry.is_dir():
                                names.append(entry.name)
                        except OSError:
                            continue
                names.sort(key=str.lower)
                self.current_dir = path
                self.lbl_path.config(text=f"Path: {display_name(path)}")
                self.names = names
                self.listbox.delete(0, tk.END)
                for n in names:
                    self.listbox.insert(tk.END, f"[DIR]  {display_name(n)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def go_parent_dir(self):
        if self.is_closing:
            return
        parent = os.path.dirname(self.current_dir)
        if parent and parent != self.current_dir:
            self.load_directory(parent)

    def create_new_folder(self):
        if self.is_closing:
            return
        folder_name = simpledialog.askstring("New Folder", "Enter name for the new folder:", parent=self)
        if folder_name:
            folder_name = folder_name.strip()
            new_path = os.path.join(self.current_dir, folder_name)
            try:
                os.makedirs(new_path, exist_ok=True)
                self.load_directory(self.current_dir)
                for i, name in enumerate(self.names):
                    if name == folder_name:
                        self.listbox.selection_set(i)
                        self.listbox.see(i)
                        break
            except Exception as e:
                messagebox.showerror("Error", f"Could not create folder:\n{e}")

    def get_current_name(self):
        sel = self.listbox.curselection()
        try:
            idx = sel[0] if sel else self.listbox.index(tk.ACTIVE)
        except Exception:
            return None
        if isinstance(idx, int) and 0 <= idx < len(self.names):
            return self.names[idx]
        return None

    def on_open_folder(self, event):
        if self.is_closing:
            return "break"
        name = self.get_current_name()
        if name:
            self.load_directory(os.path.join(self.current_dir, name))
        return "break"

    def use_this_folder(self):
        if self.is_closing:
            return
        if not os.access(self.current_dir, os.W_OK):
            messagebox.showwarning("Warning", "This folder is not writable. Choose another one.")
            return
        self.result = self.current_dir
        self._close()

    def use_default(self):
        if self.is_closing:
            return
        self.result = ""
        self._close()

    def cancel(self):
        if self.is_closing:
            return
        self.result = None
        self._close()

    def _close(self):
        self.is_closing = True
        self.after(50, self._finalize_destroy)

    def _finalize_destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()



# ---------------------------------------------------------------------------
#  Geometry helpers
# ---------------------------------------------------------------------------

def _order_corners(pts):
    pts = np.asarray(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def _is_quad_plausible(quad, img_w, img_h, used_fallback, margin_px=3):
    """Decide whether an automatically detected quad is trustworthy enough for unattended crop.
    Returns (ok: bool, reason: str). The reason is empty when ok is True.

    Criteria (any of these makes the quad NOT plausible):
      - the detected area is less than 5% of the image area (likely a false positive);
      - the quad touches all four image borders (less than `margin_px` from each side),
        which means there is no real margin to crop;
      - the rotation angle is more than 45 degrees;
      - the detection used the fallback method (the primary method failed).
    """
    if used_fallback:
        return False, "detection used the fallback method"
    q = np.asarray(quad, dtype=np.float64)
    if q.shape != (4, 2):
        return False, "invalid quad shape"

    img_area = float(img_w) * float(img_h)
    quad_area = _polygon_area(q)
    if img_area <= 0:
        return False, "empty image"
    ratio = quad_area / img_area
    if ratio < 0.05:
        return False, f"detected area is only {ratio * 100:.1f}% of the image"

    x_min = float(q[:, 0].min())
    y_min = float(q[:, 1].min())
    x_max = float(q[:, 0].max())
    y_max = float(q[:, 1].max())
    touches_left = x_min <= margin_px
    touches_top = y_min <= margin_px
    touches_right = x_max >= (img_w - 1 - margin_px)
    touches_bottom = y_max >= (img_h - 1 - margin_px)
    if touches_left and touches_top and touches_right and touches_bottom:
        return False, "quad touches all four image borders"

    try:
        angle = _identify_photo_angle(q)
    except Exception:
        angle = 0.0
    if abs(angle) > 45.0:
        return False, f"rotation angle is {angle:.1f} degrees"

    return True, ""


def _detect_card_quad(img_bgr):
    """
    Detect the 4 corners of the printed image on the scan, using:
      1. Sobel gradients to highlight the photographic paper's border
         (a strong luminance transition from paper to scanner background).
      2. HoughLinesP to find the main straight lines of the border.
      3. cv2.fitLine on the edge pixels near each Hough line, for
         sub-pixel accuracy.
      4. Intersection of the 4 refined lines to get the 4 corners.

    Returns an (4,2) float32 array ordered TL, TR, BR, BL.

    Falls back to the previous threshold-based method if Hough fails.
    """
    h, w = img_bgr.shape[:2]

    # ----- 1) Find an approximate bounding box of the photo -----
    bbox_scale = min(1.0, 800.0 / max(h, w))
    if bbox_scale < 1.0:
        small = cv2.resize(img_bgr, (int(w * bbox_scale), int(h * bbox_scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img_bgr
    sh, sw = small.shape[:2]
    hsv_s = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    gray_s = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray_s = cv2.GaussianBlur(gray_s, (5, 5), 0)

    # Adaptive luminance threshold from the image border.
    border = 40
    border_pixels = np.concatenate([
        gray_s[:border, :].ravel(),
        gray_s[-border:, :].ravel(),
        gray_s[:, :border].ravel(),
        gray_s[:, -border:].ravel(),
    ])
    bg_median = float(np.median(border_pixels))
    lum_threshold = bg_median - 3.0
    sat_threshold = 30
    bg_mask = ((gray_s > lum_threshold) & (hsv_s[:, :, 1] < sat_threshold)).astype(np.uint8) * 255
    non_bg = 255 - bg_mask
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    non_bg = cv2.morphologyEx(non_bg, cv2.MORPH_CLOSE, kernel, iterations=3)
    non_bg = cv2.morphologyEx(non_bg, cv2.MORPH_OPEN, kernel, iterations=1)
    cnts, _ = cv2.findContours(non_bg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return _detect_card_quad_fallback(img_bgr), True
    img_area_s = sh * sw
    cands = [c for c in cnts if cv2.contourArea(c) > 0.05 * img_area_s]
    if not cands:
        cands = cnts
    big = max(cands, key=cv2.contourArea)
    x0_s, y0_s, w_s, h_s = cv2.boundingRect(big)
    # ----- 2) Thin band (30 px on each side) around the contour -----
    contour_full = (big.astype(np.float32) / bbox_scale).astype(np.int32)
    band = np.zeros((h, w), dtype=np.uint8)
    band_px = 100
    cv2.drawContours(band, [contour_full], -1, 255, thickness=band_px * 2)

    # ----- 3) Gradients, limited to the band -----
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    thr = np.percentile(mag, 96)
    mask = (mag > thr).astype(np.uint8) * 255
    mask = cv2.bitwise_and(mask, band)

    # ----- 4) Hough lines -----
    min_len = int(0.20 * min(h, w))
    lines = cv2.HoughLinesP(mask, 1, np.pi / 720, threshold=80,
                            minLineLength=min_len, maxLineGap=30)
    if lines is None or len(lines) < 4:
        return _detect_card_quad_fallback(img_bgr), True

    # ----- 3) Refine each line with sub-pixel fitLine -----
    ys, xs = np.where(mask > 0)
    edge_pts = np.column_stack([xs, ys]).astype(np.float32)
    if len(edge_pts) < 100:
        return _detect_card_quad_fallback(img_bgr), True

    def refine(x1, y1, x2, y2, tol=3.0):
        p1 = np.array([x1, y1], dtype=np.float32)
        p2 = np.array([x2, y2], dtype=np.float32)
        d = p2 - p1
        L = float(np.linalg.norm(d))
        if L < 1.0:
            return None
        u = d / L
        n = np.array([-u[1], u[0]], dtype=np.float32)
        rel = edge_pts - p1
        perp = rel @ n
        along = rel @ u
        keep = (np.abs(perp) < tol) & (along > -10) & (along < L + 10)
        pts = edge_pts[keep]
        if len(pts) < 50:
            return None
        vx, vy, x0, y0 = (float(c) for c in cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel())
        angle = np.degrees(np.arctan2(vy, vx))
        # Normalize to [0, 180): this avoids the +89.99 vs -90.0 mismatch.
        while angle < 0.0:
            angle += 180.0
        while angle >= 180.0:
            angle -= 180.0
        return {
            'x0': x0, 'y0': y0, 'vx': vx, 'vy': vy,
            'angle': angle, 'n_points': len(pts),
        }

    refined = []
    for l in lines:
        l = l.ravel()  # flatten (1, 4) or (4,) to (4,)
        x1, y1, x2, y2 = int(l[0]), int(l[1]), int(l[2]), int(l[3])
        r = refine(x1, y1, x2, y2)
        if r is not None:
            refined.append(r)

    if len(refined) < 4:
        return _detect_card_quad_fallback(img_bgr), True

    # ----- 4) Separate into H and V clusters -----
    # With angles now in [0, 180): H = near 0 or 180, V = near 90.
    horiz = [r for r in refined if r['angle'] < 45.0 or r['angle'] >= 135.0]
    vert  = [r for r in refined if 45.0 <= r['angle'] < 135.0]
    # Normalize horizontal angles near 180 down to negative (so mean works)
    for r in horiz:
        if r['angle'] >= 135.0:
            r['angle'] -= 180.0
    # Normalize vertical angles around 90 for averaging
    for r in vert:
        r['angle'] -= 90.0

    if not horiz or not vert:
        return _detect_card_quad_fallback(img_bgr), True

    # ----- 5) Weighted average line for each cluster -----
    def cluster_line(items, is_vertical=False):
        # Merge by weighted average: point + direction
        ang = np.array([r['angle'] for r in items], dtype=np.float64)
        wts = np.array([r['n_points'] for r in items], dtype=np.float64)
        avg_ang = float(np.average(ang, weights=wts))
        # Average reference point (weighted)
        x0 = float(np.average([r['x0'] for r in items], weights=wts))
        y0 = float(np.average([r['y0'] for r in items], weights=wts))
        # For vertical: add 90 back to get the actual direction (near 90 deg)
        direction_ang = avg_ang + 90.0 if is_vertical else avg_ang
        rad = np.radians(direction_ang)
        vx = float(np.cos(rad))
        vy = float(np.sin(rad))
        return {'x0': x0, 'y0': y0, 'vx': vx, 'vy': vy, 'angle': direction_ang}

    h_line = cluster_line(horiz, is_vertical=False)
    v_line = cluster_line(vert, is_vertical=True)

    # ----- 6) Intersections of the two lines with themselves -----
    # Since Hough can find multiple parallel lines, we need TWO parallel
    # lines per direction (top and bottom for H, left and right for V).
    # We compute them by projecting each refined line's reference point
    # onto the normal of the averaged direction, then taking the two
    # extremes.

    def cluster_lines_two(items, avg_ang):
        """
        Split the lines of one cluster into two groups corresponding to the
        two opposite sides (e.g. left and right for vertical lines).
        We sort the lines by their offset along the normal direction and
        split at the largest gap between consecutive offsets. This is more
        robust than splitting at the midpoint when extra lines are present.
        """
        rad = np.radians(avg_ang)
        n = np.array([-np.sin(rad), np.cos(rad)])
        offs = []
        for r in items:
            p = np.array([r['x0'], r['y0']])
            offs.append((float(np.dot(p, n)), r))
        offs.sort(key=lambda t: t[0])

        if len(offs) < 2:
            return None, None

        # Find the largest gap between consecutive offsets
        gaps = []
        for i in range(len(offs) - 1):
            gap = offs[i + 1][0] - offs[i][0]
            gaps.append((gap, i))
        gaps.sort(reverse=True)
        largest_gap, split_idx = gaps[0]

        # Sanity: the gap must be at least 50 pixels. Otherwise the lines
        # are all clustered together and we cannot separate the two sides.
        if largest_gap < 50.0:
            return None, None

        low = [r for o, r in offs[:split_idx + 1]]
        high = [r for o, r in offs[split_idx + 1:]]

        def avg_offset(group):
            xs_ = [r['x0'] for r in group]
            ys_ = [r['y0'] for r in group]
            pts = np.column_stack([xs_, ys_]).astype(np.float64)
            return float(np.mean(pts @ n))

        off_low = avg_offset(low)
        off_high = avg_offset(high)
        p_low = off_low * n
        p_high = off_high * n
        return (
            {'x0': p_low[0], 'y0': p_low[1], 'vx': np.cos(rad), 'vy': np.sin(rad)},
            {'x0': p_high[0], 'y0': p_high[1], 'vx': np.cos(rad), 'vy': np.sin(rad)},
        )

    h_top, h_bot = cluster_lines_two(horiz, h_line['angle'])
    v_left, v_right = cluster_lines_two(vert, v_line['angle'])

    # ----- Smart fallback: reconstruct missing sides -----
    bb_x0 = int(x0_s / bbox_scale)
    bb_y0 = int(y0_s / bbox_scale)
    bb_x1 = int((x0_s + w_s) / bbox_scale)
    bb_y1 = int((y0_s + h_s) / bbox_scale)

    def _line_at_offset(angle_deg, offset, normal_dir):
        """Build a line dict with given angle and offset along normal_dir."""
        rad = np.radians(angle_deg)
        vx, vy = np.cos(rad), np.sin(rad)
        n = np.array([-np.sin(rad), np.cos(rad)])
        # If the given normal points the other way, flip it
        if np.dot(n, normal_dir) < 0:
            n = -n
        p = offset * n
        return {'x0': float(p[0]), 'y0': float(p[1]),
                'vx': vx, 'vy': vy, 'angle': angle_deg}

    def _point_offset(point, angle_deg):
        """Offset of a point along the normal of a line with angle_deg."""
        rad = np.radians(angle_deg)
        n = np.array([-np.sin(rad), np.cos(rad)])
        return float(np.dot(np.array(point), n))

    # Reconstruct horizontal sides
    if h_top is not None and h_bot is None:
        # Only top found: make bottom parallel, passing through bb_y1
        ang = h_top['angle']
        p = (bb_x1, bb_y1)  # use bottom-right corner of bbox
        off_top = _point_offset((h_top['x0'], h_top['y0']), ang)
        n = np.array([-np.sin(np.radians(ang)), np.cos(np.radians(ang))])
        off_bot = _point_offset(p, ang)
        if abs(off_bot - off_top) < 50:
            off_bot = off_top + max(bb_y1 - bb_y0, 100)
        h_bot = _line_at_offset(ang, off_bot, n)
    elif h_bot is not None and h_top is None:
        ang = h_bot['angle']
        p = (bb_x0, bb_y0)
        off_bot = _point_offset((h_bot['x0'], h_bot['y0']), ang)
        n = np.array([-np.sin(np.radians(ang)), np.cos(np.radians(ang))])
        off_top = _point_offset(p, ang)
        if abs(off_bot - off_top) < 50:
            off_top = off_bot - max(bb_y1 - bb_y0, 100)
        h_top = _line_at_offset(ang, off_top, n)
    elif h_top is None and h_bot is None:
        # Both missing: create two horizontal lines at bb_y0 and bb_y1
        h_top = _line_at_offset(0.0, float(bb_y0), np.array([0.0, 1.0]))
        h_bot = _line_at_offset(0.0, float(bb_y1), np.array([0.0, 1.0]))

    # Reconstruct vertical sides
    if v_left is not None and v_right is None:
        ang = v_left['angle']
        p = (bb_x1, bb_y1)
        off_left = _point_offset((v_left['x0'], v_left['y0']), ang)
        n = np.array([-np.sin(np.radians(ang)), np.cos(np.radians(ang))])
        off_right = _point_offset(p, ang)
        if abs(off_right - off_left) < 50:
            off_right = off_left + max(bb_x1 - bb_x0, 100)
        v_right = _line_at_offset(ang, off_right, n)
    elif v_right is not None and v_left is None:
        ang = v_right['angle']
        p = (bb_x0, bb_y0)
        off_right = _point_offset((v_right['x0'], v_right['y0']), ang)
        n = np.array([-np.sin(np.radians(ang)), np.cos(np.radians(ang))])
        off_left = _point_offset(p, ang)
        if abs(off_right - off_left) < 50:
            off_left = off_right - max(bb_x1 - bb_x0, 100)
        v_left = _line_at_offset(ang, off_left, n)
    elif v_left is None and v_right is None:
        # Both missing: create two vertical lines at bb_x0 and bb_x1
        v_left = _line_at_offset(90.0, float(bb_x0), np.array([1.0, 0.0]))
        v_right = _line_at_offset(90.0, float(bb_x1), np.array([1.0, 0.0]))

    # ----- 7) Intersections to get 4 corners -----
    def intersect(l1, l2):
        # l1: x = x1 + t1*vx1, y = y1 + t1*vy1
        # l2: x = x2 + t2*vx2, y = y2 + t2*vy2
        A = np.array([[l1['vx'], -l2['vx']], [l1['vy'], -l2['vy']]], dtype=np.float64)
        b = np.array([l2['x0'] - l1['x0'], l2['y0'] - l1['y0']], dtype=np.float64)
        det = A[0, 0] * A[1, 1] - A[0, 1] * A[1, 0]
        if abs(det) < 1e-9:
            return None
        t1, t2 = np.linalg.solve(A, b)
        return np.array([l1['x0'] + t1 * l1['vx'], l1['y0'] + t1 * l1['vy']],
                        dtype=np.float32)

    corners = [
        intersect(v_left, h_top),
        intersect(v_right, h_top),
        intersect(v_right, h_bot),
        intersect(v_left, h_bot),
    ]
    if any(c is None for c in corners):
        return _detect_card_quad_fallback(img_bgr), True

    quad = np.array(corners, dtype=np.float32)
    quad[:, 0] = np.clip(quad[:, 0], 0, w - 1)
    quad[:, 1] = np.clip(quad[:, 1], 0, h - 1)

    return _order_corners(quad), False


def _detect_card_quad_fallback(img_bgr):
    """
    Previous threshold-based detection. Used only if the Sobel + Hough
    method fails to find 4 plausible lines.
    """
    h, w = img_bgr.shape[:2]
    max_side = 900.0
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        small = cv2.resize(img_bgr, (int(w * scale), int(h * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img_bgr
    sh, sw = small.shape[:2]

    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    background_mask = ((gray > 210) & (hsv[:, :, 1] < 20)).astype(np.uint8) * 255
    non_bg_mask = 255 - background_mask
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    non_bg_mask = cv2.morphologyEx(non_bg_mask, cv2.MORPH_CLOSE, kernel, iterations=3)
    non_bg_mask = cv2.morphologyEx(non_bg_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(non_bg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)

    img_area = sh * sw
    candidates = [c for c in contours if cv2.contourArea(c) > 0.05 * img_area]
    if not candidates:
        candidates = contours
    best = max(candidates, key=cv2.contourArea)
    rect = cv2.minAreaRect(best)
    quad_small = cv2.boxPoints(rect).astype(np.float32)
    quad = quad_small / scale
    quad[:, 0] = np.clip(quad[:, 0], 0, w - 1)
    quad[:, 1] = np.clip(quad[:, 1], 0, h - 1)
    return _order_corners(quad)


def _identify_photo_angle(quad):
    """
    Given the 4 vertices of the photo (in any order, but typically
    TL, TR, BR, BL), return the rotation angle that aligns the photo
    to the horizontal axis.

    Logic:
      1. For each vertex, compute its internal angle.
      2. Pick the vertex whose internal angle is closest to 90 deg.
         That is the most "square" corner of the photo.
      3. From that vertex, take the two adjacent edges and pick the one
         that is most horizontal (smallest absolute angle w.r.t. the X axis).
      4. Return that angle.

    The returned angle is in degrees, in the range (-45, 45].
    """
    pts = np.asarray(quad, dtype=np.float64)
    n = 4

    # 1) Internal angles
    best_idx = 0
    best_dev = None
    for i in range(n):
        prev_p = pts[(i - 1) % n]
        curr_p = pts[i]
        next_p = pts[(i + 1) % n]
        v1 = prev_p - curr_p
        v2 = next_p - curr_p
        n1 = np.linalg.norm(v1)
        n2 = np.linalg.norm(v2)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        cos_a = np.dot(v1, v2) / (n1 * n2)
        angle_internal = np.degrees(np.arccos(np.clip(cos_a, -1.0, 1.0)))
        dev = abs(angle_internal - 90.0)
        if best_dev is None or dev < best_dev:
            best_dev = dev
            best_idx = i

    # 2) Two edges from the best vertex
    curr_p = pts[best_idx]
    prev_p = pts[(best_idx - 1) % n]
    next_p = pts[(best_idx + 1) % n]

    candidates = []
    for other in (prev_p, next_p):
        dx = other[0] - curr_p[0]
        dy = other[1] - curr_p[1]
        # Normalise direction so dx >= 0 (avoid 180-deg flips)
        if dx < 0:
            dx, dy = -dx, -dy
        angle = np.degrees(np.arctan2(dy, dx))
        candidates.append(angle)

    # 3) Pick the most horizontal edge
    angle = min(candidates, key=lambda a: abs(a))

    # 4) Normalise to (-45, 45]
    while angle <= -45.0:
        angle += 90.0
    while angle > 45.0:
        angle -= 90.0

    return float(angle)


def _polygon_area(pts):
    """Shoelace formula. pts is (N,2)."""
    p = np.asarray(pts, dtype=np.float64)
    x = p[:, 0]
    y = p[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _align_angle_from_pair(p1, p2, p3, p4):
    """
    Return the mean angle (in degrees) of the two edges (p1->p2) and (p3->p4)
    with respect to the horizontal axis, normalised to (-45, 45].
    """
    a1 = np.degrees(np.arctan2(p2[1] - p1[1], p2[0] - p1[0]))
    a2 = np.degrees(np.arctan2(p4[1] - p3[1], p4[0] - p3[0]))
    # Normalise each to (-90, 90]
    def norm90(a):
        while a <= -90.0:
            a += 180.0
        while a > 90.0:
            a -= 180.0
        return a
    a1 = norm90(a1)
    a2 = norm90(a2)
    # Average of the two angles, handling the 180-deg ambiguity
    if abs(a1 - a2) > 90.0:
        if a1 > a2:
            a2 += 180.0
        else:
            a1 += 180.0
    ang = (a1 + a2) / 2.0
    # Normalise final to (-45, 45]
    while ang <= -45.0:
        ang += 90.0
    while ang > 45.0:
        ang -= 90.0
    return float(ang)


def _compute_alignment_options(quad):
    """
    Given the 4 vertices of the photo (TL, TR, BR, BL), compute the two
    possible alignment angles:
      - angle_h: rotation that aligns the horizontal pair (TL-TR, BL-BR)
      - angle_v: rotation that aligns the vertical pair (TL-BL, TR-BR)
    """
    pts = np.asarray(quad, dtype=np.float64)
    tl, tr, br, bl = pts

    angle_h = _align_angle_from_pair(tl, tr, bl, br)

    angle_v = _align_angle_from_pair(tl, bl, tr, br) - 90.0
    while angle_v <= -45.0:
        angle_v += 90.0
    while angle_v > 45.0:
        angle_v -= 90.0

    # Negate both angles so that the sign convention matches the user's
    # expectation: + means clockwise, - means counter-clockwise.
    # _rotate_image already rotates clockwise for positive angles, so
    # passing the raw angle of the side (which is measured from the axis
    # in OpenCV convention) would rotate the wrong way.
    return {
        'angle_h': -float(angle_h),
        'angle_v': -float(angle_v),
    }


def _rotate_image(img_bgr, angle_deg, interp=cv2.INTER_CUBIC):
    h, w = img_bgr.shape[:2]
    center = (w / 2.0, h / 2.0)
    # Negate so positive angles rotate clockwise on screen.
    M = cv2.getRotationMatrix2D(center, -angle_deg, 1.0)
    cos = abs(M[0, 0])
    sin = abs(M[0, 1])
    new_w = int(h * sin + w * cos)
    new_h = int(h * cos + w * sin)
    M[0, 2] += (new_w / 2.0) - center[0]
    M[1, 2] += (new_h / 2.0) - center[1]
    rotated = cv2.warpAffine(img_bgr, M, (new_w, new_h),
                             flags=interp,
                             borderMode=cv2.BORDER_REPLICATE)
    return rotated, M


def _apply_affine_to_pts(pts, M):
    pts = np.asarray(pts, dtype=np.float32)
    ones = np.ones((pts.shape[0], 1), dtype=np.float32)
    homo = np.hstack([pts, ones])
    out = homo @ M.T
    return out.astype(np.float32)


def _clip_quad_to_image(quad, img_w, img_h):
    q = np.copy(quad).astype(np.float32)
    q[:, 0] = np.clip(q[:, 0], 0, img_w - 1)
    q[:, 1] = np.clip(q[:, 1], 0, img_h - 1)
    return q


def _inscribed_rect_from_quad(quad):
    """
    Given a quad (4,2) that is *almost* axis-aligned (which is the case
    after we rotate the whole image by the angle of the short sides), return
    the largest axis-aligned rectangle that is guaranteed to lie inside it.

    For an almost-axis-aligned quad, this is simply:
        x_min = max(x of all vertices on the left side)
        x_max = min(x of all vertices on the right side)
        y_min = max(y of all vertices on the top side)
        y_max = min(y of all vertices on the bottom side)

    Because the quad is ordered TL, TR, BR, BL (clockwise in image coords),
    we can identify the sides directly.
    """
    pts = np.asarray(quad, dtype=np.float64)
    if pts.shape != (4, 2):
        raise ValueError("quad must be a 4x2 array")

    tl, tr, br, bl = pts

    # For each side of the quad, take the most restrictive coordinate:
    # - x_min: the rightmost of {tl.x, bl.x}  (left side)
    # - x_max: the leftmost  of {tr.x, br.x}  (right side)
    # - y_min: the bottommost of {tl.y, tr.y} (top side)
    # - y_max: the topmost of {bl.y, br.y}    (bottom side)
    x_min = max(tl[0], bl[0])
    x_max = min(tr[0], br[0])
    y_min = max(tl[1], tr[1])
    y_max = min(bl[1], br[1])

    # Safety: ensure valid rectangle
    if x_max <= x_min or y_max <= y_min:
        cx = float(pts[:, 0].mean())
        cy = float(pts[:, 1].mean())
        return np.array([
            [cx - 1.0, cy - 1.0],
            [cx + 1.0, cy - 1.0],
            [cx + 1.0, cy + 1.0],
            [cx - 1.0, cy + 1.0],
        ], dtype=np.float32)

    return np.array([
        [x_min, y_min],
        [x_max, y_min],
        [x_max, y_max],
        [x_min, y_max],
    ], dtype=np.float32)


# ---------------------------------------------------------------------------
#  Interactive crop dialog
# ---------------------------------------------------------------------------

class InteractiveCropDialog(tk.Toplevel):
    CORNER_NAMES = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"]
    ANGLE_STEPS = [0.01, 0.10, 1.00]

    def __init__(self, parent, on_confirm=None, on_skip=None):
        super().__init__(parent)
        self.on_confirm = on_confirm
        self.on_skip = on_skip
        self.title("Photo Crop - Interactive Inspector")
        self.update_idletasks()
        cfg = load_config()
        saved_geo = cfg.get("crop_geometry")
        if saved_geo:
            try:
                self.geometry(saved_geo)
            except Exception:
                saved_geo = None
        if not saved_geo:
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            win_w = min(1900, screen_w - 40)
            win_h = min(1010, screen_h - 70)
            self.geometry(f"{win_w}x{win_h}+20+20")
        # Save geometry on close
        self.bind("<Destroy>", self._save_geometry_on_destroy)
        self.transient(parent)
        self.configure(bg="#0d1116")

        self.image_path = None
        self.img_bgr_orig = None
        self.img_full = None
        self.interp_flag = cv2.INTER_CUBIC
        self.h_orig = 0
        self.w_orig = 0
        self.initial_quad = None
        self.angle_step_idx = 0
        # Zoom levels (in source pixels) for the corner/edge inspectors.
        self.ZOOM_LEVELS = [25, 45, 100, 200]
        self.zoom_idx = 1  # default = 45
        self.INSPECTOR_BOX = self.ZOOM_LEVELS[self.zoom_idx]

        self.angle_h = 0.0
        self.angle_v = 0.0
        self.alignment_mode = 'H'
        self.rotation_angle = 0.0
        self.img_bgr = None
        self.rot_matrix = None
        self.h_rot = 0
        self.w_rot = 0
        self.img_rgb = None
        self.pil_rot = None
        self.rect = [0.0, 0.0, 1.0, 1.0]
        self.pts = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)

        self.active_corner_idx = 0
        self.inspector_axis = 'h'
        self.result_pts = None
        self.result_angle = None
        self.result_rotated_img = None
        self.dragging_item = None
        self.drag_start_pts = None
        self.drag_start_mouse_x = 0
        self.drag_start_mouse_y = 0
        self.scale_full = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.nudge_step_val = 1
        self.shift_held = False
        self.alt_held = False
        self.held_corners = set()
        self.last_zoom_x = 0
        self.last_zoom_y = 0
        self._view_update_scheduled = False
        self._full_preview_cache_key = None
        self._full_preview_dirty = True
        self._corner_zoom_cache_key = None
        self._edge_zoom_cache_key = None

        main_frame = tk.Frame(self, bg="#0d1116", padx=12, pady=12)
        main_frame.pack(fill=tk.BOTH, expand=True)
        self.lbl_header = tk.Label(main_frame,
                                    text="PRECISION CROP REVIEW",
                                    font=("Inter", 18, "bold"), bg="#0d1116", fg="#ffffff")
        self.lbl_header.pack(pady=2)

        content_pane = tk.Frame(main_frame, bg="#0d1116")
        content_pane.pack(fill=tk.BOTH, expand=True, pady=4)
        content_pane.grid_columnconfigure(0, weight=1, uniform="equal_cols")
        content_pane.grid_columnconfigure(1, weight=1, uniform="equal_cols")
        content_pane.grid_rowconfigure(0, weight=1)

        left_frame = tk.Frame(content_pane, bg="#161b22", bd=1, relief="solid")
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.canvas_full = tk.Canvas(left_frame, bg="#0d1116", bd=0, highlightthickness=0)
        self.canvas_full.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.canvas_full.bind("<Configure>", self.on_full_canvas_resize)
        self.canvas_full.bind("<Button-1>", self.on_full_click)
        self.canvas_full.bind("<B1-Motion>", self.on_full_drag)
        self.canvas_full.bind("<ButtonRelease-1>", self.on_full_release)

        right_frame = tk.Frame(content_pane, bg="#161b22", bd=1, relief="solid", padx=10, pady=10)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))


        tabs_frame = tk.Frame(right_frame, bg="#161b22")
        tabs_frame.pack(fill=tk.X, pady=2)
        self.corner_buttons = []
        for i, name in enumerate(self.CORNER_NAMES):
            btn = SegmentedButton(tabs_frame, text=f"{i+1}  {name}",
                                  command=lambda idx=i: self.select_corner(idx),
                                  font=("Inter", 18, "bold"),
                                  width=200, height=46)
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
            btn.draw(i == 0)
            self.corner_buttons.append(btn)

        controls_frame = tk.Frame(right_frame, bg="#161b22")
        controls_frame.pack(fill=tk.X, pady=4)

        def _cbtn(text, cmd, width=80, padx=None, w_px=None, side=tk.LEFT, pad=1):
            b = SegmentedButton(controls_frame, text=text, command=cmd,
                                font=("Inter", 18, "bold"),
                                width=w_px if w_px else 80,
                                height=46)
            b.draw(False)
            b.pack(side=side, padx=pad)
            return b

        tk.Label(controls_frame, text="Angle", font=("Inter", 18, "bold"),
                 bg="#161b22", fg="#ffffff").pack(side=tk.LEFT, padx=(0, 4))
        _cbtn("-", lambda: self.adjust_angle(-self.get_angle_step()), w_px=55)
        self.lbl_angle = tk.Label(controls_frame, text=f"{self.rotation_angle:+.2f}",
                                  font=("Inter", 18, "bold"), bg="#161b22", fg="#ffffff",
                                  width=7, anchor="center")
        self.lbl_angle.pack(side=tk.LEFT, padx=1)
        _cbtn("+", lambda: self.adjust_angle(self.get_angle_step()), w_px=55)
        self.btn_angle_step = _cbtn("0.01", self.cycle_angle_step, w_px=90, pad=4)

        _cbtn("90 CCW", self.rotate_90_ccw, w_px=140)
        _cbtn("90 CW", self.rotate_90_cw, w_px=125)

        # Zoom controls: label "Z xx px" with fixed width + two buttons.
        zoom_label_frame = tk.Frame(controls_frame, bg="#161b22",
                                     width=100, height=46)
        zoom_label_frame.pack(side=tk.LEFT, padx=(8, 4))
        zoom_label_frame.pack_propagate(False)
        self.lbl_zoom = tk.Label(zoom_label_frame,
                                 text=f"{self.INSPECTOR_BOX} px",
                                 font=("Inter", 18, "bold"),
                                 bg="#161b22", fg="#ffffff",
                                 anchor="center")
        self.lbl_zoom.pack(fill=tk.BOTH, expand=True)
        _cbtn("-", self.zoom_in, w_px=50)
        _cbtn("+", self.zoom_out, w_px=50)

        nudge_outer_frame = tk.Frame(right_frame, bg="#161b22")
        nudge_outer_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=4)
        nudge_grid = tk.Frame(nudge_outer_frame, bg="#161b22")
        nudge_grid.pack(anchor=tk.CENTER)
        def _grid_btn(text, cmd, w=120, col=0, row=0, cp=2, rp=2):
            b = SegmentedButton(nudge_grid, text=text, command=cmd,
                                font=("Inter", 18, "bold"),
                                width=w, height=46)
            b.draw(False)
            b.grid(row=row, column=col, padx=cp, pady=rp)
            return b

        _grid_btn("UP", lambda: self.nudge(0, -self.get_active_step(), 'h'), col=1, row=0)
        _grid_btn("LEFT", lambda: self.nudge(-self.get_active_step(), 0, 'v'), col=0, row=1)
        _grid_btn("DOWN", lambda: self.nudge(0, self.get_active_step(), 'h'), col=1, row=1)
        _grid_btn("RIGHT", lambda: self.nudge(self.get_active_step(), 0, 'v'), col=2, row=1)
        self.btn_step = _grid_btn("Step: 1px", self.cycle_step_size, w=140, col=3, row=1, cp=8)

        zoom_inspectors_container = tk.Frame(right_frame, bg="#161b22")
        zoom_inspectors_container.pack(fill=tk.BOTH, expand=True, pady=4)
        zoom_inspectors_container.grid_columnconfigure(0, weight=1, uniform="zoom_cols")
        zoom_inspectors_container.grid_columnconfigure(1, weight=1, uniform="zoom_cols")
        zoom_inspectors_container.grid_rowconfigure(0, weight=1)

        col1_frame = tk.Frame(zoom_inspectors_container, bg="#161b22")
        col1_frame.grid(row=0, column=0, sticky="nsew", padx=2)
        col1_frame.grid_rowconfigure(0, weight=1)
        col1_frame.grid_columnconfigure(0, weight=1)
        self.zoom_canvas = tk.Canvas(col1_frame, bg="#0d1116", bd=1, relief="solid",
                                     highlightthickness=0)
        self.zoom_canvas.grid(row=0, column=0, sticky="nsew")
        self.zoom_canvas.bind("<Button-1>", self.on_zoom_click)
        self.zoom_canvas.bind("<B1-Motion>", self.on_zoom_drag)
        self.zoom_canvas.bind("<Configure>", lambda e: self.schedule_view_update())
        self.lbl_corner_sub = tk.Label(col1_frame, text="", font=("Inter", 18, "bold"),
                                       bg="#161b22", fg="#8b949e")
        self.lbl_corner_sub.grid(row=1, column=0, sticky="ew", pady=(4, 2))

        col2_frame = tk.Frame(zoom_inspectors_container, bg="#161b22")
        col2_frame.grid(row=0, column=1, sticky="nsew", padx=2)
        col2_frame.grid_rowconfigure(0, weight=1)
        col2_frame.grid_columnconfigure(0, weight=1)
        self.zoom_edge_canvas = tk.Canvas(col2_frame, bg="#0d1116", bd=1, relief="solid",
                                          highlightthickness=0)
        self.zoom_edge_canvas.grid(row=0, column=0, sticky="nsew")
        self.zoom_edge_canvas.bind("<Configure>", lambda e: self.schedule_view_update())
        self.lbl_edge_title = tk.Label(col2_frame, text="", font=("Inter", 18, "bold"),
                                       bg="#161b22", fg="#8b949e")
        self.lbl_edge_title.grid(row=1, column=0, sticky="ew", pady=(4, 2))

        action_bar = tk.Frame(main_frame, bg="#0d1116")
        action_bar.pack(fill=tk.X, pady=6)

        # Left side: Reset + alignment buttons
        left_bar = tk.Frame(action_bar, bg="#0d1116")
        left_bar.pack(side=tk.LEFT)

        self.btn_reset = SegmentedButton(left_bar, text="Reset Auto Rect",
                                         command=self.reset_auto,
                                         font=("Inter", 18, "bold"),
                                         width=230, height=46)
        self.btn_reset.draw(False)
        self.btn_reset.pack(side=tk.LEFT, padx=5)

        # Two segmented buttons for H / V alignment
        align_container = tk.Frame(left_bar, bg="#0d1116")
        align_container.pack(side=tk.LEFT, padx=(15, 0))

        self.btn_align_h = SegmentedButton(align_container,
                                           text="Horizontal",
                                           command=lambda: self.set_alignment('H'),
                                           font=("Inter", 18, "bold"),
                                           width=165, height=46)
        self.btn_align_h.pack(side=tk.LEFT, padx=3)

        self.btn_align_v = SegmentedButton(align_container,
                                           text="Vertical",
                                           command=lambda: self.set_alignment('V'),
                                           font=("Inter", 18, "bold"),
                                           width=165, height=46)
        self.btn_align_v.pack(side=tk.LEFT, padx=3)

        # Right side: Confirm + Skip
        right_bar = tk.Frame(action_bar, bg="#0d1116")
        right_bar.pack(side=tk.RIGHT)

        self.btn_confirm = SegmentedButton(right_bar, text="Confirm & Crop",
                                           command=self.confirm,
                                           font=("Inter", 18, "bold"),
                                           width=225, height=46,
                                           bg_off="#ffffff", fg_off="#0d1116")
        self.btn_confirm.draw(False)
        self.btn_confirm.pack(side=tk.LEFT, padx=5)

        self.btn_skip = SegmentedButton(right_bar, text="Skip Image",
                                        command=self.skip,
                                        font=("Inter", 18, "bold"),
                                        width=170, height=46)
        self.btn_skip.draw(False)
        self.btn_skip.pack(side=tk.LEFT, padx=5)

        # Apply current alignment state to the segmented buttons
        self._update_alignment_buttons()

        self.initial_pts_backup = np.copy(self.pts)
        self.bind("<Escape>", lambda e: self.skip())
        self.bind("<Control-w>", lambda e: self.skip())
        self.bind("<Control-q>", lambda e: self.skip())
        self.bind("<Return>", lambda e: self.confirm())
        self.bind("<Up>", self.on_up_key)
        self.bind("<Down>", self.on_down_key)
        self.bind("<Left>", lambda e: self.nudge(-self.get_active_step(), 0, 'v'))
        self.bind("<Right>", lambda e: self.nudge(self.get_active_step(), 0, 'v'))
        self.z_held = False
        self.bind("<KeyPress>", self.on_key_press)
        self.bind("<KeyRelease>", self.on_key_release)
        for k, idx in [('1', 0), ('2', 1), ('3', 2), ('4', 3)]:
            self.bind(f"<KeyPress-{k}>", lambda e, i=idx: self.on_corner_key_press(i))
            self.bind(f"<KeyRelease-{k}>", lambda e, i=idx: self.on_corner_key_release(i))
        self.focus_set()
        self.after(120, self.schedule_view_update)

    def _save_geometry_on_destroy(self, event=None):
        # Only react to the toplevel's own destroy event
        if event is not None and event.widget is not self:
            return
        try:
            geo = self.winfo_geometry()
        except Exception:
            return
        try:
            cfg = load_config()
            cfg["crop_geometry"] = geo
            save_config(cfg)
        except Exception:
            pass

    def load_image(self, image_path, initial_quad, img_full=None, img_view=None):
        """Load a new image into the already-built dialog.
        img_full: the scan at its native bit depth (used for the final crop).
        img_view: its 8-bit version, used for preview and zoom (the same array when the scan is 8-bit)."""
        self.image_path = image_path
        if img_full is None or img_view is None:
            img_full, img_view = load_scan(image_path)
        if img_view is None:
            raise IOError(f"Cannot load image: {image_path}")
        self.img_full = img_full
        self.img_bgr_orig = img_view
        self.interp_flag = INTERPOLATIONS[get_settings()["interpolation"]]
        self.h_orig, self.w_orig = self.img_bgr_orig.shape[:2]
        self.initial_quad = np.copy(initial_quad).astype(np.float32)

        opts = _compute_alignment_options(self.initial_quad)
        self.angle_h = opts['angle_h']
        self.angle_v = opts['angle_v']

        if abs(self.angle_h) <= abs(self.angle_v):
            self.alignment_mode = 'H'
            self.rotation_angle = self.angle_h
        else:
            self.alignment_mode = 'V'
            self.rotation_angle = self.angle_v

        self.img_bgr, self.rot_matrix = _rotate_image(self.img_bgr_orig, self.rotation_angle, self.interp_flag)
        self.h_rot, self.w_rot = self.img_bgr.shape[:2]
        self.img_rgb = cv2.cvtColor(self.img_bgr, cv2.COLOR_BGR2RGB)
        self.pil_rot = Image.fromarray(self.img_rgb)

        rotated_quad = _apply_affine_to_pts(self.initial_quad, self.rot_matrix)
        rotated_quad = _clip_quad_to_image(rotated_quad, self.w_rot, self.h_rot)
        inscribed = _inscribed_rect_from_quad(rotated_quad)
        self.rect = [
            float(inscribed[:, 0].min()),
            float(inscribed[:, 1].min()),
            float(inscribed[:, 0].max()),
            float(inscribed[:, 1].max()),
        ]
        self._update_pts_from_rect()
        self.initial_pts_backup = np.copy(self.pts)

        # Reset caches
        self._full_preview_cache_key = None
        self._full_preview_dirty = True
        self._corner_zoom_cache_key = None
        self._edge_zoom_cache_key = None

        # Update UI
        self.lbl_header.config(text=f"PRECISION CROP REVIEW: {display_name(os.path.basename(image_path))}")
        self.lbl_angle.config(text=f"{self.rotation_angle:+.2f}")
        self._update_alignment_buttons()
        self.select_corner(0)
        self.schedule_view_update()

    def _update_alignment_buttons(self):
        """Redraw the two segmented buttons according to self.alignment_mode."""
        try:
            self.btn_align_h.draw(self.alignment_mode == 'H')
            self.btn_align_v.draw(self.alignment_mode == 'V')
        except AttributeError:
            pass

    def set_alignment(self, mode):
        """
        Force the alignment to 'H' (align horizontal pair) or 'V'
        (align vertical pair). Rebuild the rotated image and the crop
        rectangle accordingly.
        """
        if mode not in ('H', 'V'):
            return
        if mode == self.alignment_mode:
            self._update_alignment_buttons()
            return

        self.alignment_mode = mode
        if mode == 'H':
            self.rotation_angle = self.angle_h
        else:
            self.rotation_angle = self.angle_v

        # Rebuild rotated image
        self.img_bgr, self.rot_matrix = _rotate_image(self.img_bgr_orig, self.rotation_angle, self.interp_flag)
        self.h_rot, self.w_rot = self.img_bgr.shape[:2]
        self.img_rgb = cv2.cvtColor(self.img_bgr, cv2.COLOR_BGR2RGB)
        self.pil_rot = Image.fromarray(self.img_rgb)

        # Recompute the crop rectangle from the initial quad
        rotated_quad = _apply_affine_to_pts(self.initial_quad, self.rot_matrix)
        rotated_quad = _clip_quad_to_image(rotated_quad, self.w_rot, self.h_rot)
        inscribed = _inscribed_rect_from_quad(rotated_quad)
        self.rect = [
            float(inscribed[:, 0].min()),
            float(inscribed[:, 1].min()),
            float(inscribed[:, 0].max()),
            float(inscribed[:, 1].max()),
        ]
        self._update_pts_from_rect()
        self.initial_pts_backup = np.copy(self.pts)

        self.lbl_angle.config(text=f"{self.rotation_angle:+.2f}")
        self._full_preview_dirty = True
        self._corner_zoom_cache_key = None
        self._edge_zoom_cache_key = None
        self._update_alignment_buttons()
        self.schedule_view_update()

    def zoom_in(self):
        if self.zoom_idx < len(self.ZOOM_LEVELS) - 1:
            self.zoom_idx += 1
            self.INSPECTOR_BOX = self.ZOOM_LEVELS[self.zoom_idx]
            self.lbl_zoom.config(text=f"{self.INSPECTOR_BOX} px")
            self._corner_zoom_cache_key = None
            self._edge_zoom_cache_key = None
            self.schedule_view_update()

    def zoom_out(self):
        if self.zoom_idx > 0:
            self.zoom_idx -= 1
            self.INSPECTOR_BOX = self.ZOOM_LEVELS[self.zoom_idx]
            self.lbl_zoom.config(text=f"{self.INSPECTOR_BOX} px")
            self._corner_zoom_cache_key = None
            self._edge_zoom_cache_key = None
            self.schedule_view_update()

    def adjust_angle(self, delta_deg):
        self.rotation_angle += delta_deg

        inv = cv2.invertAffineTransform(self.rot_matrix)
        pts_in_orig = _apply_affine_to_pts(self.pts, inv)

        self.img_bgr, self.rot_matrix = _rotate_image(self.img_bgr_orig, self.rotation_angle, self.interp_flag)
        self.h_rot, self.w_rot = self.img_bgr.shape[:2]
        self.img_rgb = cv2.cvtColor(self.img_bgr, cv2.COLOR_BGR2RGB)
        self.pil_rot = Image.fromarray(self.img_rgb)

        new_pts = _apply_affine_to_pts(pts_in_orig, self.rot_matrix)
        new_pts = _clip_quad_to_image(new_pts, self.w_rot, self.h_rot)

        old_w = self.rect[2] - self.rect[0]
        old_h = self.rect[3] - self.rect[1]
        cx = float(new_pts[:, 0].mean())
        cy = float(new_pts[:, 1].mean())
        half_w = old_w / 2.0
        half_h = old_h / 2.0
        self.rect = [cx - half_w, cy - half_h, cx + half_w, cy + half_h]
        if self.rect[0] < 0:
            self.rect[2] -= self.rect[0]
            self.rect[0] = 0
        if self.rect[1] < 0:
            self.rect[3] -= self.rect[1]
            self.rect[1] = 0
        if self.rect[2] > self.w_rot - 1:
            shift = self.rect[2] - (self.w_rot - 1)
            self.rect[0] -= shift
            self.rect[2] -= shift
        if self.rect[3] > self.h_rot - 1:
            shift = self.rect[3] - (self.h_rot - 1)
            self.rect[1] -= shift
            self.rect[3] -= shift

        self._update_pts_from_rect()
        self.lbl_angle.config(text=f"{self.rotation_angle:+.2f}")
        self._full_preview_dirty = True
        self._corner_zoom_cache_key = None
        self._edge_zoom_cache_key = None
        self.schedule_view_update()

    def cycle_angle_step(self):
        self.angle_step_idx = (self.angle_step_idx + 1) % len(self.ANGLE_STEPS)
        self.btn_angle_step.set_text(f"{self.ANGLE_STEPS[self.angle_step_idx]:.2f}")

    def get_angle_step(self):
        return self.ANGLE_STEPS[self.angle_step_idx]

    def rotate_90(self, clockwise):
        delta = 90.0 if clockwise else -90.0
        inv = cv2.invertAffineTransform(self.rot_matrix)
        pts_in_orig = _apply_affine_to_pts(self.pts, inv)

        self.rotation_angle += delta
        self.img_bgr, self.rot_matrix = _rotate_image(self.img_bgr_orig, self.rotation_angle, self.interp_flag)
        self.h_rot, self.w_rot = self.img_bgr.shape[:2]
        self.img_rgb = cv2.cvtColor(self.img_bgr, cv2.COLOR_BGR2RGB)
        self.pil_rot = Image.fromarray(self.img_rgb)

        new_pts = _apply_affine_to_pts(pts_in_orig, self.rot_matrix)
        new_pts = _clip_quad_to_image(new_pts, self.w_rot, self.h_rot)

        old_w = self.rect[2] - self.rect[0]
        old_h = self.rect[3] - self.rect[1]
        new_w = old_h
        new_h = old_w
        cx = float(new_pts[:, 0].mean())
        cy = float(new_pts[:, 1].mean())
        half_w = new_w / 2.0
        half_h = new_h / 2.0
        self.rect = [cx - half_w, cy - half_h, cx + half_w, cy + half_h]
        if self.rect[0] < 0:
            self.rect[2] -= self.rect[0]
            self.rect[0] = 0
        if self.rect[1] < 0:
            self.rect[3] -= self.rect[1]
            self.rect[1] = 0
        if self.rect[2] > self.w_rot - 1:
            shift = self.rect[2] - (self.w_rot - 1)
            self.rect[0] -= shift
            self.rect[2] -= shift
        if self.rect[3] > self.h_rot - 1:
            shift = self.rect[3] - (self.h_rot - 1)
            self.rect[1] -= shift
            self.rect[3] -= shift

        self._update_pts_from_rect()
        self.lbl_angle.config(text=f"{self.rotation_angle:+.2f}")
        self._full_preview_dirty = True
        self._corner_zoom_cache_key = None
        self._edge_zoom_cache_key = None
        self.schedule_view_update()

    def rotate_90_cw(self):
        self.rotate_90(True)

    def rotate_90_ccw(self):
        self.rotate_90(False)

    def schedule_view_update(self):
        if self._view_update_scheduled:
            return
        self._view_update_scheduled = True
        self.after_idle(self._do_view_update)

    def _do_view_update(self):
        self._view_update_scheduled = False
        if not self.winfo_exists():
            return
        self.update_views()

    def on_up_key(self, event):
        if getattr(self, 'z_held', False):
            self.zoom_out()
        else:
            self.nudge(0, -self.get_active_step(), 'h')

    def on_down_key(self, event):
        if getattr(self, 'z_held', False):
            self.zoom_in()
        else:
            self.nudge(0, self.get_active_step(), 'h')

    def on_key_press(self, event):
        if event.keysym in ('Shift_L', 'Shift_R'):
            self.shift_held = True
        elif event.keysym in ('Alt_L', 'Alt_R'):
            self.alt_held = True
        elif event.keysym.lower() == 'z':
            self.z_held = True

    def on_key_release(self, event):
        if event.keysym in ('Shift_L', 'Shift_R'):
            self.shift_held = False
        elif event.keysym in ('Alt_L', 'Alt_R'):
            self.alt_held = False
        elif event.keysym.lower() == 'z':
            self.z_held = False

    def get_active_step(self):
        if self.shift_held and self.alt_held:
            return 20
        elif self.shift_held:
            return 10
        return self.nudge_step_val

    def on_corner_key_press(self, idx):
        self.held_corners.add(idx)
        self.select_corner(idx)

    def on_corner_key_release(self, idx):
        if idx in self.held_corners:
            self.held_corners.remove(idx)

    def cycle_step_size(self):
        if self.nudge_step_val == 1:
            self.nudge_step_val = 10
        elif self.nudge_step_val == 10:
            self.nudge_step_val = 20
        else:
            self.nudge_step_val = 1
        self.btn_step.set_text(f"Step: {self.nudge_step_val}px")

    def select_corner(self, idx):
        self.active_corner_idx = idx
        self.inspector_axis = 'h'
        for i, btn in enumerate(self.corner_buttons):
            btn.draw(i == idx)
        self.schedule_view_update()

    def _update_pts_from_rect(self):
        x_min, y_min, x_max, y_max = self.rect
        self.pts = np.array([
            [x_min, y_min],
            [x_max, y_min],
            [x_max, y_max],
            [x_min, y_max],
        ], dtype=np.float32)

    def _move_pts_keeping_square(self, dx, dy):
        """
        Move the dragged corner keeping the rectangle axis-aligned in the
        rotated frame.  dx, dy are deltas in rotated-image coordinates.
        """
        target_indices = self.held_corners if self.held_corners else {self.active_corner_idx}

        x_min, y_min, x_max, y_max = self.rect

        for idx in target_indices:
            if idx == 0:      # TL
                x_min += dx
                y_min += dy
            elif idx == 1:    # TR
                x_max += dx
                y_min += dy
            elif idx == 2:    # BR
                x_max += dx
                y_max += dy
            elif idx == 3:    # BL
                x_min += dx
                y_max += dy

        if x_min > x_max:
            x_min, x_max = x_max, x_min
        if y_min > y_max:
            y_min, y_max = y_max, y_min

        x_min = max(0.0, min(x_min, self.w_rot - 1.0))
        x_max = max(0.0, min(x_max, self.w_rot - 1.0))
        y_min = max(0.0, min(y_min, self.h_rot - 1.0))
        y_max = max(0.0, min(y_max, self.h_rot - 1.0))

        self.rect = [x_min, y_min, x_max, y_max]
        self._update_pts_from_rect()

    def nudge(self, dx, dy, axis):
        self.inspector_axis = axis
        self._move_pts_keeping_square(dx, dy)
        self.schedule_view_update()

    def reset_auto(self):
        # Recompute the best alignment (smaller absolute angle)
        if abs(self.angle_h) <= abs(self.angle_v):
            best_mode = 'H'
        else:
            best_mode = 'V'

        if best_mode != self.alignment_mode:
            self.set_alignment(best_mode)
            return

        # Same alignment: just rebuild the crop rectangle
        rotated_quad = _apply_affine_to_pts(self.initial_quad, self.rot_matrix)
        rotated_quad = _clip_quad_to_image(rotated_quad, self.w_rot, self.h_rot)
        inscribed = _inscribed_rect_from_quad(rotated_quad)
        self.rect = [
            float(inscribed[:, 0].min()),
            float(inscribed[:, 1].min()),
            float(inscribed[:, 0].max()),
            float(inscribed[:, 1].max()),
        ]
        self._update_pts_from_rect()
        self.schedule_view_update()

    def on_full_canvas_resize(self, event=None):
        self._full_preview_dirty = True
        self.schedule_view_update()

    def update_full_preview(self):
        c_width = self.canvas_full.winfo_width()
        c_height = self.canvas_full.winfo_height()
        if c_width < 10 or c_height < 10:
            return
        cache_key = (c_width, c_height)
        if self._full_preview_dirty or self._full_preview_cache_key != cache_key:
            self.scale_full = min(c_width / self.w_rot, c_height / self.h_rot)
            new_w = max(1, int(self.w_rot * self.scale_full))
            new_h = max(1, int(self.h_rot * self.scale_full))
            pil_resized = self.pil_rot.resize((new_w, new_h), Image.Resampling.BILINEAR)
            self.full_tk_img = ImageTk.PhotoImage(pil_resized)
            self.offset_x = (c_width - new_w) // 2
            self.offset_y = (c_height - new_h) // 2
            self._full_preview_cache_key = cache_key
            self._full_preview_dirty = False
            self.canvas_full.delete("bg")
            self.canvas_full.create_image(self.offset_x, self.offset_y, anchor=tk.NW,
                                          image=self.full_tk_img, tags="bg")
        self.canvas_full.delete("overlay")
        scaled_pts = []
        for pt in self.pts:
            sx = pt[0] * self.scale_full + self.offset_x
            sy = pt[1] * self.scale_full + self.offset_y
            scaled_pts.append((sx, sy))
        for i in range(4):
            p1 = scaled_pts[i]
            p2 = scaled_pts[(i + 1) % 4]
            self.canvas_full.create_line(p1[0], p1[1], p2[0], p2[1],
                                         fill="#00ffcc", width=4, tags="overlay")
        for i, (sx, sy) in enumerate(scaled_pts):
            color = "#ffffff" if (i == self.active_corner_idx or i in self.held_corners) else "#00ffcc"
            r = 14 if (i == self.active_corner_idx or i in self.held_corners) else 9
            self.canvas_full.create_oval(sx - r, sy - r, sx + r, sy + r,
                                         fill=color, outline="#0d1116", width=2,
                                         tags="overlay")

    def update_corner_zoom(self):
        z_w = self.zoom_canvas.winfo_width()
        z_h = self.zoom_canvas.winfo_height()
        if z_w < 20 or z_h < 20:
            return
        box_size = self.INSPECTOR_BOX
        cx, cy = int(self.pts[self.active_corner_idx][0]), int(self.pts[self.active_corner_idx][1])
        x1 = max(0, cx - box_size // 2)
        y1 = max(0, cy - box_size // 2)
        x2 = min(self.w_rot, cx + box_size // 2)
        y2 = min(self.h_rot, cy + box_size // 2)
        corner_cache_key = (x1, y1, x2, y2, z_w, z_h)
        if self._corner_zoom_cache_key != corner_cache_key:
            crop_rgb = self.img_rgb[y1:y2, x1:x2]
            if crop_rgb.size > 0:
                zoomed = cv2.resize(crop_rgb, (z_w, z_h), interpolation=cv2.INTER_NEAREST)
                self.zoom_tk_img = ImageTk.PhotoImage(Image.fromarray(zoomed))
                self._corner_zoom_cache_key = corner_cache_key
                self.zoom_canvas.delete("all")
                self.zoom_canvas.create_image(0, 0, anchor=tk.NW, image=self.zoom_tk_img)
                self.zoom_canvas.create_line(z_w // 2, 0, z_w // 2, z_h,
                                             fill="#ff3333", width=1, dash=(4, 4))
                self.zoom_canvas.create_line(0, z_h // 2, z_w, z_h // 2,
                                             fill="#ff3333", width=1, dash=(4, 4))
                self.zoom_canvas.create_oval(z_w // 2 - 5, z_h // 2 - 5,
                                             z_w // 2 + 5, z_h // 2 + 5,
                                             outline="#ff3333", width=2)
        self.lbl_corner_sub.config(text=f"X: {cx}, Y: {cy}")

        if self.inspector_axis == 'h':
            if self.active_corner_idx in [0, 1]:
                p_curr, p_next = self.pts[0], self.pts[1]
                edge_label = "Top Side"
            else:
                p_curr, p_next = self.pts[2], self.pts[3]
                edge_label = "Bottom Side"
        else:
            if self.active_corner_idx in [0, 3]:
                p_curr, p_next = self.pts[3], self.pts[0]
                edge_label = "Left Side"
            else:
                p_curr, p_next = self.pts[1], self.pts[2]
                edge_label = "Right Side"

        mx = int((p_curr[0] + p_next[0]) / 2.0)
        my = int((p_curr[1] + p_next[1]) / 2.0)
        ex1 = max(0, mx - box_size // 2)
        ey1 = max(0, my - box_size // 2)
        ex2 = min(self.w_rot, mx + box_size // 2)
        ey2 = min(self.h_rot, my + box_size // 2)
        edge_cache_key = (ex1, ey1, ex2, ey2, z_w, z_h)
        if self._edge_zoom_cache_key != edge_cache_key:
            edge_crop = self.img_rgb[ey1:ey2, ex1:ex2]
            if edge_crop.size > 0:
                edge_zoomed = cv2.resize(edge_crop, (z_w, z_h), interpolation=cv2.INTER_NEAREST)
                self.zoom_edge_tk_img = ImageTk.PhotoImage(Image.fromarray(edge_zoomed))
                self._edge_zoom_cache_key = edge_cache_key
                self.zoom_edge_canvas.delete("all")
                self.zoom_edge_canvas.create_image(0, 0, anchor=tk.NW, image=self.zoom_edge_tk_img)
                self.zoom_edge_canvas.create_line(z_w // 2, 0, z_w // 2, z_h,
                                                  fill="#00ffcc", width=2)
                self.zoom_edge_canvas.create_line(0, z_h // 2, z_w, z_h // 2,
                                                  fill="#00ffcc", width=2)
                self.zoom_edge_canvas.create_oval(z_w // 2 - 4, z_h // 2 - 4,
                                                  z_w // 2 + 4, z_h // 2 + 4,
                                                  outline="#00ffcc", width=2)
        self.lbl_edge_title.config(text=edge_label)

    def update_views(self):
        self.update_full_preview()
        self.update_corner_zoom()

    def on_full_click(self, event):
        for i, pt in enumerate(self.pts):
            sx = pt[0] * self.scale_full + self.offset_x
            sy = pt[1] * self.scale_full + self.offset_y
            if abs(event.x - sx) < 30 and abs(event.y - sy) < 30:
                self.dragging_item = i
                self.drag_start_pts = np.copy(self.pts)
                self.drag_start_mouse_x = event.x
                self.drag_start_mouse_y = event.y
                self.select_corner(i)
                return
        dists = [np.hypot((pt[0] * self.scale_full + self.offset_x) - event.x,
                          (pt[1] * self.scale_full + self.offset_y) - event.y)
                 for pt in self.pts]
        closest = int(np.argmin(dists))
        if dists[closest] < 60:
            self.dragging_item = closest
            self.drag_start_pts = np.copy(self.pts)
            self.drag_start_mouse_x = event.x
            self.drag_start_mouse_y = event.y
            self.select_corner(closest)

    def on_full_drag(self, event):
        if self.dragging_item is None:
            return
        dx_px = (event.x - self.drag_start_mouse_x) / self.scale_full
        dy_px = (event.y - self.drag_start_mouse_y) / self.scale_full
        self.pts = np.copy(self.drag_start_pts)
        self._move_pts_keeping_square(dx_px, dy_px)
        self.schedule_view_update()

    def on_full_release(self, event):
        self.dragging_item = None
        self.drag_start_pts = None

    def on_zoom_click(self, event):
        self.last_zoom_x = event.x
        self.last_zoom_y = event.y

    def on_zoom_drag(self, event):
        dx = (event.x - self.last_zoom_x) / 8.0
        dy = (event.y - self.last_zoom_y) / 8.0
        self._move_pts_keeping_square(dx, dy)
        self.last_zoom_x = event.x
        self.last_zoom_y = event.y
        self.schedule_view_update()

    def confirm(self):
        if self.on_confirm is not None:
            self.on_confirm(self)

    def skip(self):
        if self.on_skip is not None:
            self.on_skip(self)


# ---------------------------------------------------------------------------
#  Output formats window
# ---------------------------------------------------------------------------

class FormatSettingsDialog(tk.Toplevel):
    """Output formats window. After wait_window(): .result is the new settings dict, or None if cancelled."""
    BG, PANEL, MUTED = "#0d1116", "#161b22", "#8b949e"
    TABS = [("general", "General"), ("metadata", "Metadata"),
            ("jpeg", "JPEG"), ("tiff", "TIFF"), ("png", "PNG")]

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Output Formats")
        self.geometry("1024x834")
        self.transient(parent)
        self.grab_set()
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.result = None
        self.is_closing = False
        self._toggles = []   # (button, variable)
        self._choices = []   # frames of radio-like buttons
        self._sliders = []   # dicts: scale, label, var, fn
        self.ctrls = {"jpeg": [], "tiff": [], "png": []}  # (kind, widget, supported)
        # (Force DPI is now an LED indicator inside the DPI panel, not a toggle button)
        self.meta_entries = {}  # field key -> tk.Entry (manual metadata)
        self._create_vars()

        main = tk.Frame(self, bg=self.BG, padx=20, pady=14)
        main.pack(fill=tk.BOTH, expand=True)
        tk.Label(main, text="OUTPUT FORMATS", font=("Inter", 22, "bold"),
                 bg=self.BG, fg="#ffffff").pack(pady=(0, 8))

        tabs = tk.Frame(main, bg=self.BG)
        tabs.pack(fill=tk.X, pady=(0, 6))
        self.tab_buttons = {}
        for i, (key, label) in enumerate(self.TABS):
            tabs.grid_columnconfigure(i, weight=1, uniform="tabs")
            b = SegmentedButton(tabs, label, None, font=("Inter", 20, "bold"), height=52)
            b.command = (lambda k=key: self.show_page(k))
            b.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0))
            self.tab_buttons[key] = b

        self.page_host = tk.Frame(main, bg=self.BG)
        self.page_host.pack(fill=tk.BOTH, expand=True)
        self.pages = {}
        self._build_general()
        self._build_metadata()
        self._build_jpeg()
        self._build_tiff()
        self._build_png()

        btns = tk.Frame(main, bg=self.BG)
        btns.pack(fill=tk.X, pady=(10, 0))
        tk.Button(btns, text="Save", command=self.save, bg="#ffffff", fg="#0d1116",
                  font=("Inter", 22, "bold"), bd=1, relief="solid", highlightthickness=0,
                  padx=20, pady=12).pack(side=tk.LEFT, padx=5)
        tk.Button(btns, text="Defaults", command=self.reset_defaults, bg="#21262d", fg="#ffffff",
                  font=("Inter", 22, "bold"), bd=1, relief="solid", highlightthickness=0,
                  padx=20, pady=12).pack(side=tk.LEFT, padx=5)
        tk.Button(btns, text="Cancel (Esc)", command=self.cancel, bg="#21262d", fg="#ffffff",
                  font=("Inter", 22, "bold"), bd=1, relief="solid", highlightthickness=0,
                  padx=20, pady=12).pack(side=tk.RIGHT, padx=5)
        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Control-w>", lambda e: self.cancel())

        self._set_vars(get_settings())
        self.show_page("general")
        self._redraw_all()

    # ----- variables -----------------------------------------------------

    def _create_vars(self):
        B, I, S = tk.BooleanVar, tk.IntVar, tk.StringVar
        self.v = {
            "same_as_original": B(), "out_jpeg": B(), "out_tiff": B(), "out_png": B(), "interp": S(),
            "j_asorig": B(), "j_quality": I(), "j_sub": S(), "j_prog": B(), "j_opt": B(),
            "j_meta_exif": B(), "j_meta_xmp": B(),
            "j_dpi_force": B(), "j_dpi_auto": B(), "j_dpi": I(), "j_dpi_unit": S(),
            "t_asorig": B(), "t_comp": I(), "t_bits": S(),
            "t_meta_exif": B(), "t_meta_xmp": B(),
            "t_dpi_force": B(), "t_dpi_auto": B(), "t_dpi": I(), "t_dpi_unit": S(),
            "p_asorig": B(), "p_level": I(), "p_bits": S(),
            "p_meta_exif": B(), "p_meta_xmp": B(),
            "p_dpi_force": B(), "p_dpi_auto": B(), "p_dpi": I(), "p_dpi_unit": S(),
        }
        for k in MANUAL_FIELDS:
            self.v["manual_" + k] = S()

    def _set_vars(self, s):
        v = self.v
        v["same_as_original"].set(s["same_as_original"])
        v["out_jpeg"].set(s["outputs"]["jpeg"])
        v["out_tiff"].set(s["outputs"]["tiff"])
        v["out_png"].set(s["outputs"]["png"])
        v["interp"].set(s["interpolation"])
        j, t, p = s["jpeg"], s["tiff"], s["png"]
        v["j_asorig"].set(j["as_original"])
        v["j_quality"].set(j["quality"])
        v["j_sub"].set(j["subsampling"])
        v["j_prog"].set(j["progressive"])
        v["j_opt"].set(j["optimize"])
        v["j_meta_exif"].set(j.get("metadata_exif", True))
        v["j_meta_xmp"].set(j.get("metadata_xmp", True))
        v["t_asorig"].set(t["as_original"])
        v["t_comp"].set(TIFF_KEYS.index(t["compression"]))
        v["t_bits"].set(t["bits"])
        v["t_meta_exif"].set(t.get("metadata_exif", True))
        v["t_meta_xmp"].set(t.get("metadata_xmp", True))
        v["p_asorig"].set(p["as_original"])
        v["p_level"].set(p["level"])
        v["p_bits"].set(p["bits"])
        v["p_meta_exif"].set(p.get("metadata_exif", True))
        v["p_meta_xmp"].set(p.get("metadata_xmp", True))
        v["j_dpi_force"].set(j.get("dpi_force", False))
        v["j_dpi_auto"].set(j.get("dpi_auto", True))
        v["j_dpi"].set(j.get("dpi_value", 300))
        v["j_dpi_unit"].set(j.get("dpi_unit", "in"))
        v["t_dpi_force"].set(t.get("dpi_force", False))
        v["t_dpi_auto"].set(t.get("dpi_auto", True))
        v["t_dpi"].set(t.get("dpi_value", 300))
        v["t_dpi_unit"].set(t.get("dpi_unit", "in"))
        v["p_dpi_force"].set(p.get("dpi_force", False))
        v["p_dpi_auto"].set(p.get("dpi_auto", True))
        v["p_dpi"].set(p.get("dpi_value", 300))
        v["p_dpi_unit"].set(p.get("dpi_unit", "in"))
        man = s.get("manual", {})
        for k in MANUAL_FIELDS:
            v["manual_" + k].set(man.get(k, "") if isinstance(man.get(k), str) else "")

    def _collect(self):
        v = self.v
        return sanitize_settings({
            "same_as_original": v["same_as_original"].get(),
            "outputs": {"jpeg": v["out_jpeg"].get(), "tiff": v["out_tiff"].get(), "png": v["out_png"].get()},
            "interpolation": v["interp"].get(),
            "jpeg": {"as_original": v["j_asorig"].get(), "quality": v["j_quality"].get(),
                     "subsampling": v["j_sub"].get(), "progressive": v["j_prog"].get(),
                     "optimize": v["j_opt"].get(),
                     "metadata_exif": v["j_meta_exif"].get(),
                     "metadata_xmp": v["j_meta_xmp"].get(),
                     "dpi_force": v["j_dpi_force"].get(), "dpi_auto": v["j_dpi_auto"].get(),
                     "dpi_value": v["j_dpi"].get(),
                     "dpi_unit": v["j_dpi_unit"].get()},
            "tiff": {"as_original": v["t_asorig"].get(), "compression": TIFF_KEYS[v["t_comp"].get()],
                     "bits": v["t_bits"].get(),
                     "metadata_exif": v["t_meta_exif"].get(),
                     "metadata_xmp": v["t_meta_xmp"].get(),
                     "dpi_force": v["t_dpi_force"].get(), "dpi_auto": v["t_dpi_auto"].get(),
                     "dpi_value": v["t_dpi"].get(),
                     "dpi_unit": v["t_dpi_unit"].get()},
            "png": {"as_original": v["p_asorig"].get(), "level": v["p_level"].get(),
                    "bits": v["p_bits"].get(),
                    "metadata_exif": v["p_meta_exif"].get(),
                    "metadata_xmp": v["p_meta_xmp"].get(),
                    "dpi_force": v["p_dpi_force"].get(), "dpi_auto": v["p_dpi_auto"].get(),
                    "dpi_value": v["p_dpi"].get(),
                    "dpi_unit": v["p_dpi_unit"].get()},
            "manual": {k: v["manual_" + k].get() for k in MANUAL_FIELDS},
        })

    # ----- small widget builders ----------------------------------------

    def _section(self, parent, title):
        box = tk.Frame(parent, bg=self.PANEL, bd=1, relief="solid", padx=14, pady=8)
        box.pack(fill=tk.X, pady=5)
        tk.Label(box, text=title, font=("Inter", 17, "bold"), bg=self.PANEL,
                 fg=self.MUTED, anchor="w").pack(fill=tk.X)
        return box

    def _hint(self, parent, text):
        tk.Label(parent, text=text, font=("Inter", 13), bg=self.PANEL, fg=self.MUTED,
                 anchor="w", justify="left", wraplength=900).pack(fill=tk.X, pady=(2, 0))

    def _toggle(self, parent, text, var, command=None, font=("Inter", 18, "bold")):
        """A button that behaves like a check box. Calls self._normalize_outputs when var is an output flag."""
        btn = SegmentedButton(parent, text, None, font=font, height=50)
        btn.enabled = True
        is_output_flag = var in (self.v["same_as_original"], self.v["out_jpeg"],
                                 self.v["out_tiff"], self.v["out_png"])

        def flip():
            # enabled is checked inside SegmentedButton._on_release, so no need to check it here
            var.set(not var.get())
            if is_output_flag:
                self._normalize_outputs(var)
                self._redraw_all()
            else:
                btn.draw(var.get())
            if command:
                command()
        btn.command = flip
        self._toggles.append((btn, var))
        return btn

    def _normalize_outputs(self, changed_var):
        """Output selection rules:
        - 'Same as original' and the explicit formats (JPEG/TIFF/PNG) are mutually exclusive.
        - At least one output must stay selected.
        """
        v = self.v
        same = v["same_as_original"]
        formats = [v["out_jpeg"], v["out_tiff"], v["out_png"]]

        # If the user just turned ON "Same as original", turn OFF all formats.
        if changed_var is same and same.get():
            for f in formats:
                f.set(False)
            return

        # If the user just turned ON a format, turn OFF "Same as original".
        if changed_var in formats and changed_var.get():
            same.set(False)
            return

        # If the user turned OFF "Same as original", turn ON JPEG (default).
        if changed_var is same and not same.get():
            if not any(f.get() for f in formats):
                v["out_jpeg"].set(True)
            return

        # If the user turned OFF a format, make sure something is still active.
        if changed_var in formats and not changed_var.get():
            if not same.get() and not any(f.get() for f in formats):
                same.set(True)
            return

    def _choice(self, parent, options, var, command=None):
        """Row of buttons acting as radio buttons. options = [(value, label), ...]."""
        frame = tk.Frame(parent, bg=self.PANEL)
        frame.enabled = True
        frame.buttons = []

        def refresh():
            for val, b in frame.buttons:
                b.draw(var.get() == val)

        def pick(val):
            if not frame.enabled:
                return
            var.set(val)
            refresh()
            if command:
                command()
        for i, (val, label) in enumerate(options):
            frame.grid_columnconfigure(i, weight=1, uniform="choice")
            b = SegmentedButton(frame, label, None, font=("Inter", 18, "bold"), height=50)
            b.command = (lambda v=val: pick(v))
            b.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0))
            frame.buttons.append((val, b))
        frame.refresh = refresh
        self._choices.append(frame)
        return frame

    def _slider(self, parent, title, var, lo, hi, label_fn):
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill=tk.X, pady=4)
        tk.Label(row, text=title, font=("Inter", 18, "bold"), bg=self.PANEL, fg="#ffffff",
                 width=12, anchor="w").pack(side=tk.LEFT)
        lbl = tk.Label(row, font=("Inter", 22, "bold"), bg=self.PANEL, fg="#ffffff", width=11, anchor="e")
        lbl.pack(side=tk.RIGHT)
        scale = tk.Scale(row, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var, showvalue=False,
                         resolution=1, width=36, sliderlength=80, bd=0, relief="flat",
                         highlightthickness=0, takefocus=0, bg="#e6edf3", activebackground="#ffffff",
                         troughcolor="#30363d",
                         command=lambda _v=None: lbl.config(text=label_fn(var.get())))
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        item = {"scale": scale, "label": lbl, "var": var, "fn": label_fn}
        self._sliders.append(item)
        return item

    def _dpi_panel(self, parent, fam):
        """DPI panel with Force on the first row and presets on the second. fam in ('jpeg','tiff','png')."""
        box = tk.Frame(parent, bg=self.PANEL, bd=1, relief="solid", padx=14, pady=8)
        box.pack(fill=tk.X, pady=5)

        var_dpi = self.v[f"{fam[0]}_dpi"]
        var_unit = self.v[f"{fam[0]}_dpi_unit"]
        var_force = self.v[f"{fam[0]}_dpi_force"]

        # Row 1: DPI + entry + unit + Force
        r1 = tk.Frame(box, bg=self.PANEL)
        r1.pack(fill=tk.X, pady=(0, 6))
        tk.Label(r1, text="DPI", font=("Inter", 18, "bold"), bg=self.PANEL,
                 fg="#ffffff", anchor="w").pack(side=tk.LEFT, padx=(12, 14))

        # NOTE: the entry is NOT bound to var_dpi with textvariable, because it must show
        # the text "Auto" (not a number). The value is written by _dpi_refresh_family and
        # read back into var_dpi when the user types (see the bind below).
        entry = tk.Entry(r1, font=("Inter", 18, "bold"),
                         bg="#161b22", fg="#ffffff", insertbackground="#ffffff",
                         readonlybackground="#161b22", disabledbackground="#161b22",
                         disabledforeground="#6e7681",
                         bd=1, relief="solid", highlightthickness=0, width=7,
                         justify="center")
        entry.pack(side=tk.LEFT, padx=(0, 12))

        def _on_entry_typed(event=None):
            # Only when the block is editable and not in Auto mode
            if not _entry_state["enabled"]:
                return
            if var_auto.get():
                return
            txt = entry.get().strip()
            try:
                val = int(txt)
            except ValueError:
                return  # invalid input: leave var_dpi as it is
            if 1 <= val <= 100000:
                var_dpi.set(val)
                self._dpi_refresh_family(fam)
        _entry_state = {"enabled": True}
        entry.bind("<FocusOut>", _on_entry_typed)
        entry.bind("<Return>", _on_entry_typed)

        unit_menu = tk.OptionMenu(r1, var_unit, "in", "cm")
        unit_menu.config(font=("Inter", 15, "bold"), bg="#21262d", fg="#ffffff",
                         activebackground="#30363d", activeforeground="#ffffff",
                         bd=1, relief="solid", highlightthickness=0, width=4)
        unit_menu["menu"].config(font=("Inter", 14), bg="#21262d", fg="#ffffff")
        unit_menu.pack(side=tk.LEFT, padx=(0, 18))

        # Force DPI is now a pure indicator (LED + label): no longer clickable.
        led_frame = tk.Frame(r1, bg=self.PANEL)
        led_frame.pack(side=tk.LEFT, padx=(8, 0))

        led = tk.Canvas(led_frame, width=20, height=20, bg=self.PANEL,
                        highlightthickness=0, bd=0)
        led.pack(side=tk.LEFT, padx=(0, 6))
        led_circle = led.create_oval(3, 3, 17, 17, fill="#21262d", outline="#30363d", width=1)

        lbl_force = tk.Label(led_frame, text="Force DPI", font=("Inter", 18, "bold"),
                             bg=self.PANEL, fg="#ffffff", anchor="w")
        lbl_force.pack(side=tk.LEFT)

        # Row 2: preset buttons (SegmentedButton, same look as the rest of the app)
        # The first button is "Auto": the DPI is inherited from the original, the entry shows "Auto".
        r2 = tk.Frame(box, bg=self.PANEL)
        r2.pack(fill=tk.X)
        tk.Label(r2, text="Preset", font=("Inter", 15, "bold"), bg=self.PANEL,
                 fg="#8b949e", anchor="w").pack(side=tk.LEFT, padx=(12, 14))

        var_auto = self.v[f"{fam[0]}_dpi_auto"]

        def on_auto():
            if not btn_auto.enabled:
                return
            var_auto.set(True)
            self._dpi_refresh_family(fam)

        def on_preset(p, btn):
            if not btn.enabled:
                return
            var_auto.set(False)
            var_dpi.set(p)
            self._dpi_refresh_family(fam)

        btn_auto = SegmentedButton(r2, text="Auto",
                                   command=on_auto,
                                   font=("Inter", 18, "bold"),
                                   width=80, height=46)
        btn_auto.pack(side=tk.LEFT, padx=3)

        btn_presets_enabled = [True]  # flag used by on_preset to check if the block is enabled

        preset_buttons = []
        for preset in DPI_PRESETS:
            b = SegmentedButton(r2, text=str(preset),
                                command=None,
                                font=("Inter", 18, "bold"),
                                width=80, height=46)
            b.command = (lambda p=preset, bb=b: on_preset(p, bb))
            b.pack(side=tk.LEFT, padx=3)
            preset_buttons.append(b)

        self._hint(box, "Auto: keep the DPI of the original scan. Any other value is written to the "
                        "file (Force DPI): choose it when you know the scan resolution.")

        # Store all the DPI widgets so _refresh_states can enable/disable them
        # (entry, unit_menu, preset_buttons, led_canvas, led_circle, lbl_force, btn_auto,
        #  btn_presets_enabled, _entry_state)
        self.ctrls[fam].append(("dpi_widgets_v2",
                                (entry, unit_menu, preset_buttons, led, led_circle,
                                 lbl_force, btn_auto, btn_presets_enabled, _entry_state), True))

        # Initial visual state (Auto on/off, entry text, preset highlight)
        self._dpi_refresh_family(fam)

        return box

    def _dpi_refresh_family(self, fam):
        """Single point that updates the DPI widgets for one format.
        Handles entry text/colours, Auto button colours and preset highlight.
        When the whole block is disabled (As original ON), the colours are the "disabled" ones,
        so the widgets look the same as the other disabled controls of the app."""
        var_auto = self.v[f"{fam[0]}_dpi_auto"]
        var_dpi = self.v[f"{fam[0]}_dpi"]
        entry = unit_menu = None
        preset_buttons = None
        btn_auto = None
        entry_state = None
        for kind, obj, _sup in self.ctrls[fam]:
            if kind == "dpi_widgets_v2":
                entry, unit_menu, preset_buttons, led, led_circle, lbl_force, btn_auto, _flag, entry_state = obj
                break
        if entry is None:
            return

        is_auto = bool(var_auto.get())
        enabled = bool(entry_state.get("enabled", True))

        # Entry text (the enabled state is respected: a disabled block stays disabled)
        entry.configure(state="normal")
        entry.delete(0, tk.END)
        if is_auto:
            entry.insert(0, "Auto")
        else:
            try:
                entry.insert(0, str(int(var_dpi.get())))
            except Exception:
                entry.insert(0, "")
        if not enabled:
            entry.configure(state=tk.DISABLED)
        elif is_auto:
            entry.configure(state="readonly")
        else:
            entry.configure(state="normal")

        # Entry colours: grey text when the block is disabled, white otherwise
        if not enabled:
            entry.configure(fg="#6e7681", disabledforeground="#6e7681")
        else:
            entry.configure(fg="#ffffff")

        # Auto button colours: three cases (disabled, enabled+selected, enabled+unselected)
        if not enabled:
            btn_auto.bg_off, btn_auto.fg_off = "#161b22", "#6e7681"
            btn_auto.bg_on,  btn_auto.fg_on  = "#30363d", "#8b949e"
            btn_auto.draw(is_auto)
        elif is_auto:
            btn_auto.bg_on, btn_auto.fg_on = "#ffffff", "#30363d"
            btn_auto.draw(True)
        else:
            btn_auto.bg_off, btn_auto.fg_off = "#21262d", "#ffffff"
            btn_auto.draw(False)

        # LED: lit only when the DPI will actually be written (block editable AND not Auto).
        # With Auto ON the file keeps the original scan's DPI, so nothing is forced.
        if enabled and not is_auto:
            led.itemconfigure(led_circle, fill="#00ffcc", outline="#00ffcc")
            lbl_force.config(fg="#ffffff")
        elif not enabled:
            led.itemconfigure(led_circle, fill="#21262d", outline="#30363d")
            lbl_force.config(fg="#6e7681")
        else:
            led.itemconfigure(led_circle, fill="#21262d", outline="#30363d")
            lbl_force.config(fg="#8b949e")

        # Preset highlight (only when not in Auto mode)
        try:
            current = int(var_dpi.get())
        except Exception:
            current = None
        for b, p in zip(preset_buttons, DPI_PRESETS):
            if not enabled:
                b.bg_off, b.fg_off = "#161b22", "#6e7681"
                b.bg_on,  b.fg_on  = "#30363d", "#8b949e"
                b.draw(False)
            else:
                self._style_seg(b, True)
                b.draw((not is_auto) and current == p)

    def _new_scrollable_page(self, key):
        page = tk.Frame(self.page_host, bg=self.BG)
        self.pages[key] = page
        canvas = tk.Canvas(page, bg=self.BG, bd=0, highlightthickness=0)
        vscroll = tk.Scrollbar(page, orient="vertical", command=canvas.yview, width=20, bg="#161b22", troughcolor="#161b22", activebackground="#30363d", bd=0, highlightthickness=0)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        inner = tk.Frame(canvas, bg=self.BG)
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        
        # Aggiorna la regione di scorrimento quando il contenuto cambia
        def _on_configure(e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Calcola il fattore di conversione tra pixel del contenuto e frazione di scroll
            canvas.update_idletasks()
            total_h = inner.winfo_height()
            canvas_h = canvas.winfo_height()
            if total_h > canvas_h:
                canvas._scroll_factor = total_h / canvas_h
            else:
                canvas._scroll_factor = 1.0

        inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(canvas_window, width=e.width), _on_configure()))

        # Stato per il drag assoluto
        drag_state = {"start_y": 0, "start_view": 0.0, "active": False}

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_touch_scroll(event):
            if event.num == 4: canvas.yview_scroll(-1, "units")
            elif event.num == 5: canvas.yview_scroll(1, "units")

        def _on_press(event):
            drag_state["start_y"] = event.y
            # Salva la posizione attuale dello scroll in frazione (0.0 - 1.0)
            drag_state["start_view"] = canvas.yview()[0]
            drag_state["active"] = True

        def _on_drag(event):
            if not drag_state["active"]: return
            
            # Calcola di quanti pixel si e' spostato il dito dall'inizio del drag
            dy = drag_state["start_y"] - event.y
            
            # Converti i pixel in frazione di scorrimento (in base all'altezza totale)
            # Se ti muovi di 'dy' pixel, la frazione sara' dy / altezza_totale_del_contenuto
            canvas_h = canvas.winfo_height()
            total_h = inner.winfo_height()
            
            if total_h > 0:
                # Calcoliamo la frazione: usiamo l'altezza del canvas come riferimento per la velocita'
                # (1 pixel di dito = 1 pixel di contenuto, ma in frazione)
                delta_fraction = dy / total_h
                
                new_view = drag_state["start_view"] + delta_fraction
                # Limita tra 0 e 1
                new_view = max(0.0, min(1.0, new_view))
                
                # Applica la posizione
                canvas.yview_moveto(new_view)

        def _on_release(event):
            drag_state["active"] = False

        def _bind_scroll(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_touch_scroll)
            canvas.bind_all("<Button-5>", _on_touch_scroll)
            canvas.bind_all("<ButtonPress-1>", _on_press, add="+")
            canvas.bind_all("<B1-Motion>", _on_drag, add="+")
            canvas.bind_all("<ButtonRelease-1>", _on_release, add="+")

        def _unbind_scroll(event):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")
            canvas.unbind_all("<ButtonPress-1>")
            canvas.unbind_all("<B1-Motion>")
            canvas.unbind_all("<ButtonRelease-1>")

        canvas.bind("<Enter>", _bind_scroll)
        canvas.bind("<Leave>", _unbind_scroll)
        
        # Esegui un primo calcolo della scrollregion
        canvas.after(100, _on_configure)
        return inner

    def _new_page(self, key):
        page = tk.Frame(self.page_host, bg=self.BG)
        self.pages[key] = page
        return page

    # ----- pages ---------------------------------------------------------

    def _build_general(self):
        page = self._new_page("general")
        box = self._section(page, "Save these formats together")
        grid = tk.Frame(box, bg=self.PANEL)
        grid.pack(fill=tk.X, pady=(6, 2))
        items = [("same_as_original", "Same as original"), ("out_jpeg", "JPEG"),
                 ("out_tiff", "TIFF"), ("out_png", "PNG")]
        for i, (key, label) in enumerate(items):
            grid.grid_columnconfigure(i, weight=1, uniform="outs")
            self._toggle(grid, label, self.v[key]).grid(row=0, column=i, sticky="nsew",
                                                        padx=(0 if i == 0 else 4, 0))
        self._hint(box, "Select one or more. \"Same as original\" keeps each file's own format. "
                        "Every format uses the settings of its own tab.")

        box = self._section(page, "Straightening (rotation)")
        self._choice(box, [("linear", "Linear"), ("cubic", "Bicubic"), ("lanczos", "Lanczos")],
                     self.v["interp"]).pack(fill=tk.X, pady=(6, 2))
        self._hint(box, "Only used when the angle is not 0\u00b0: at exactly 0\u00b0 the pixels are never resampled. "
                        "Bicubic is the standard choice; Lanczos is sharper but slower.")

        box = self._section(page, "Metadata")
        tk.Button(box, text="\u270E  Edit metadata", command=self.open_metadata_tab,
                  bg="#21262d", fg="#ffffff", font=("Inter", 18, "bold"), bd=1,
                  relief="solid", highlightthickness=0, padx=14, pady=8,
                  activebackground="#30363d", activeforeground="#ffffff"
                  ).pack(fill=tk.X, pady=(6, 4))
        if EXIFTOOL:
            self._hint(box, "exiftool found: EXIF, DPI and ICC profile can be copied from the original scan "
                            "(see the option in each format tab).")
        else:
            self._hint(box, "exiftool NOT found: metadata cannot be copied.\n"
                            "Install it with: sudo apt install libimage-exiftool-perl")
        
        info_row = tk.Frame(page, bg=self.BG)
        info_row.pack(fill=tk.X, pady=(15, 10))
        tk.Button(info_row, text="\u24d8", command=self._show_about, 
                  font=("Inter", 32, "bold"), bg="#0d1116", fg="#ffffff", 
                  bd=0, highlightthickness=0, padx=20, activebackground="#0d1116", activeforeground="#8b949e").pack(side=tk.RIGHT)

    def _show_about(self):
        AboutDialog(self)

    def open_metadata_tab(self):
        """Apre la tab Metadata dall'interno della stessa finestra."""
        self.show_page("metadata")

    # ----- tab Metadata --------------------------------------------------

    def _meta_field(self, parent, key, label):
        """Crea una riga con etichetta + entry per un campo manuale."""
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill=tk.X, pady=3)
        tk.Label(row, text=label, font=("Inter", 16, "bold"), bg=self.PANEL, fg="#ffffff",
                 width=22, anchor="w").pack(side=tk.LEFT, padx=(6, 8))
        entry = tk.Entry(row, textvariable=self.v["manual_" + key],
                         font=("Inter", 16, "bold"), bg="#161b22", fg="#ffffff",
                         insertbackground="#ffffff", bd=1, relief="solid",
                         highlightthickness=0)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.meta_entries[key] = entry
        return row

    def _meta_date_field(self, parent, key, label):
        """Date field: entry + 'Today' button that inserts DD/MM/YYYY HH:MM:SS."""
        row = self._meta_field(parent, key, label)
        tk.Button(row, text="Today", font=("Inter", 14, "bold"),
                  bg="#21262d", fg="#ffffff", bd=1, relief="solid",
                  highlightthickness=0, padx=10,
                  command=lambda k=key: self._set_today(k)
                  ).pack(side=tk.LEFT)
        return row

    def _set_today(self, key):
        import datetime
        now = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.v["manual_" + key].set(now)

    def _build_metadata(self):
        inner = self._new_scrollable_page("metadata")
        
        box1 = self._section(inner, "Shooting data")
        self._meta_field(box1, "make", "Make")
        self._meta_field(box1, "model", "Model")
        self._meta_field(box1, "iso", "ISO")
        self._meta_field(box1, "aperture", "Aperture (f/)")
        self._meta_field(box1, "shutter", "Shutter (s)")
        self._meta_field(box1, "focal", "Focal (mm)")
        self._meta_field(box1, "film_type", "Film")
        self._meta_field(box1, "paper_type", "Paper")
        self._hint(box1, "Fill in only the fields you want to write: empty fields are ignored. The values you type override those copied from the original file. Film and Paper are written to EXIF UserComment and XMP Description as Film: X | Paper: Y (only the filled parts are written).")
        
        box2 = self._section(inner, "Date")
        self._meta_date_field(box2, "datetime_original", "Date taken")
        self._meta_date_field(box2, "datetime_digitized", "Date digitized")
        self._hint(box2, "Format: DD/MM/YYYY HH:MM:SS (e.g. 15/08/2024 14:30:00). It is converted automatically to the standard EXIF format YYYY:MM:DD. The Today button inserts the current date and time.")
        
        box3 = self._section(inner, "Author / Copyright")
        self._meta_field(box3, "artist", "Author")
        self._meta_field(box3, "copyright", "Copyright")
        self._meta_field(box3, "software", "Software (scanner)")
        
        box4 = self._section(inner, "GPS (optional)")
        self._meta_field(box4, "gps_lat", "Latitude (decimal)")
        self._meta_field(box4, "gps_lon", "Longitude (decimal)")
        self._meta_field(box4, "gps_alt", "Altitude (m)")
        self._hint(box4, "Example: 44.4949 and 11.3426 for Bologna. Use negative values for South/West. Altitude is in metres.")

    def _build_jpeg(self):
        page = self._new_scrollable_page("jpeg")
        box = self._section(page, "Compression")
        c = self.ctrls["jpeg"]
        self._toggle(box, "As original", self.v["j_asorig"],
                     command=self._refresh_states).pack(fill=tk.X, pady=(6, 4))

        q_row = tk.Frame(box, bg=self.PANEL)
        q_row.pack(fill=tk.X, pady=4)
        tk.Label(q_row, text="Quality", font=("Inter", 18, "bold"), bg=self.PANEL,
                 fg="#ffffff", anchor="w").pack(side=tk.LEFT, padx=(0, 6))
        q_minus = tk.Button(q_row, text="\u2212", font=("Inter", 16, "bold"),
                            bg="#21262d", fg="#ffffff", bd=1, relief="solid",
                            highlightthickness=0, width=3,
                            command=lambda: self._jpeg_quality_step(-1))
        q_minus.pack(side=tk.LEFT, padx=(0, 2))
        q_lbl = tk.Label(q_row, font=("Inter", 22, "bold"), bg=self.PANEL, fg="#ffffff",
                         width=5, anchor="center")
        q_lbl.pack(side=tk.LEFT)
        q_plus = tk.Button(q_row, text="+", font=("Inter", 16, "bold"),
                           bg="#21262d", fg="#ffffff", bd=1, relief="solid",
                           highlightthickness=0, width=3,
                           command=lambda: self._jpeg_quality_step(1))
        q_plus.pack(side=tk.LEFT, padx=(2, 6))
        q_scale = tk.Scale(q_row, from_=JPEG_MIN_QUALITY, to=100, orient=tk.HORIZONTAL,
                           variable=self.v["j_quality"], showvalue=False, resolution=1,
                           width=36, sliderlength=80, bd=0, relief="flat",
                           highlightthickness=0, takefocus=0, bg="#e6edf3",
                           activebackground="#ffffff", troughcolor="#30363d",
                           command=lambda _v=None: q_lbl.config(text=f"{int(self.v['j_quality'].get())} %"))
        q_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        q_lbl.config(text=f"{int(self.v['j_quality'].get())} %")
        item = {"scale": q_scale, "label": q_lbl, "var": self.v["j_quality"],
                "fn": lambda q: f"{int(q)} %"}
        self._sliders.append(item)
        c.append(("scale", item, True))
        self._jpeg_q_lbl = q_lbl
        self._jpeg_q_scale = q_scale
        self._jpeg_q_minus = q_minus
        self._jpeg_q_plus = q_plus
        tk.Label(box, text="Chroma subsampling", font=("Inter", 16, "bold"), bg=self.PANEL,
                 fg="#ffffff", anchor="w").pack(fill=tk.X, pady=(6, 2))
        grp = self._choice(box, [("444", "4:4:4 (best)"), ("422", "4:2:2"), ("420", "4:2:0 (smallest)")],
                           self.v["j_sub"])
        grp.pack(fill=tk.X)
        # Chroma subsampling is written through PIL, so it is always available
        # regardless of the OpenCV version.
        c.append(("group", grp, True))
        # Remember the chroma group so _refresh_states can disable it with "As original"
        self._chroma_group = grp
        row = tk.Frame(box, bg=self.PANEL)
        row.pack(fill=tk.X, pady=(6, 2))
        row.grid_columnconfigure(0, weight=1, uniform="jopt")
        row.grid_columnconfigure(1, weight=1, uniform="jopt")
        b1 = self._toggle(row, "Progressive", self.v["j_prog"])
        b1.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        b2 = self._toggle(row, "Optimize Huffman", self.v["j_opt"])
        b2.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        c.append(("seg", b1, True))
        c.append(("seg", b2, True))
        self._hint(box, "As original: for JPEG sources it uses the source's quality level and chroma "
                        "subsampling; for other sources the values shown here are used.")

        self._dpi_panel(page, "jpeg")

        box = self._section(page, "Metadata")
        meta_row = tk.Frame(box, bg=self.PANEL)
        meta_row.pack(fill=tk.X, pady=(6, 2))
        meta_row.grid_columnconfigure(0, weight=1, uniform="jmeta")
        meta_row.grid_columnconfigure(1, weight=1, uniform="jmeta")
        bj1 = self._toggle(meta_row, "Save EXIF + ICC", self.v["j_meta_exif"])
        bj1.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        bj2 = self._toggle(meta_row, "Save XMP", self.v["j_meta_xmp"])
        bj2.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

    def _build_tiff(self):
        page = self._new_page("tiff")
        box = self._section(page, "Compression")
        c = self.ctrls["tiff"]
        self._toggle(box, "As original", self.v["t_asorig"],
                     command=self._refresh_states).pack(fill=tk.X, pady=(6, 4))
        item = self._slider(box, "Method", self.v["t_comp"], 0, len(TIFF_KEYS) - 1,
                            lambda i: TIFF_LABELS[TIFF_KEYS[int(i)]])
        c.append(("scale", item, HAS_TIFF_COMPRESSION))
        hint = ("TIFF compression is lossless; the slider goes from no compression (largest, fastest) to "
                "Deflate (smallest). As original: uses the source TIFF's method when it can be read, "
                "otherwise the value shown.")
        if not HAS_TIFF_COMPRESSION:
            hint += "\nThis OpenCV version cannot set the TIFF compression: not available on this system."
        self._hint(box, hint)

        box = self._section(page, "Bit depth")
        t_bits_grp = self._choice(box, [("keep", "Keep original (8 / 16 bit)"), ("8", "Convert to 8-bit")],
                                  self.v["t_bits"])
        t_bits_grp.pack(fill=tk.X, pady=(6, 2))
        c.append(("group", t_bits_grp, True))
        self._dpi_panel(page, "tiff")
        box = self._section(page, "Metadata")
        meta_row = tk.Frame(box, bg=self.PANEL)
        meta_row.pack(fill=tk.X, pady=(6, 2))
        meta_row.grid_columnconfigure(0, weight=1, uniform="tmeta")
        meta_row.grid_columnconfigure(1, weight=1, uniform="tmeta")
        bt1 = self._toggle(meta_row, "Save EXIF + ICC", self.v["t_meta_exif"])
        bt1.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        bt2 = self._toggle(meta_row, "Save XMP", self.v["t_meta_xmp"])
        bt2.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

    def _build_png(self):
        page = self._new_page("png")
        box = self._section(page, "Compression")
        c = self.ctrls["png"]
        self._toggle(box, "As original", self.v["p_asorig"],
                     command=self._refresh_states).pack(fill=tk.X, pady=(6, 4))
        item = self._slider(box, "Level", self.v["p_level"], PNG_MIN_LEVEL, PNG_MAX_LEVEL,
                            lambda n: f"Level {int(n)}")
        c.append(("scale", item, True))
        self._hint(box, "PNG is lossless: the level only changes file size and saving time (1 = fastest, "
                        "9 = smallest). PNG files do not record their level, so As original uses the "
                        "value shown.")

        box = self._section(page, "Bit depth")
        p_bits_grp = self._choice(box, [("keep", "Keep original (8 / 16 bit)"), ("8", "Convert to 8-bit")],
                                  self.v["p_bits"])
        p_bits_grp.pack(fill=tk.X, pady=(6, 2))
        c.append(("group", p_bits_grp, True))
        self._dpi_panel(page, "png")
        box = self._section(page, "Metadata")
        meta_row = tk.Frame(box, bg=self.PANEL)
        meta_row.pack(fill=tk.X, pady=(6, 2))
        meta_row.grid_columnconfigure(0, weight=1, uniform="pmeta")
        meta_row.grid_columnconfigure(1, weight=1, uniform="pmeta")
        bp1 = self._toggle(meta_row, "Save EXIF + ICC", self.v["p_meta_exif"])
        bp1.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        bp2 = self._toggle(meta_row, "Save XMP", self.v["p_meta_xmp"])
        bp2.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

    # ----- look and state -----------------------------------------------

    def _style_seg(self, b, enabled):
        if enabled:
            b.bg_off, b.fg_off, b.bg_on, b.fg_on = "#21262d", "#ffffff", "#ffffff", "#0d1116"
        else:  # greyed out, but the value is still visible
            b.bg_off, b.fg_off, b.bg_on, b.fg_on = "#161b22", "#6e7681", "#30363d", "#8b949e"
        b.draw(b.is_selected)

    def _style_scale(self, item, enabled):
        if enabled:
            item["scale"].configure(state=tk.NORMAL, bg="#e6edf3", troughcolor="#30363d")
            item["label"].config(fg="#ffffff")
        else:
            item["scale"].configure(state=tk.DISABLED, bg="#6e7681", troughcolor="#21262d")
            item["label"].config(fg=self.MUTED)

    def _set_enabled(self, kind, obj, enabled):
        if kind == "seg":
            obj.enabled = enabled
            self._style_seg(obj, enabled)
        elif kind == "group":
            obj.enabled = enabled
            for _val, b in obj.buttons:
                self._style_seg(b, enabled)
        elif kind == "scale":
            self._style_scale(obj, enabled)
        elif kind == "dpi_widgets_v2":
            entry, unit_menu, preset_buttons, led, led_circle, lbl_force, btn_auto, flag, entry_state = obj
            if not enabled:
                entry.configure(state=tk.DISABLED)
                unit_menu.configure(state=tk.DISABLED)
                flag[0] = False
                entry_state["enabled"] = False
            else:
                unit_menu.configure(state=tk.NORMAL)
                flag[0] = True
                entry_state["enabled"] = True
                # entry state (normal/readonly) is set by _dpi_refresh_family
            for b in preset_buttons:
                b.enabled = enabled
                self._style_seg(b, enabled)
            btn_auto.enabled = enabled
            self._style_seg(btn_auto, enabled)
            # LED: white and bright when enabled, dark and dim when disabled
            if enabled:
                led.itemconfigure(led_circle, fill="#ffffff", outline="#ffffff")
                lbl_force.config(fg="#ffffff")
            else:
                led.itemconfigure(led_circle, fill="#21262d", outline="#30363d")
                lbl_force.config(fg="#6e7681")

    def _refresh_states(self):
        """'As original' greys out the compression controls of that format; their values stay visible.
        The DPI block follows the same rule: when 'As original' is ON, the DPI cannot be changed and
        the Force DPI LED is dark. When it is OFF, the DPI block becomes editable and the LED lights up.
        Bit depth is forced to "keep" when 'As original' is ON, to avoid losing information
        (a 16-bit scan accidentally saved as 8-bit would lose the higher bits)."""
        for fam, key in (("jpeg", "j_asorig"), ("tiff", "t_asorig"), ("png", "p_asorig")):
            editable = not self.v[key].get()
            # Keep dpi_force in sync with "As original" for this format.
            force_var = self.v[f"{fam[0]}_dpi_force"]
            force_var.set(editable)
            # Force bit depth to "keep" when "As original" is ON (TIFF and PNG only)
            if not editable:
                if fam == "tiff":
                    self.v["t_bits"].set("keep")
                elif fam == "png":
                    self.v["p_bits"].set("keep")
            for kind, obj, supported in self.ctrls[fam]:
                self._set_enabled(kind, obj, editable and supported)
            # entry / preset / auto button: refresh after the enable/disable pass
            self._dpi_refresh_family(fam)
        # Redraw the choice groups so the forced values are visible
        for frame in self._choices:
            frame.refresh()

    # (the old _style_force_buttons is gone: Force DPI is now a pure LED indicator)

    def _redraw_all(self):
        for btn, var in self._toggles:
            btn.draw(var.get())
        for frame in self._choices:
            frame.refresh()
        for item in self._sliders:
            item["label"].config(text=item["fn"](item["var"].get()))
        self._refresh_states()

    def show_page(self, key):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill=tk.BOTH, expand=True)
        for k, b in self.tab_buttons.items():
            b.draw(k == key)

    # ----- actions -------------------------------------------------------

    def _jpeg_quality_step(self, direction):
        """Increase/decrease the JPEG quality by 1."""
        try:
            cur = int(self.v["j_quality"].get())
            cur = max(JPEG_MIN_QUALITY, min(100, cur + direction))
            self.v["j_quality"].set(cur)
            if hasattr(self, "_jpeg_q_lbl"):
                self._jpeg_q_lbl.config(text=f"{cur} %")
        except Exception:
            pass

    def reset_defaults(self):
        self._set_vars(copy.deepcopy(DEFAULT_SETTINGS))
        self._redraw_all()

    def save(self):
        if self.is_closing:
            return
        s = self._collect()
        if not (s["same_as_original"] or any(s["outputs"].values())):
            messagebox.showwarning("Warning", "Select at least one output format.", parent=self)
            return
        self.result = s
        self._close()

    def cancel(self):
        if self.is_closing:
            return
        self.result = None
        self._close()

    def _close(self):
        self.is_closing = True
        self.after(50, self._finalize_destroy)

    def _finalize_destroy(self):
        drop = getattr(self, "_drop_wheel_bindings", None)
        if drop is not None:
            try:
                drop()
            except Exception:
                pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
#  Main application
# ---------------------------------------------------------------------------

class StyledMessageBox(tk.Toplevel):
    """A simple, dark-themed message box that matches the app style and auto-sizes."""
    def __init__(self, parent, title, message):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#0d1116")
        self.resizable(False, False)

        # Position the window BEFORE drawing widgets to avoid hitbox misalignment
        self.update_idletasks()
        self.geometry("+%d+%d" % (
            parent.winfo_rootx() + (parent.winfo_width() // 2) - 200,
            parent.winfo_rooty() + (parent.winfo_height() // 2) - 100
        ))

        outer = tk.Frame(self, bg="#0d1116", padx=50, pady=35)
        outer.pack(fill=tk.BOTH, expand=True)

        tk.Label(outer, text=title, font=("Inter", 24, "bold"),
                 bg="#0d1116", fg="#ffffff").pack(pady=(0, 15))

        tk.Label(outer, text=message, font=("Inter", 20),
                 bg="#0d1116", fg="#e6edf3", wraplength=450, justify="center").pack(pady=(0, 10))

        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_set()

        self.wait_window()

class AboutDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title(f"About {APP_NAME}")
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#0d1116")
        self.resizable(False, False)
        
        self._logo_img = None
        
        # Un unico frame che contiene tutto, con un po' di respiro (padding)
        outer = tk.Frame(self, bg="#0d1116", padx=40, pady=40)
        outer.pack(fill=tk.BOTH, expand=True)

        if os.path.exists(LOGO_PATH):
            try:
                pil_img = Image.open(LOGO_PATH)
                pil_img = pil_img.resize((450, 450), Image.Resampling.LANCZOS)
                self._logo_img = ImageTk.PhotoImage(pil_img)
                tk.Label(outer, image=self._logo_img, bg="#0d1116").pack(pady=(0, 20))
            except Exception as e:
                print(f"Logo error: {e}")

        tk.Label(outer, text=APP_NAME.upper(), font=("Inter", 36, "bold"), bg="#0d1116", fg="#ffffff").pack(pady=(0, 5))
        tk.Label(outer, text=APP_TAGLINE, font=("Inter", 18), bg="#0d1116", fg="#8b949e").pack(pady=(0, 20))
        tk.Label(outer, text=f"Version {APP_VERSION}", font=("Inter", 22, "bold"), bg="#0d1116", fg="#ffffff").pack(pady=(0, 5))
        tk.Label(outer, text=f"{APP_AUTHOR}", font=("Inter", 22, "bold"), bg="#0d1116", fg="#e6edf3").pack(pady=(0, 5))
        tk.Label(outer, text=f"{APP_TECH}", font=("Inter", 16), bg="#0d1116", fg="#8b949e", justify=tk.CENTER).pack(pady=(0, 15))
        
        tk.Label(outer, text=f"\u00a9 {APP_YEAR}", font=("Inter", 16), bg="#0d1116", fg="#6e7681").pack(pady=(0, 30))
        
        
        self.bind("<Escape>", lambda e: self.destroy())
        
        # Aggiorna l'interfaccia per calcolare la dimensione reale, poi centra
        self.update_idletasks()
        
        # Calcola la dimensione richiesta dal contenuto
        win_w = self.winfo_reqwidth()
        win_h = self.winfo_reqheight()
        
        # Trova il centro dello schermo
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        
        # Imposta la geometria finale (dimensioni + posizione)
        self.geometry(f"{win_w}x{win_h}+{max(0, x)}+{max(0, y)}")

class PhotoCropApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Photo Crop")
        self.root.geometry("1024x834")
        self.root.minsize(800, 680)
        self.root.resizable(True, True)
        self.root.configure(bg="#0d1116")

        config = load_config()
        self.files_to_process = []
        self.last_dir = config.get("last_dir", os.path.expanduser("~"))
        self.output_dir = config.get("output_dir", "")
        # Crop mode: "auto" or "manual" (remembered between sessions)
        settings = get_settings()
        self.crop_mode = settings.get("crop_mode", "manual")
        if self.crop_mode not in ("auto", "manual"):
            self.crop_mode = "manual"
        self.processing_index = 0
        self.success_count = 0
        self.inspector = None
        self.worker = None
        self.writer = None          # background thread that crops and writes the files
        self.slots = None
        self.saves = []
        self.reserved = set()
        self.run_settings = None
        self._confirming = False
        self.pending_manual = []  # photos whose auto-detected quad is not plausible
        self._manual_pass = False  # True during the final manual review of the photos auto mode rejected
        self.batch_total = 0       # number of scans selected for this batch

        main_frame = tk.Frame(root, bg="#0d1116", padx=15, pady=15)
        main_frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(main_frame, text="PHOTO CROP", font=("Inter", 22, "bold"),
                 bg="#0d1116", fg="#ffffff").pack(pady=4)

        self.btn_select = LightButton(main_frame, text="1. Select Scan Images",
                                      command=self.select_files,
                                      font=("Inter", 24, "bold"), bd=1, relief="solid",
                                      highlightthickness=0, padx=15, pady=14)
        self.btn_select.pack(fill=tk.X, pady=6)

        self.lbl_status = tk.Label(main_frame, text="No files selected",
                                   font=("Inter", 20, "bold"), bg="#0d1116", fg="#ffffff")
        self.lbl_status.pack(pady=4)

        options_container = tk.Frame(main_frame, bg="#0d1116")
        options_container.pack(fill=tk.X, pady=6)
        options_container.grid_columnconfigure(0, weight=1)
        options_container.grid_columnconfigure(1, weight=1)

        self.var_suffix = tk.BooleanVar(value=True)
        self.btn_suffix_opt = SegmentedButton(options_container, text="Add '_cropped' suffix",
                                              command=lambda: self.set_mode(True),
                                              font=("Inter", 22, "bold"))
        self.btn_suffix_opt.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=4)
        self.btn_overwrite_opt = SegmentedButton(options_container, text="Overwrite Original",
                                                 command=lambda: self.set_mode(False),
                                                 font=("Inter", 22, "bold"))
        self.btn_overwrite_opt.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=4)
        self.update_mode_ui()

        self.btn_output = GreyButton(main_frame, text=self.output_button_text(),
                                     command=self.select_output_dir,
                                     bg="#21262d", fg="#ffffff", font=("Inter", 20, "bold"),
                                     bd=1, relief="solid", highlightthickness=0,
                                     padx=15, pady=10)
        self.btn_output.pack(fill=tk.X, pady=6)

        self.btn_format = GreyButton(main_frame, text=self.format_button_text(),
                                     command=self.open_format_settings,
                                     bg="#21262d", fg="#ffffff", font=("Inter", 20, "bold"),
                                     bd=1, relief="solid", highlightthickness=0,
                                     padx=15, pady=10)
        self.btn_format.pack(fill=tk.X, pady=6)

        # Crop mode: Auto / Manual (same style as the suffix/overwrite buttons)
        crop_mode_container = tk.Frame(main_frame, bg="#0d1116")
        crop_mode_container.pack(fill=tk.X, pady=6)
        crop_mode_container.grid_columnconfigure(0, weight=1)
        crop_mode_container.grid_columnconfigure(1, weight=1)

        self.btn_auto_opt = SegmentedButton(crop_mode_container, text="Auto",
                                            command=lambda: self.set_crop_mode("auto"),
                                            font=("Inter", 22, "bold"))
        self.btn_auto_opt.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=4)
        self.btn_manual_opt = SegmentedButton(crop_mode_container, text="Manual",
                                              command=lambda: self.set_crop_mode("manual"),
                                              font=("Inter", 22, "bold"))
        self.btn_manual_opt.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=4)
        self.update_crop_mode_ui()

        self.btn_process = LightButton(main_frame, text="2. Start Cropping",
                                       command=self.start_processing, state=tk.DISABLED,
                                       font=("Inter", 24, "bold"), bd=1, relief="solid",
                                       highlightthickness=0, padx=15, pady=14)
        self.root.bind("<Return>", lambda e: self.start_processing() if self.btn_process.cget("state") != tk.DISABLED else None)
        self.btn_process.pack(fill=tk.X, pady=6)

        log_container = tk.Frame(main_frame, bg="#161b22", bd=1, relief="solid")
        log_container.pack(fill=tk.BOTH, expand=True, pady=6)
        self.txt_log = tk.Text(log_container, font=("Inter", 13), bg="#161b22",
                               fg="#e6edf3", bd=0, highlightthickness=0, state="disabled")
        self.txt_log.pack(fill=tk.BOTH, expand=True)
        self.log_scroll = AutoHideScrollbar(log_container, self.txt_log, hide_delay=1000, width=14,
                                            bg="#30363d", troughcolor="#161b22",
                                            activebackground="#484f58",
                                            bd=0, highlightthickness=0)

    def set_mode(self, add_suffix):
        self.var_suffix.set(add_suffix)
        self.update_mode_ui()

    def set_crop_mode(self, mode):
        """Switch between 'auto' (batch, no window) and 'manual' (open the crop window)."""
        if mode not in ("auto", "manual"):
            return
        self.crop_mode = mode
        self.update_crop_mode_ui()
        # Persist the choice in the settings file
        s = get_settings()
        s["crop_mode"] = mode
        save_settings(s)

    def update_crop_mode_ui(self):
        is_auto = self.crop_mode == "auto"
        self.btn_auto_opt.draw(is_auto)
        self.btn_manual_opt.draw(not is_auto)

    def update_mode_ui(self):
        is_suffix = self.var_suffix.get()
        self.btn_suffix_opt.draw(is_suffix)
        self.btn_overwrite_opt.draw(not is_suffix)

    def output_button_text(self):
        if not self.output_dir:
            return "Save to: Same folder as source files"
        path = self.output_dir
        if len(path) > 38:
            path = "..." + path[-37:]
        return f"Save to: {path}"

    def select_output_dir(self):
        start_dir = self.output_dir if (self.output_dir and os.path.isdir(self.output_dir)) else self.last_dir
        dialog = FolderSelectorDialog(self.root, start_dir)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self.output_dir = dialog.result or ""
        self.btn_output.config(text=self.output_button_text())
        config = load_config()
        config["output_dir"] = self.output_dir
        save_config(config)
        if self.output_dir:
            self.log_safe(f"Output folder: {self.output_dir}")
        else:
            self.log_safe("Output folder: same folder as source files")

    def format_button_text(self):
        text = f"\u2699  Save as: {summarize_settings(get_settings())}"
        return text if len(text) <= 48 else text[:47] + "\u2026"

    def open_format_settings(self):
        dialog = FormatSettingsDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        # Preserve the current crop_mode: FormatSettingsDialog doesn't manage it
        result = dialog.result
        result["crop_mode"] = self.crop_mode
        save_settings(result)
        self.btn_format.config(text=self.format_button_text())
        self.log_safe(f"Output formats: {summarize_settings(result)}")

    def log_safe(self, message):
        self.txt_log.config(state="normal")
        self.txt_log.insert(tk.END, _tk_text(message) + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state="disabled")

    def status_safe(self, message):
        self.lbl_status.config(text=_tk_text(message))

    def update_select_button_text(self):
        if self.files_to_process:
            self.btn_select.config(text="1. Add More Scans")
        else:
            self.btn_select.config(text="1. Select Scan Images")

    def finish_batch_ui(self):
        total = self.batch_total or len(self.files_to_process)
        finish_text = f"Completed! {self.success_count}/{total} scans processed."
        self.status_safe(finish_text)
        self.log_safe(f"\nBatch completed! Processed {self.success_count}/{total} files.")
        self.files_to_process.clear()
        self.update_select_button_text()
        self.btn_select.config(state=tk.NORMAL)
        self.btn_output.config(state=tk.NORMAL)
        self.btn_format.config(state=tk.NORMAL)
        self.btn_process.config(state=tk.DISABLED)
        
        # Show the dialog only if at least one file was processed
        if self.success_count > 0:
            StyledMessageBox(self.root, "Batch completed",
                             f"{self.success_count} of {total} files processed")

    def select_files(self):
        dialog = FileSelectorDialog(self.root, self.last_dir)
        self.root.wait_window(dialog)
        if hasattr(dialog, 'current_dir') and os.path.exists(dialog.current_dir):
            self.last_dir = dialog.current_dir
            config = load_config()
            config["last_dir"] = self.last_dir
            save_config(config)
        if dialog.selected_files:
            already_listed = set(self.files_to_process)
            new_files = [p for p in dialog.selected_files if p not in already_listed]
            skipped = len(dialog.selected_files) - len(new_files)
            if not new_files:
                self.log_safe("No new files added: the selected images are already in the list")
                return
            first_selection = not self.files_to_process
            self.files_to_process.extend(new_files)
            added = len(new_files)
            total_ready = len(self.files_to_process)
            added_noun = "image" if added == 1 else "images"
            total_noun = "image" if total_ready == 1 else "images"
            self.lbl_status.config(text=f"{total_ready} scan {total_noun} ready")
            self.update_select_button_text()
            self.btn_process.config(state=tk.NORMAL)
            if first_selection:
                self.log_safe(f"Loaded {added} scan {added_noun}")
            else:
                self.log_safe(f"Added {added} scan {added_noun} ({total_ready} total)")
            for path in new_files:
                folder, name = os.path.split(path)
                self.log_safe(f"{name} from: {folder}")
            if skipped:
                self.log_safe(f"({skipped} already in the list, skipped)")

    def start_processing(self):
        if self.output_dir and not (os.path.isdir(self.output_dir) and os.access(self.output_dir, os.W_OK)):
            messagebox.showwarning("Warning",
                                   f"The output folder is not available or not writable:\n{self.output_dir}")
            return
        self.btn_select.config(state=tk.DISABLED)
        self.btn_output.config(state=tk.DISABLED)
        self.btn_format.config(state=tk.DISABLED)
        self.btn_process.config(state=tk.DISABLED)
        self.processing_index = 0
        self.success_count = 0
        self.pending_manual = []
        self._manual_pass = False
        self.batch_total = len(self.files_to_process)

        # Settings are read once per batch; saving runs in a background thread
        self.run_settings = get_settings()
        self.writer = ThreadPoolExecutor(max_workers=1)
        self.slots = threading.BoundedSemaphore(MAX_PENDING_SAVES)
        self.saves = []
        self.reserved = set()
        self._confirming = False
        st = self.run_settings
        self.log_safe(f"Output formats: {summarize_settings(st)}")
        families = [f for f in ("jpeg", "tiff", "png") if st["same_as_original"] or st["outputs"][f]]
        wants_meta = (any(st[f]["metadata_exif"] or st[f]["metadata_xmp"] or st[f]["dpi_force"] for f in families)
                      or any(str(v).strip() for v in st["manual"].values()))
        if not EXIFTOOL and wants_meta:
            self.log_safe("[WARNING] exiftool not found: DPI, ICC profile and EXIF will not be copied.\n"
                          "  Install it with: sudo apt install libimage-exiftool-perl")

        # Start the background prefetch worker
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:
                pass
        self.worker = PrefetchWorker(_detect_card_quad)
        self.worker.start()

        self.process_next_file()

    def process_next_file(self):
        if self.processing_index >= len(self.files_to_process):
            self._close_inspector_and_finish()
            return
        src_path = self.files_to_process[self.processing_index]
        total = len(self.files_to_process)
        idx = self.processing_index + 1
        file_name = os.path.basename(src_path)
        status_text = f"Processing [{idx}/{total}]: {file_name}"
        self.status_safe(status_text)
        self.log_safe(f"\n{status_text}")

        # Ask the worker for the image and wait for it WITHOUT freezing the window
        if self.worker is not None:
            self.worker.request(src_path)
            self._poll_prefetch(src_path, time.time() + 30.0)
        else:
            self._continue_with_scan(src_path, None)

    def _poll_prefetch(self, src_path, deadline):
        worker = self.worker
        result = worker.poll_result(src_path) if worker is not None else None
        if result is None and worker is not None and time.time() < deadline:
            self.root.after(25, self._poll_prefetch, src_path, deadline)
            return
        self._continue_with_scan(src_path, result)

    def _continue_with_scan(self, src_path, result):
        file_name = os.path.basename(src_path)

        if result is None:
            # Fallback: load synchronously
            try:
                scan = load_scan(src_path)
                if scan[0] is None:
                    self.log_safe(f"  [ERROR] Could not load image: {file_name}")
                    self.processing_index += 1
                    self.root.after(100, self.process_next_file)
                    return
                quad, used_fallback = _split_detection(_detect_card_quad(scan[1]))
            except Exception as e:
                self.log_safe(f"  [EXCEPTION] {e}")
                self.processing_index += 1
                self.root.after(100, self.process_next_file)
                return
        else:
            scan, quad, err, used_fallback = result
            if err is not None or scan is None:
                self.log_safe(f"  [ERROR] {err or 'Cannot load image'}: {file_name}")
                self.processing_index += 1
                self.root.after(100, self.process_next_file)
                return

        # Look-ahead: load and analyse the NEXT scan in the background while this one is handled
        # (in manual and in auto mode)
        next_idx = self.processing_index + 1
        if self.worker is not None and next_idx < len(self.files_to_process):
            self.worker.request(self.files_to_process[next_idx])

        # In auto mode, skip the dialog and apply the same crop functions directly.
        if self.crop_mode == "auto" and not self._manual_pass:
            self._process_auto_crop(src_path, scan, quad, used_fallback)
            return

        # Create or reuse the inspector dialog
        if self.inspector is None:
            self.inspector = InteractiveCropDialog(
                self.root,
                on_confirm=self._on_inspector_confirm,
                on_skip=self._on_inspector_skip,
            )

        try:
            self.inspector.load_image(src_path, quad, scan[0], scan[1])
        except Exception as e:
            self.log_safe(f"  [EXCEPTION] load_image: {e}")
            self.processing_index += 1
            self.root.after(100, self.process_next_file)
            return

    def _process_auto_crop(self, src_path, scan, quad, used_fallback=False):
        """Apply the same crop pipeline as the manual dialog, without opening the window.
        scan: (full-depth image, 8-bit view). quad: (4,2) corners from _detect_card_quad.
        If the quad is not plausible, the photo is queued for manual review at the end of the batch."""
        full, view = scan
        img_w = view.shape[1]
        img_h = view.shape[0]

        # A detection obtained with the fallback method is not trusted in unattended mode.
        ok, reason = _is_quad_plausible(quad, img_w, img_h, used_fallback=used_fallback)
        if not ok:
            file_name = os.path.basename(src_path)
            self.log_safe(f"  [AUTO->MANUAL] {file_name}: {reason}")
            self.pending_manual.append(src_path)
            self.processing_index += 1
            self.root.after(50, self.process_next_file)
            return

        # Build the same rotated image and crop rectangle the dialog would build.
        settings = self.run_settings
        interp_flag = INTERPOLATIONS[settings["interpolation"]]

        # Alignment options (same as dialog.load_image)
        opts = _compute_alignment_options(quad)
        angle_h = opts["angle_h"]
        angle_v = opts["angle_v"]
        if abs(angle_h) <= abs(angle_v):
            rotation_angle = angle_h
        else:
            rotation_angle = angle_v

        # Rotate the 8-bit view (for the preview) and only the crop region from the full-depth image.
        rotated_view, rot_matrix = _rotate_image(view, rotation_angle, interp_flag)
        h_rot, w_rot = rotated_view.shape[:2]

        rotated_quad = _apply_affine_to_pts(quad, rot_matrix)
        rotated_quad = _clip_quad_to_image(rotated_quad, w_rot, h_rot)
        inscribed = _inscribed_rect_from_quad(rotated_quad)
        rect = [
            float(inscribed[:, 0].min()),
            float(inscribed[:, 1].min()),
            float(inscribed[:, 0].max()),
            float(inscribed[:, 1].max()),
        ]

        # Compute the integer crop box (same as _do_confirm)
        try:
            x_min = max(0, int(np.floor(rect[0])))
            y_min = max(0, int(np.floor(rect[1])))
            x_max = min(rotated_view.shape[1], int(np.ceil(rect[2])))
            y_max = min(rotated_view.shape[0], int(np.ceil(rect[3])))
            if x_max <= x_min or y_max <= y_min:
                raise ValueError("the crop area is empty")
        except Exception as e:
            file_name = os.path.basename(src_path)
            self.log_safe(f"  [AUTO->MANUAL] {file_name}: {e}")
            self.pending_manual.append(src_path)
            self.processing_index += 1
            self.root.after(50, self.process_next_file)
            return

        # Plan the outputs (same as _do_confirm)
        dir_name, file_name = os.path.split(src_path)
        base_name, ext = os.path.splitext(file_name)
        out_dir = self.output_dir if self.output_dir else dir_name
        suffix = "_cropped" if self.var_suffix.get() else ""

        # Never more than MAX_PENDING_SAVES images waiting to be written (limits RAM usage)
        if not self.slots.acquire(blocking=False):
            self.status_safe("Saving the previous images...")
            self.root.after(150, lambda: self._process_auto_crop(src_path, scan, quad, used_fallback))
            return

        try:
            plans = self._plan_outputs(src_path, out_dir, base_name, ext, suffix)
            job = {
                "src_path": src_path,
                "settings": settings,
                "plans": plans,
                "full": None if full.dtype == np.uint8 else full,
                "rotated": rotated_view,
                "matrix": np.copy(rot_matrix),
                "angle": float(rotation_angle),
                "box": (x_min, y_min, x_max, y_max),
                "interp": interp_flag,
                "release": self.slots.release,
            }
            self.saves.append(self.writer.submit(save_outputs, job, self._threadsafe_log))
        except Exception as e:
            self.slots.release()
            self.log_safe(f"  [EXCEPTION] auto save: {e}")

        self.processing_index += 1
        self.root.after(50, self.process_next_file)

    def _threadsafe_log(self, message):
        self.root.after(0, lambda m=message: self.log_safe(m))

    def _plan_outputs(self, src_path, out_dir, base_name, ext, suffix):
        """Which files will be written (format + final path). Names are reserved here, in the main thread."""
        st = self.run_settings
        wanted = []
        if st["same_as_original"]:
            wanted.append((FAMILY_BY_EXT.get(ext.lower()) or "jpeg", ext if ext else ".jpg"))
        for fam, e in (("jpeg", ".jpg"), ("tiff", ".tif"), ("png", ".png")):
            if st["outputs"][fam] and all(fam != w[0] for w in wanted):
                wanted.append((fam, e))
        return [{"family": fam,
                 "path": make_output_path(out_dir, base_name, suffix, e, src_path, self.reserved)}
                for fam, e in wanted]

    def _on_inspector_confirm(self, dialog):
        if self._confirming:
            return
        # Never more than MAX_PENDING_SAVES images waiting to be written (limits RAM usage)
        if not self.slots.acquire(blocking=False):
            self._confirming = True
            self.status_safe("Saving the previous images...")
            self.root.after(150, self._retry_confirm, dialog)
            return
        self._do_confirm(dialog)

    def _retry_confirm(self, dialog):
        if self.slots.acquire(blocking=False):
            self._confirming = False
            self._do_confirm(dialog)
        else:
            self.root.after(150, self._retry_confirm, dialog)

    def _do_confirm(self, dialog):
        src_path = self.files_to_process[self.processing_index]
        dir_name, file_name = os.path.split(src_path)
        base_name, ext = os.path.splitext(file_name)
        out_dir = self.output_dir if self.output_dir else dir_name
        try:
            final_pts = np.copy(dialog.pts)
            rotated = dialog.img_bgr
            x_min = max(0, int(np.floor(final_pts[:, 0].min())))
            y_min = max(0, int(np.floor(final_pts[:, 1].min())))
            x_max = min(rotated.shape[1], int(np.ceil(final_pts[:, 0].max())))
            y_max = min(rotated.shape[0], int(np.ceil(final_pts[:, 1].max())))
            if x_max <= x_min or y_max <= y_min:
                raise ValueError("the crop area is empty")
            suffix = "_cropped" if self.var_suffix.get() else ""
            plans = self._plan_outputs(src_path, out_dir, base_name, ext, suffix)
            full = dialog.img_full
            job = {
                "src_path": src_path,
                "settings": self.run_settings,
                "plans": plans,
                "full": None if full.dtype == np.uint8 else full,  # 8-bit scans are cropped from the preview pixels
                "rotated": rotated,
                "matrix": np.copy(dialog.rot_matrix),
                "angle": float(dialog.rotation_angle),
                "box": (x_min, y_min, x_max, y_max),
                "interp": dialog.interp_flag,
                "release": self.slots.release,
            }
            self.saves.append(self.writer.submit(save_outputs, job, self._threadsafe_log))
        except Exception as e:
            self.slots.release()
            self.log_safe(f"  [EXCEPTION] save: {e}")
        self.processing_index += 1
        self.root.after(50, self.process_next_file)

    def _on_inspector_skip(self, dialog):
        if self._confirming:
            return
        src_path = self.files_to_process[self.processing_index]
        file_name = os.path.basename(src_path)
        self.log_safe(f"  [SKIPPED] {file_name} skipped by user")
        self.processing_index += 1
        self.root.after(50, self.process_next_file)

    def _close_inspector_and_finish(self):
        # If auto mode left some photos for manual review, process them now.
        if self.pending_manual:
            remaining = list(self.pending_manual)
            self.pending_manual = []
            self.files_to_process = remaining
            self.processing_index = 0
            self.log_safe(f"\nManual review for {len(remaining)} photo(s) skipped by auto-detection...")
            # Manual review for this final pass only: the Auto/Manual choice itself is left untouched
            self._manual_pass = True
            self.root.after(50, self.process_next_file)
            return

        if self.inspector is not None:
            try:
                self.inspector.destroy()
            except Exception:
                pass
            self.inspector = None
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None
        self._finish_when_saved()

    def _finish_when_saved(self):
        """Wait (without freezing the window) until the background writer has finished every file."""
        if any(not f.done() for f in self.saves):
            self.status_safe("Saving the last images...")
            self.root.after(200, self._finish_when_saved)
            return
        self.success_count = sum(1 for f in self.saves if f.result())
        self.saves = []
        if self.writer is not None:
            self.writer.shutdown(wait=False)
            self.writer = None
        self.finish_batch_ui()


if __name__ == "__main__":
    root = tk.Tk()
    app = PhotoCropApp(root)
    root.mainloop()