"""Revalidate recorded extraction against its immutable source; no provider calls."""
from pathlib import Path
import os
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.infrastructure.documents import inspect_document
from invoicedesk.infrastructure.extraction.evidence import verify_references
from invoicedesk.infrastructure.extraction.grounding import ground
from invoicedesk.infrastructure.extraction.text import read_document_text


def verify_stored_source(invoice: ExtractedInvoice, path: Path | None, expected_sha256: str, *, raw_response: dict | None = None):
    inv = invoice.model_copy(deep=True)
    pages = []
    reader_error = None
    if path and path.is_file():
        document = inspect_document(path)
        if document.sha256 != expected_sha256:
            raise ValueError('Stored document integrity mismatch. Approval is blocked.')
        try:
            pages = read_document_text(path).pages
        except Exception as exc:
            reader_error = type(exc).__name__
    reconstructed = False
    if raw_response is not None and any(page.strip() for page in pages):
        from invoicedesk.domain.validation import parse_response
        from invoicedesk.infrastructure.extraction.tier1 import parse
        from invoicedesk.infrastructure.extraction.router import merge_extraction
        parsed = parse_response(raw_response)
        rebuilt = merge_extraction(parse('\n'.join(pages)),parsed.invoice,provided_fields=parsed.provided_fields,
            invalid_fields={issue['field'].split('.')[0] for issue in parsed.issues},text='\n'.join(pages),pages=pages)
        rebuilt.extraction_path = inv.extraction_path
        inv = rebuilt
        reconstructed = True
    if inv.extraction_path in {'llm_text', 'llm_vision'}:
        # Recompute only source warnings. Parse failures and tier conflicts survive.
        inv.warnings = [w for w in inv.warnings if not w.startswith('Grounding failed: ')]
        ground(inv, '\n'.join(pages), pages=pages)
    references = verify_references(inv, pages)
    metadata = dict(validator='source-validation-4',document_sha256=expected_sha256,
        pdf_reader=os.getenv('INVOICEDESK_PARSER','pdfplumber'),reader_error=reader_error,
        text_available=any(page.strip() for page in pages),
        grounded_count=sum(r.verification in {'VERIFIED','GROUNDED'} for r in references),
        unverified_count=sum(r.verification=='UNVERIFIED' for r in references),
        provider_calls=0,reconstructed_from_recording=reconstructed)
    return inv, references, metadata
