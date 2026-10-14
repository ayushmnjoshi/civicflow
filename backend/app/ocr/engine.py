"""OCR engine adapter.

Design
------
- No temp files: everything in memory.
- Images (jpg/png): Pillow + pytesseract with a timeout (SIGALRM on Unix,
  threading.Timer on Windows).
- PDFs: pypdfium2. First try embedded text. If shorter than OCR_MIN_TEXT_CHARS,
  render up to OCR_MAX_PAGES pages to PIL images and OCR them.
- Errors are raised as OcrError so callers can distinguish OCR failures from
  application bugs. The app never crashes due to a bad document.
- Exposed via get_ocr_engine() FastAPI dependency so tests can inject a fake
  that returns canned text without needing Tesseract installed.
"""

from __future__ import annotations

import io
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Result and error types ────────────────────────────────────────────────────

@dataclass
class OcrResult:
    text: str
    method: str  # "pdf_text" | "tesseract"
    page_count: int = 0


class OcrError(Exception):
    """Raised when the OCR engine cannot process a document."""


class TesseractNotFoundError(OcrError):
    """Raised when the Tesseract binary cannot be located."""


# ── Abstract interface (for dependency injection in tests) ────────────────────

class AbstractOcrEngine(ABC):
    @abstractmethod
    def extract(self, data: bytes, ext: str) -> OcrResult:
        """Extract text from *data* (file bytes) with file extension *ext*."""


# ── Real engine ───────────────────────────────────────────────────────────────

class TesseractOcrEngine(AbstractOcrEngine):
    """OCR engine backed by Tesseract + pypdfium2."""

    def __init__(
        self,
        tesseract_cmd: str,
        max_pages: int,
        timeout_seconds: int,
        min_text_chars: int,
    ) -> None:
        self._tesseract_cmd = tesseract_cmd
        self._max_pages = max_pages
        self._timeout_seconds = timeout_seconds
        self._min_text_chars = min_text_chars

    def _configure_pytesseract(self) -> None:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd

    def _ocr_image_bytes(self, image_bytes: bytes) -> str:
        """Run Tesseract on raw image bytes with a timeout.

        Uses a background thread + Timer so it works on Windows (no SIGALRM).
        Never passes user-controlled data to a shell.
        """
        import pytesseract
        from PIL import Image, UnidentifiedImageError
        from PIL.Image import DecompressionBombError

        self._configure_pytesseract()

        try:
            img = Image.open(io.BytesIO(image_bytes))
        except DecompressionBombError:
            raise OcrError("File rejected: decompression bomb detected.")
        except (UnidentifiedImageError, Exception) as exc:
            raise OcrError(f"Cannot open image: {exc}") from exc

        result_holder: list[str | BaseException] = []

        def _run() -> None:
            try:
                # lang=eng; no shell=True; no user data on command line
                text = pytesseract.image_to_string(img, lang="eng")
                result_holder.append(text)
            except pytesseract.TesseractNotFoundError as e:
                result_holder.append(TesseractNotFoundError(str(e)))
            except Exception as e:
                result_holder.append(OcrError(f"Tesseract failed: {e}"))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=self._timeout_seconds)

        if not result_holder:
            raise OcrError(
                f"Tesseract timed out after {self._timeout_seconds} seconds."
            )
        val = result_holder[0]
        if isinstance(val, BaseException):
            raise val
        return val  # type: ignore[return-value]

    def _pdf_embedded_text(self, data: bytes) -> tuple[str, int]:
        """Return (full_text, page_count) from a PDF using pypdfium2."""
        import pypdfium2 as pdfium

        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as exc:
            raise OcrError(f"Cannot open PDF: {exc}") from exc

        page_count = len(pdf)
        texts: list[str] = []
        for page in pdf:
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range())
        return "\n".join(texts), page_count

    def _pdf_ocr(self, data: bytes, page_count: int) -> str:
        """Render PDF pages to images and OCR them (fallback for scanned PDFs)."""
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        texts: list[str] = []
        for i, page in enumerate(pdf):
            if i >= self._max_pages:
                break
            bitmap = page.render(scale=2.0)  # 144 DPI for better OCR
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            texts.append(self._ocr_image_bytes(buf.getvalue()))
        return "\n".join(texts)

    def extract(self, data: bytes, ext: str) -> OcrResult:
        """Extract text from *data*.  Never logs document content."""
        ext = ext.lower().lstrip(".")

        if ext in ("jpg", "jpeg", "png"):
            text = self._ocr_image_bytes(data)
            return OcrResult(text=text, method="tesseract", page_count=1)

        if ext == "pdf":
            embedded_text, page_count = self._pdf_embedded_text(data)
            if len(embedded_text.strip()) >= self._min_text_chars:
                return OcrResult(
                    text=embedded_text, method="pdf_text", page_count=page_count
                )
            # Embedded text is too short — render and OCR
            ocr_text = self._pdf_ocr(data, page_count)
            return OcrResult(
                text=ocr_text, method="tesseract", page_count=page_count
            )

        raise OcrError(f"Unsupported extension: '{ext}'")


# ── FastAPI dependency ────────────────────────────────────────────────────────

_engine: TesseractOcrEngine | None = None


def _get_tesseract_engine() -> TesseractOcrEngine:
    global _engine
    if _engine is None:
        from app.config import settings
        _engine = TesseractOcrEngine(
            tesseract_cmd=settings.tesseract_cmd,
            max_pages=settings.ocr_max_pages,
            timeout_seconds=settings.ocr_timeout_seconds,
            min_text_chars=settings.ocr_min_text_chars,
        )
    return _engine


def get_ocr_engine() -> AbstractOcrEngine:
    """FastAPI dependency — override in tests with a fake engine."""
    return _get_tesseract_engine()
