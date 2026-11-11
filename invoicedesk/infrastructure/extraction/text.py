"""Read PDF text while retaining coverage of every page."""
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

@dataclass(frozen=True)
class DocumentText:
    pages: list[str]

    @property
    def text(self) -> str:
        return '\n'.join(self.pages)

    @property
    def needs_vision(self) -> bool:
        # A text cover page must not conceal unread scanned invoice pages.
        return not self.pages or any(len(page.strip()) < 40 for page in self.pages)


def _ocr_image(image) -> str:
    """Independent local OCR; model-authored evidence is never its own proof."""
    executable = shutil.which('tesseract')
    if not executable:
        return ''
    with tempfile.TemporaryDirectory(prefix='invoicedesk-ocr-') as directory:
        source = Path(directory)/'page.png'
        image.save(source, format='PNG')
        result = subprocess.run([executable, str(source), 'stdout', '--psm', '3'],
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', check=True, timeout=30)
        return result.stdout


def _ocr_pdf_pages(path: Path, pages: list[str]) -> list[str]:
    if not shutil.which('tesseract') or not any(len(page.strip()) < 40 for page in pages):
        return pages
    import pypdfium2
    result = list(pages)
    with pypdfium2.PdfDocument(path) as pdf:
        for index, text in enumerate(result):
            if len(text.strip()) >= 40:
                continue
            try:
                page = pdf[index]
                scale = min(2.5, (20_000_000 / max(1, page.get_width()*page.get_height()))**0.5)
                bitmap = page.render(scale=scale)
                try:
                    result[index] = _ocr_image(bitmap.to_pil()) or text
                finally:
                    bitmap.close()
                    page.close()
            except (OSError, subprocess.SubprocessError, ValueError):
                # Unreadable pages stay unverified rather than blocking model extraction.
                continue
    return result


def read_document_text(path: Path) -> DocumentText:
    if path.read_bytes()[:5] != b'%PDF-':
        from PIL import Image, ImageOps
        try:
            with Image.open(path) as image:
                return DocumentText([_ocr_image(ImageOps.exif_transpose(image).convert('RGB'))])
        except (OSError, subprocess.SubprocessError):
            return DocumentText([''])
    if os.getenv('INVOICEDESK_PARSER', 'pdfplumber') == 'poppler':
        if not shutil.which('pdftotext'):
            raise RuntimeError('Install pdfplumber or Poppler to read PDF text.')
        result = subprocess.run(
            ['pdftotext', '-layout', str(path), '-'], capture_output=True,
            text=True, check=True, timeout=20,
        )
        pages = result.stdout.split('\f')
        if pages and not pages[-1].strip():
            pages.pop()
        return DocumentText(_ocr_pdf_pages(path, pages))
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or '' for page in pdf.pages]
    return DocumentText(_ocr_pdf_pages(path, pages))


def read_text(path: Path) -> str:
    return read_document_text(path).text
