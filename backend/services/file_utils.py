"""Helpers for handling untrusted names and files coming from email / Excel / Drive."""
import io
import os
import re

import pymupdf as fitz
from PIL import Image, ImageOps

from backend.config import WORK_ROOT

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def safe_name(value: str, fallback: str = "file") -> str:
    """Reduce an arbitrary string to a filesystem-safe single path component."""
    name = _UNSAFE.sub("_", os.path.basename(str(value or "").replace("\\", "/"))).strip("._")
    return name[:100] or fallback


def candidate_workdir(candidate_id: str) -> str:
    return os.path.join(WORK_ROOT, safe_name(candidate_id, "candidate"))


A4_PT = (595, 842)


def image_to_pdf(image_path: str, pdf_path: str) -> str:
    """Wrap a photo/scan in a one-page PDF (fitted to A4, EXIF rotation applied) so the
    rest of the pipeline only ever sees PDFs."""
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        width, height = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)

    page_w, page_h = A4_PT if height >= width else A4_PT[::-1]
    scale = min(page_w / width, page_h / height)
    w, h = width * scale, height * scale

    doc = fitz.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def drive_escape(value: str) -> str:
    """Escape a literal for use inside a single-quoted Drive API query."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")
