"""Versioned pilot HTTP API. No document processing runs in a request task."""
import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import time
from typing import Annotated
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from authlib.integrations.starlette_client import OAuth
from invoicedesk.application.jobs import JobService
from invoicedesk.application.review import ReviewService
from invoicedesk.domain.approval import ApprovalBlocked, Conflict
from invoicedesk.domain.master import Vendor, PurchaseOrder
from invoicedesk.domain.policy import Policy
from invoicedesk.infrastructure.documents import MAX_BYTES
from .security import SecuritySettings, DatabaseSessions, ServerSessionMiddleware, Identity, identity_from_claims, csrf_valid


class ReviewCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    reason: str = Field(default='', max_length=4000)
    confirmed_source: bool = False


class CorrectionCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)
    changes: dict


class RetryCommand(BaseModel):
    revision: int = Field(ge=0)


class AdminCommand(BaseModel):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)
    data: dict


def create_app(repository, storage, security: SecuritySettings, *, production: bool = True) -> FastAPI:
    security.validate_startup()
    @asynccontextmanager
    async def lifespan(app):
        if production:
            from invoicedesk.infrastructure.preflight import require_production_dependencies
            require_production_dependencies()
            from invoicedesk.infrastructure.preflight import require_evaluated_model
            require_evaluated_model(mode='live')
        yield
    app = FastAPI(title='InvoiceDesk pilot backend', version='1.0.0', lifespan=lifespan,
                  description='Single-company invoice extraction, automatic approval, review and exact PO ledger posting.')
    app.add_middleware(ServerSessionMiddleware, store=DatabaseSessions(repository, security.session_seconds))
    from urllib.parse import urlsplit
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(security.origin).hostname, 'localhost'])
    @app.middleware('http')
    async def response_security(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        return response
    oauth = OAuth()
    oauth.register(name='company', client_id=security.client_id, client_secret=security.client_secret,
                   server_metadata_url=security.issuer.rstrip('/')+'/.well-known/openid-configuration',
                   client_kwargs={'scope': 'openid profile', 'code_challenge_method': 'S256'})
    jobs, reviews = JobService(repository, storage), ReviewService(repository)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.exception_handler(ApprovalBlocked)
    async def blocked(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={'detail': str(exc)})

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={'detail': 'Access denied.'})

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={'detail': 'Resource not found.'})

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        # Raw provider or document data is never interpolated into the response.
        return JSONResponse(status_code=422, content={'detail': 'Invalid request data.'})

    def current_user(request: Request) -> Identity:
        subject = request.session.get('subject')
        if not subject:
            raise HTTPException(401, 'Login required.')
        roles = security.allowed_users.get(subject)
        if roles is None:
            raise HTTPException(403, 'User is no longer allowed.')
        return Identity(subject=subject, roles=roles)

    def mutation(request: Request, user: Annotated[Identity, Depends(current_user)]) -> Identity:
        if not csrf_valid(request.session.get('csrf', ''), request.headers.get('x-csrf-token', ''),
                          request.headers.get('origin', ''), security.origin):
            raise HTTPException(403, 'CSRF validation failed.')
        return user

    # Retained on app.state to allow offline identity injection without a production bypass.
    app.state.current_user, app.state.mutation = current_user, mutation
    User = Annotated[Identity, Depends(current_user)]
    Mutator = Annotated[Identity, Depends(mutation)]
    Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=128)]

    @app.get('/health/live', include_in_schema=False)
    def health():
        return {'status': 'alive', 'release_accepted': False}

    @app.get('/health/ready', include_in_schema=False)
    def ready():
        with repository.engine.connect() as c:
            repository.policy(c)
        return {'status': 'ready'}

    @app.get('/auth/login')
    async def login(request: Request):
        if 'login_started' in request.session and time.time()-request.session['login_started'] < 2:
            raise HTTPException(429, 'Login already starting.')
        request.session.clear()
        request.session['login_started'] = time.time()
        return await oauth.company.authorize_redirect(request, security.origin.rstrip('/')+'/auth/callback')

    @app.get('/auth/callback')
    async def callback(request: Request):
        try:
            # Authlib validates authorization state, nonce, issuer, audience, signature and expiry.
            token = await oauth.company.authorize_access_token(request)
            if not token.get('id_token') or not token.get('userinfo'):
                raise PermissionError('An OIDC ID token is required.')
            user = identity_from_claims(dict(token['userinfo']), security)
        except Exception:
            request.session.clear()
            raise HTTPException(401, 'OIDC login failed.') from None
        request.session.clear()
        request.session.update(subject=user.subject, csrf=secrets.token_urlsafe(32))
        return RedirectResponse('/', status_code=303)

    @app.get('/v1/me')
    def me(request: Request, user: User):
        return dict(subject=user.subject, roles=sorted(user.roles), csrf_token=request.session['csrf'])

    @app.post('/auth/logout')
    def logout(request: Request, user: Mutator):
        request.session.clear()
        return {'logged_out': True}

    @app.post('/v1/jobs', status_code=202, openapi_extra={'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def submit(request: Request, user: Mutator, idempotency_key: Key,
                     filename: Annotated[str, Header(alias='X-Filename', max_length=255)] = 'document.pdf'):
        # Streaming raw bytes bounds memory before multipart parsers/spooling. OpenAPI documents the body below.
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, 'Document exceeds 20 MB.')
        return await run_in_threadpool(jobs.submit, bytes(data), filename, actor=user.subject, roles=user.roles, key=idempotency_key)

    @app.get('/v1/jobs')
    def history(user: User, limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        return {'items': repository.history(user.subject, user.roles, limit, offset), 'limit': limit, 'offset': offset}

    @app.get('/v1/jobs/{job_id}')
    def status(job_id: str, user: User):
        with repository.engine.connect() as c:
            row = repository.job(c, job_id, user.subject, user.roles)
            return {k: row[k] for k in ('id', 'state', 'revision', 'review_state', 'error', 'attempt', 'heartbeat_at')}

    @app.get('/v1/jobs/{job_id}/report')
    def report(job_id: str, user: User):
        return repository.report(job_id, user.subject, user.roles)

    @app.get('/v1/jobs/{job_id}/revisions/{revision}')
    def revision_report(job_id: str, revision: int, user: User):
        return repository.report(job_id, user.subject, user.roles, revision=revision)

    @app.get('/v1/jobs/{job_id}/document')
    def download(job_id: str, user: User):
        with repository.engine.connect() as c:
            job = repository.job(c, job_id, user.subject, user.roles)
            doc = repository.document(c, job)
        if not storage.path(doc['sha256']).is_file():
            raise HTTPException(410, 'Document expired under the configured retention policy.')
        return FileResponse(storage.path(doc['sha256']), filename=doc['filename'], media_type='application/octet-stream', content_disposition_type='attachment')

    @app.get('/v1/jobs/{job_id}/events')
    async def event_stream(job_id: str, request: Request, user: User, after: int = Query(default=0, ge=0),
                           last_event_id: Annotated[str | None, Header()] = None):
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(422, 'Invalid Last-Event-ID.') from None
        # Authorization happens before response headers are sent and is rechecked while streaming.
        await run_in_threadpool(repository.replay, job_id, user.subject, user.roles, after)
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                if user.subject not in security.allowed_users:
                    return
                entries = await run_in_threadpool(repository.replay, job_id, user.subject, security.allowed_users[user.subject], cursor)
                for entry in entries:
                    cursor = entry['seq']
                    yield f'id: {cursor}\nevent: stage\ndata: {json.dumps(entry)}\n\n'
                with repository.engine.connect() as c:
                    job = repository.job(c, job_id, user.subject, user.roles)
                if job['state'] in {'completed', 'retry_required'} and not entries:
                    return
                yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/v1/jobs/{job_id}/corrections')
    def correct(job_id: str, body: CorrectionCommand, user: Mutator, idempotency_key: Key):
        return reviews.correct(job_id, actor=user.subject, roles=user.roles, key=idempotency_key, **body.model_dump())

    @app.post('/v1/jobs/{job_id}/approve')
    def approve(job_id: str, body: ReviewCommand, user: Mutator, idempotency_key: Key):
        return reviews.act(job_id, actor=user.subject, roles=user.roles, key=idempotency_key, approve=True, **body.model_dump())

    @app.post('/v1/jobs/{job_id}/reject')
    def reject(job_id: str, body: ReviewCommand, user: Mutator, idempotency_key: Key):
        return reviews.act(job_id, actor=user.subject, roles=user.roles, key=idempotency_key, approve=False, **body.model_dump())

    @app.post('/v1/jobs/{job_id}/retry')
    def retry(job_id: str, body: RetryCommand, user: Mutator, idempotency_key: Key):
        return jobs.retry(job_id, actor=user.subject, roles=user.roles, key=idempotency_key, revision=body.revision)

    @app.get('/v1/admin/procurement')
    def procurement(user: User):
        if 'admin' not in user.roles:
            raise PermissionError()
        with repository.engine.begin() as c:
            repository.lock(c)
            snapshot = repository.snapshot(c)
            return dict(revision=snapshot.revision, vendors=snapshot.vendors(), pos=snapshot.pos(),
                        balances=dict(snapshot.balances), policy=repository.policy(c).model_dump(mode='json'))

    @app.put('/v1/admin/{kind}')
    def administer(kind: str, body: AdminCommand, user: Mutator, idempotency_key: Key):
        if 'admin' not in user.roles:
            raise PermissionError()
        validators = {'vendor': Vendor, 'po': PurchaseOrder, 'policy': Policy}
        if kind not in validators:
            raise HTTPException(404, 'Unknown procurement resource.')
        if not body.reason.strip():
            raise ValueError('A recorded reason is required.')
        data = validators[kind].model_validate(body.data).model_dump(mode='json')
        if kind == 'policy' and data['matching_adapter'] != 'rapidfuzz':
            raise ValueError('Production requires rapidfuzz matching.')
        def action(c, company):
            if company['revision'] != body.revision:
                raise Conflict('Procurement revision changed.')
            repository.administer(c, kind, data)
            repository.audit(c, user.subject, 'administer_'+kind, 'company', dict(before_revision=body.revision, data=data, reason=body.reason))
            return {'revision': body.revision+1}
        return repository.command(user.subject, idempotency_key, dict(action='administer', kind=kind, **body.model_dump()), action)

    # The same UI contract is backed by durable production services and OIDC.
    # No local SQLite worker or unauthenticated local middleware is mounted here.
    from .local_api import create_app as operator_app
    from .production_workspace import ProductionWorkspace
    workspace = ProductionWorkspace(repository, storage)
    app.state.workspace = workspace
    async def operator_guard(request: Request):
        if not request.session.get('subject') and 'text/html' in request.headers.get('accept','') and not request.url.path.startswith('/api/'):
            raise HTTPException(307, headers={'Location':'/auth/login'})
        user = current_user(request)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            mutation(request, user)
        request.state.operator = user
    shell = operator_app(workspace, start_worker=False)
    app.include_router(shell.router, dependencies=[Depends(operator_guard)])
    from pydantic import ValidationError
    from fastapi.exceptions import RequestValidationError
    for kind in (ValidationError, RequestValidationError):
        app.add_exception_handler(kind, shell.exception_handlers[kind])
    return app


def app_factory():
    import os
    from pathlib import Path
    from invoicedesk.infrastructure.repository import Repository
    from invoicedesk.infrastructure.storage import LocalStorage
    return create_app(Repository(os.environ['DATABASE_URL']), LocalStorage(Path(os.environ['DOCUMENT_ROOT'])), SecuritySettings.from_env())
