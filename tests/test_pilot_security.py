import pytest
import os
from invoicedesk.interfaces.security import SecuritySettings, identity_from_claims, csrf_valid
from invoicedesk.infrastructure.storage import LocalStorage


def settings():
    return SecuritySettings(issuer='https://identity.example.com', client_id='invoice', client_secret='test-secret',
                            origin='https://invoice.example.com', allowed_users={'subject-1': {'reviewer'}})


def test_allowlist_is_bound_to_issuer_and_subject():
    config = settings()
    config.validate_startup()
    assert identity_from_claims({'iss':config.issuer,'sub':'subject-1'}, config).roles == {'reviewer'}
    with pytest.raises(PermissionError):
        identity_from_claims({'iss':config.issuer,'sub':'unknown','email':'subject-1'}, config)
    with pytest.raises(PermissionError):
        identity_from_claims({'iss':'https://attacker.example.com','sub':'subject-1'}, config)


def test_csrf_requires_matching_session_token_and_origin():
    assert csrf_valid('secret', 'secret', 'https://invoice.example.com', settings().origin)
    for supplied, origin in [('', settings().origin), ('wrong',settings().origin), ('secret','https://attacker.example.com')]:
        assert not csrf_valid('secret', supplied, origin, settings().origin)


def test_content_storage_does_not_accept_traversal_or_symlinks(tmp_path):
    storage = LocalStorage(tmp_path/'private')
    digest = storage.put(b'private example document')
    assert storage.path(digest).read_bytes() == b'private example document'
    if os.name != 'nt':  # Windows access is controlled by ACLs, not POSIX mode bits.
        assert storage.path(digest).stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        storage.path('../outside')
    storage.path(digest).unlink()
    try:
        (storage.root/digest).symlink_to(tmp_path/'outside')
    except OSError as exc:
        if os.name=='nt' and getattr(exc,'winerror',None)==1314:
            pytest.skip('Windows symlink creation requires developer mode or privilege.')
        raise
    with pytest.raises(ValueError):
        storage.path(digest)
