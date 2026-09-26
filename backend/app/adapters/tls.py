"""Explicit private CA trust; never fall back to verification-disabled clients."""

import os
import ssl


def trust(verify):
    return ssl.create_default_context(cafile=verify) if isinstance(verify, str) else verify


def require_https(url):
    if os.environ.get("QC_REQUIRE_TLS") == "1" and not url.startswith("https://"):
        raise ValueError("This deployment requires an HTTPS ERP endpoint.")
