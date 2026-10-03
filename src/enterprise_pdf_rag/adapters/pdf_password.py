"""Open source PDF bytes with pdfspine, authenticating a password-protected one.

Every place that opens a source PDF goes through ``open_pdf``: unauthenticated, pdfspine
reads an encrypted PDF as empty pages (or, with object streams, as no pages at all), so a
direct ``pdfspine.open`` would silently ingest or re-prove nothing. The password is
``PDF_INGEST_PASSWORD`` (``Settings.pdf_ingest_password``); it is never part of a message.
"""

import pdfspine

from ragspine.common.evidence.configs import get_settings

_MISSING = (
    "PDF is password-protected; set PDF_INGEST_PASSWORD in the project .env to the password "
    "that opens it"
)
_WRONG = "PDF_INGEST_PASSWORD does not open this password-protected PDF"


class PdfPasswordError(ValueError):
    """A password-protected PDF without a configured password, or with one that does not open it."""


def open_pdf(pdf: bytes) -> pdfspine.Document:
    """``pdfspine.open(stream=pdf)``, authenticated with the configured password when needed.

    An unencrypted or owner-password-only PDF opens exactly as before (pdfspine already
    accepts the empty user password). Raises ``PdfPasswordError`` otherwise when no password
    is configured or it does not authenticate; ``pdfspine.PdfError`` from opening propagates.
    """
    document = pdfspine.open(stream=pdf, filetype="pdf")
    if not document.needs_pass:
        return document
    password = get_settings().pdf_ingest_password
    # ``needs_pass`` stays true after a successful ``authenticate``; only its result counts.
    if password is not None and document.authenticate(password.get_secret_value()):
        return document
    document.close()
    raise PdfPasswordError(_MISSING if password is None else _WRONG)
