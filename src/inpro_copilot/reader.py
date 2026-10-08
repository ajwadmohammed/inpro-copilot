"""Step 1 of the pipeline: READ the document.

Goal: turn any invoice file (digital PDF, scanned PDF, photo) into a list of
words with their positions on the page, grouped into lines.

Why positions matter: invoices are layouts, not sentences. "Invoice No" sits
on the left and the number sits to its right. If we only kept a flat string
of text, columns would get mixed up (this happens a lot with real invoices).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf  # PyMuPDF
from PIL import Image

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # Tesseract is faster single-threaded
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
MIN_CHARS_PER_PAGE = 40  # fewer characters than this => page is probably a scan
OCR_DPI = 250


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page: int = 0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class Line:
    words: list[Word]
    page: int

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def y(self) -> float:
        return sum(w.yc for w in self.words) / len(self.words)


@dataclass
class ReadResult:
    path: str
    method: str  # "pdf_text", "ocr", or "pdf_text+ocr"
    pages: int
    words: list[Word] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


def group_into_lines(words: list[Word]) -> list[Line]:
    """Words whose vertical centres are close belong to the same visual line."""
    lines: list[Line] = []
    for page in sorted({w.page for w in words}):
        pw = sorted((w for w in words if w.page == page), key=lambda w: (w.yc, w.x0))
        current: list[Word] = []
        for w in pw:
            if current:
                ref = sum(x.yc for x in current) / len(current)
                tol = 0.55 * max(w.h, current[0].h)
                if abs(w.yc - ref) > tol:
                    lines.append(Line(sorted(current, key=lambda x: x.x0), page))
                    current = []
            current.append(w)
        if current:
            lines.append(Line(sorted(current, key=lambda x: x.x0), page))
    return lines


def _pdf_words(page: pymupdf.Page, page_no: int) -> list[Word]:
    out = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if text.strip():
            out.append(Word(x0, y0, x1, y1, text, page_no))
    return out


class OcrUnavailable(RuntimeError):
    """Tesseract is missing: scans/photos cannot be read, digital PDFs still can."""


_WINDOWS_TESSERACT = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]


def _find_tesseract() -> str | None:
    """Where is tesseract? TESSERACT_CMD setting, then PATH, then the usual Windows install folders
    (so it works even if 'Add to PATH' was not ticked during installation)."""
    import shutil
    for cand in [os.getenv("TESSERACT_CMD"), shutil.which("tesseract"), *(_WINDOWS_TESSERACT if os.name == "nt" else [])]:
        if cand and (shutil.which(cand) or os.path.isfile(cand)):
            return cand
    return None


def ocr_available() -> bool:
    import pytesseract
    cmd = _find_tesseract()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return cmd is not None


def _ocr_words(img: Image.Image, page_no: int, scale: float, min_conf: float = 0) -> list[Word]:
    """Run Tesseract. `scale` converts pixels back to PDF points so that
    OCR words and PDF words live in the same coordinate system."""
    import pytesseract

    ocr_available()          # points pytesseract at the installed tesseract, wherever it is
    try:
        data = pytesseract.image_to_data(img, lang="eng", output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError as e:
        raise OcrUnavailable(
            "This document is a scan or photo, so it needs OCR, but Tesseract is not installed "
            "(or not on PATH). Install Tesseract and restart the server. Digital PDFs work without it.") from e
    out = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text or float(data["conf"][i]) < min_conf or float(data["conf"][i]) < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        out.append(Word(x * scale, y * scale, (x + w) * scale, (y + h) * scale, text, page_no))
    return out


def _render(page: pymupdf.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def _embedded_scan(doc: pymupdf.Document, page: pymupdf.Page):
    """If the page is essentially one big picture (a scan), return (image, pixels->points scale)."""
    imgs = page.get_images(full=True)
    if not imgs:
        return None, 1.0
    xref = max(imgs, key=lambda im: im[2] * im[3])[0]
    try:
        raw = Image.open(io.BytesIO(doc.extract_image(xref)["image"])).convert("RGB")
    except Exception:
        return None, 1.0
    if raw.width < 600:          # an icon or logo, not a full-page scan
        return None, 1.0
    return raw, page.rect.width / raw.width


IMG_TEXT_MAX_PAGES, IMG_TEXT_MAX_IMAGES = 2, 6


def _picture_words(doc: pymupdf.Document, page: pymupdf.Page, page_no: int) -> list[Word]:
    """Text printed inside pictures on a page that otherwise has real text: a logo, or the supplier's company block
    pasted as an image (name, address, VAT number, IBAN). Many invoices do this, and without it the supplier's name is
    simply not in the text. Read with OCR, keeping only confident words, placed where the picture is drawn. Skipped
    quietly when Tesseract is not installed (digital PDFs must keep working without it)."""
    if not ocr_available():
        return []
    out: list[Word] = []
    for im in page.get_images(full=True)[:IMG_TEXT_MAX_IMAGES]:
        xref, w_px, h_px = im[0], im[2], im[3]
        if w_px < 300 or h_px < 60:                      # icons, lines, small decorations
            continue
        try:
            rect = page.get_image_rects(xref)[0]
            raw = Image.open(io.BytesIO(doc.extract_image(xref)["image"])).convert("RGB")
        except Exception:
            continue
        if rect.width < 80 or rect.height < 15:
            continue
        sx, sy = rect.width / raw.width, rect.height / raw.height
        try:
            words = _ocr_words(raw, page_no, 1.0, min_conf=60)
        except OcrUnavailable:
            return out
        out += [Word(rect.x0 + w.x0 * sx, rect.y0 + w.y0 * sy, rect.x0 + w.x1 * sx, rect.y0 + w.y1 * sy, w.text, page_no)
                for w in words]
    return out


def read_document(path: str | Path) -> ReadResult:
    path = Path(path)
    suffix = path.suffix.lower()

    # Case A: a plain image (photo or scan)
    if suffix in IMAGE_SUFFIXES:
        raw = Image.open(path)
        dpi = raw.info.get("dpi", (200, 200))[0] if isinstance(raw.info.get("dpi"), tuple) else 200
        dpi = dpi if 72 <= dpi <= 600 else 200       # phones often report nonsense; assume ~200 dpi
        img = raw.convert("RGB")
        words = _ocr_words(img, 0, 72.0 / dpi)        # pixels -> PDF points, same units as digital PDFs
        res = ReadResult(str(path), "ocr", 1, words, group_into_lines(words))
        if not words:
            res.notes.append("OCR found no text; image may be too blurry or empty")
        return res

    # Case B: a PDF
    doc = pymupdf.open(path)
    all_words: list[Word] = []
    used_text = used_ocr = False
    notes: list[str] = []
    for i, page in enumerate(doc):
        pw = _pdf_words(page, i)
        chars = sum(len(w.text) for w in pw)
        if chars >= MIN_CHARS_PER_PAGE:
            all_words += pw
            used_text = True
            if i < IMG_TEXT_MAX_PAGES:
                pics = _picture_words(doc, page, i)
                if pics:
                    all_words += pics
                    notes.append(f"page {i + 1}: also read the text inside pictures (logo or company details)")
        else:
            # Looks like a scan: no embedded text. Prefer the embedded image at its NATIVE
            # resolution (re-rendering would resample and blur it); else render the page.
            img, scale = _embedded_scan(doc, page)
            if img is None:
                img, scale = _render(page, OCR_DPI), 72.0 / OCR_DPI
            ow = _ocr_words(img, i, scale)
            all_words += ow
            used_ocr = True
            notes.append(f"page {i + 1}: no embedded text, used OCR")
    method = "pdf_text+ocr" if used_text and used_ocr else ("ocr" if used_ocr else "pdf_text")
    return ReadResult(str(path), method, len(doc), all_words, group_into_lines(all_words), notes)
