"""Resettable Render demo. Separate from loopback and PostgreSQL/OIDC servers."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hmac
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from .local_api import create_app as workspace_app
from .local_workspace import Workspace

NOTICE = 'Hackathon demo—data may reset. Shared judge workspace; use synthetic invoices only.'
APPLE_VENDOR_ID = 'V-009'
APPLE_PO = {
    'po_number': 'PO-1241',
    'vendor_id': APPLE_VENDOR_ID,
    'description': 'Apple Macbook M4 Apple iPhone 12 Apple iPhone Air Apple Airpods 4',
    'net_amount': '3196.00',
    'tax_rate': '0.12',
    'currency': 'USD',
    'status': 'open',
    'issue_date': '2026-11-01',
    'line_items_json': '[{"description":"Apple Macbook M4","quantity":"1","unit_price":"1499.00","amount":"1499.00"},{"description":"Apple iPhone 12","quantity":"1","unit_price":"599.00","amount":"599.00"},{"description":"Apple iPhone Air","quantity":"1","unit_price":"899.00","amount":"899.00"},{"description":"Apple Airpods 4","quantity":"1","unit_price":"199.00","amount":"199.00"}]',
    'charge_allowances_json': '[]',
}


def seed_hackathon_procurement(workspace):
    """Ensure synthetic Apple demo records exist without overwriting judge edits."""
    with workspace.db() as db, db.conn:
        vendor = db.conn.execute('INSERT OR IGNORE INTO vendors VALUES (?,?,?,?,?)', (
            APPLE_VENDOR_ID, 'Apple Technologies Ltd.',
            '["Apple Technologies Inc","Apple Technologies"]', 'approved',
            'Synthetic hackathon demonstration data; not production procurement authorization.',
        ))
        columns = list(APPLE_PO)
        po = db.conn.execute(f"INSERT OR IGNORE INTO purchase_orders ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                             tuple(APPLE_PO[column] for column in columns))
        if vendor.rowcount:
            db.conn.execute('INSERT OR IGNORE INTO ui_revisions VALUES (?,?,1)', ('vendors', APPLE_VENDOR_ID))
        if po.rowcount:
            db.conn.execute('INSERT OR IGNORE INTO ui_revisions VALUES (?,?,1)', ('purchase-orders', APPLE_PO['po_number']))
        if vendor.rowcount or po.rowcount:
            workspace.audit(db, 'workspace', 'hackathon-demo', 'synthetic_procurement_seeded',
                            {'vendor_id': APPLE_VENDOR_ID, 'po_number': APPLE_PO['po_number']}, actor='system')


@dataclass(frozen=True)
class Settings:
    origin: str
    username: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def from_env(cls):
        origin = os.environ.get('DEMO_PUBLIC_ORIGIN', '')
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.netloc != parsed.hostname
                or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', parsed.hostname)):
            raise ValueError('DEMO_PUBLIC_ORIGIN must be an exact HTTPS origin without a trailing slash.')
        username = os.environ.get('DEMO_USERNAME', '')
        password = os.environ.get('DEMO_PASSWORD', '')
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', username) or len(password) < 16:
            raise ValueError('Set DEMO_USERNAME and a DEMO_PASSWORD of at least 16 characters.')
        provider = os.environ.get('LLM_PROVIDER', '')
        if provider not in {'openai', 'anthropic'} or not os.environ.get('LLM_MODEL', '').strip():
            raise ValueError('Set LLM_PROVIDER (openai or anthropic) and LLM_MODEL.')
        if not os.environ.get(f'{provider.upper()}_API_KEY', '').strip():
            raise ValueError('Set the selected provider API key in runtime environment secrets.')
        return cls(origin, username, password)


class DemoAccess:
    """Authenticate every route and asset except the data-free health probe."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        health = scope['path'] == '/healthz' and scope['method'] in {'GET', 'HEAD'}
        if not health:
            if scope['scheme'] != 'https':
                response = JSONResponse({'error': 'HTTPS required.'}, status_code=403,
                                        headers={'Cache-Control': 'no-store'})
                return await response(scope, receive, send)
            headers = dict(scope['headers'])
            authorization = headers.get(b'authorization', b'')
            valid = False
            try:
                scheme, token = authorization.split(b' ', 1)
                if scheme.lower() == b'basic' and len(token) <= 4096:
                    user, password = base64.b64decode(token, validate=True).split(b':', 1)
                    user_ok = hmac.compare_digest(user, self.settings.username.encode())
                    password_ok = hmac.compare_digest(password, self.settings.password.encode())
                    valid = user_ok and password_ok
            except (ValueError, binascii.Error):
                pass
            if not valid:
                response = JSONResponse({'error': 'Judge login required.'}, status_code=401, headers={
                    'WWW-Authenticate': 'Basic realm="InvoiceDesk demo", charset="UTF-8"',
                    'Cache-Control': 'no-store',
                })
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(*, root: Path | None = None, start_worker=True):
    settings = Settings.from_env()  # Fail closed before creating any workspace.
    root = root or Path(tempfile.gettempdir())/'invoicedesk-hackathon'
    # A directory cannot be imported as a source DB. Never copy local history,
    # even when INVOICEDESK_DB points at a real operator workspace.
    workspace = Workspace(root=root, source=root)
    seed_hackathon_procurement(workspace)
    app = workspace_app(workspace, start_worker=start_worker,
                        allowed_hosts=[urlsplit(settings.origin).hostname],
                        allowed_origins={settings.origin}, demo_notice=NOTICE)

    async def health(request):
        healthy = not start_worker or bool(workspace.thread and workspace.thread.is_alive())
        return JSONResponse({'status': 'ok' if healthy else 'unavailable'}, status_code=200 if healthy else 503)

    # Register before the UI's catch-all route. Host validation still applies.
    app.router.routes.insert(0, Route('/healthz', health, methods=['GET', 'HEAD']))
    app.add_middleware(DemoAccess, settings=settings)
    return app


def main():
    import uvicorn
    port = int(os.environ.get('PORT', '10000'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535.')
    uvicorn.run(create_app(), host='0.0.0.0', port=port, workers=1,
                access_log=False, proxy_headers=True, forwarded_allow_ips='*')


if __name__ == '__main__':
    main()
