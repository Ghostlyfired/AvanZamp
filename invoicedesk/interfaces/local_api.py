"""Local-only testing UI. Run with ``make ui``; PostgreSQL/OIDC API is separate."""
from contextlib import asynccontextmanager
from pathlib import Path
import os
import sqlite3

from fastapi import APIRouter, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from invoicedesk.domain.approval import ApprovalBlocked, Conflict
from invoicedesk.infrastructure.documents import MAX_BYTES
from invoicedesk.interfaces.local_workspace import Workspace

WEB = Path(__file__).with_name('web')


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    reason: str = Field(default='',max_length=4000)
    confirmed_source: bool = False


class Deletion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    confirmation: str


class VendorData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    vendor_id: str = Field(min_length=1,max_length=100)
    name: str = Field(min_length=1,max_length=300)
    aliases: list[str] = Field(default_factory=list)
    status: str = 'approved'
    notes: str = Field(default='',max_length=4000)
    revision: int | None = Field(default=None,ge=1)


class POData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    po_number: str = Field(min_length=1,max_length=100)
    vendor_id: str = Field(min_length=1,max_length=100)
    description: str = Field(min_length=1,max_length=4000)
    net_amount: str
    tax_rate: str = '0'
    currency: str = Field(min_length=3,max_length=3)
    status: str = 'open'
    issue_date: str
    line_items: list[dict] = Field(default_factory=list)
    charge_allowances: list[dict] = Field(default_factory=list)
    revision: int | None = Field(default=None,ge=1)


def page(rows, q='', page=1, page_size=25):
    if q:
        needle = q.casefold()
        rows = [r for r in rows if needle in ' '.join(str(v) for v in r.values() if v is not None).casefold()]
    return dict(items=rows[(page-1)*page_size:page*page_size],total=len(rows),page=page,page_size=page_size)


def create_app(workspace: Workspace | None = None, *, start_worker=True,
               allowed_hosts=None, allowed_origins=None, demo_notice=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.workspace = workspace or Workspace()
        if start_worker:
            app.state.workspace.start()
        try:
            yield
        finally:
            if start_worker:
                await run_in_threadpool(app.state.workspace.close)

    app = FastAPI(title='Acme Operations · InvoiceDesk local',lifespan=lifespan,docs_url=None,redoc_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=allowed_hosts or ['127.0.0.1','localhost','testserver'])

    @app.middleware('http')
    async def local_guard(request: Request, call_next):
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin = request.headers.get('origin')
            origins = allowed_origins if allowed_origins is not None else {f'http://{request.headers.get("host")}',f'https://{request.headers.get("host")}'}
            if (origin or allowed_origins is not None) and origin not in origins:
                return JSONResponse({'error':{'code':'origin_rejected','message':'Use the local application to submit changes.'}},status_code=403)
            if request.url.path=='/api/v1/invoices/upload':
                try:
                    length = int(request.headers.get('content-length',''))
                except ValueError:
                    return JSONResponse({'error':{'code':'length_required','message':'Upload requires Content-Length.'}},status_code=411)
                if length>MAX_BYTES+128*1024 or length<=0:
                    return JSONResponse({'error':{'code':'too_large','message':'Upload limit is 20 MB.'}},status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({'error':{'code':'validation_error','message':'Check the submitted fields.','fields':[dict(field='.'.join(map(str,e['loc'])),message=e['msg']) for e in exc.errors()]}},status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({'error':{'code':'http_error','message':str(exc.detail)}},status_code=exc.status_code)

    def exception_handler(status, code):
        async def handle(request, exc):
            return JSONResponse({'error':{'code':code,'message':str(exc)}},status_code=status)
        return handle
    for kind,status,code in [(Conflict,409,'conflict'),(ApprovalBlocked,409,'approval_blocked'),(LookupError,404,'not_found'),(ValueError,422,'validation_error'),(sqlite3.IntegrityError,409,'record_conflict')]:
        app.add_exception_handler(kind,exception_handler(status,code))

    def ws(request):
        workspace = request.app.state.workspace
        return workspace.bind(request.state.operator) if hasattr(workspace,'bind') else workspace

    @app.get('/api/v1/config')
    def config(request: Request):
        if hasattr(ws(request), 'config'):
            return dict(ws(request).config(), csrf_token=request.session.get('csrf',''))
        return dict(company='Acme Operations Inc',mode='live',provider=os.getenv('LLM_PROVIDER','openai'),model=os.getenv('LLM_MODEL',''),
                    operator='Demo judge' if demo_notice else 'Local operator',workspace='shared demo' if demo_notice else 'private/ui',
                    upload_limit_bytes=MAX_BYTES,demo_notice=demo_notice)

    @app.get('/api/v1/invoices')
    def invoices(request: Request,q: str='',status: str='',page_number: int=Query(1,alias='page',ge=1),page_size: int=Query(25,ge=1,le=100)):
        rows = ws(request).invoices()
        if status:
            rows = [r for r in rows if r['status']==status]
        return page(rows,q,page_number,page_size)

    @app.post('/api/v1/invoices/upload',status_code=202)
    async def upload(request: Request,file: UploadFile=File(...),key: str=Header(...,alias='Idempotency-Key')):
        content = await file.read(MAX_BYTES+1)
        await file.close()
        if len(content)>MAX_BYTES:
            raise HTTPException(413,detail='Upload limit is 20 MB.')
        return await run_in_threadpool(ws(request).submit,file.filename or '',content,key)

    @app.get('/api/v1/invoices/{identity}')
    def invoice(request: Request,identity: str):
        return ws(request).detail(identity)

    @app.get('/api/v1/invoices/{identity}/events')
    def events(request: Request,identity: str,after: int=Query(0,ge=0)):
        return {'items':[e for e in ws(request).events(identity) if e['seq']>after]}

    @app.get('/api/v1/invoices/{identity}/report')
    def report(request: Request,identity: str):
        return {'report':ws(request).detail(identity)['report']}

    @app.get('/api/v1/invoices/{identity}/document')
    def document(request: Request,identity: str):
        job = ws(request).job(identity)
        if not job['document_path'] or not Path(job['document_path']).is_file():
            raise LookupError('Historical source document is unavailable.')
        return FileResponse(job['document_path'],media_type=job['media_type'],filename=job['filename'],content_disposition_type='inline')

    @app.post('/api/v1/invoices/{identity}/recheck')
    def recheck(request: Request,identity: str,body: Action,key: str=Header(...,alias='Idempotency-Key')):
        return ws(request).recheck(identity,body.revision,key)

    @app.post('/api/v1/invoices/{identity}/approve')
    def approve(request: Request,identity: str,body: Action,key: str=Header(...,alias='Idempotency-Key')):
        return ws(request).review(identity,True,body.revision,body.reason,body.confirmed_source,key)

    @app.post('/api/v1/invoices/{identity}/reject')
    def reject(request: Request,identity: str,body: Action,key: str=Header(...,alias='Idempotency-Key')):
        return ws(request).review(identity,False,body.revision,body.reason,body.confirmed_source,key)

    @app.post('/api/v1/invoices/{identity}/retry',status_code=202)
    def retry(request: Request,identity: str,body: Action,key: str=Header(...,alias='Idempotency-Key')):
        return ws(request).retry(identity,body.revision,key)

    @app.post('/api/v1/vendors/reserve-id')
    def reserve_vendor_id(request: Request,key: str=Header(...,alias='Idempotency-Key')):
        return ws(request).reserve_vendor_id(key)

    def management_router(entity):
        router = APIRouter(prefix='/api/v1/'+entity)
        @router.get('')
        def listing(request: Request,q: str='',status: str='',vendor_id: str='',page_number: int=Query(1,alias='page',ge=1),page_size: int=Query(25,ge=1,le=100)):
            rows = ws(request).records(entity)
            if status:
                rows = [r for r in rows if r['status']==status]
            if vendor_id:
                rows = [r for r in rows if r['vendor_id']==vendor_id]
            return page(rows,q,page_number,page_size)
        @router.post('',status_code=201)
        def create(request: Request,body: dict,key: str=Header(...,alias='Idempotency-Key')):
            parsed = (VendorData if entity=='vendors' else POData).model_validate(body)
            return ws(request).save_record(entity,parsed.model_dump(),key)
        @router.get('/{identity}')
        def detail(request: Request,identity: str):
            return ws(request).record(entity,identity)
        @router.put('/{identity}')
        def update(request: Request,identity: str,body: dict,key: str=Header(...,alias='Idempotency-Key')):
            parsed = (VendorData if entity=='vendors' else POData).model_validate(body)
            field = 'vendor_id' if entity=='vendors' else 'po_number'
            if getattr(parsed,field) != identity:
                raise ValueError('The record ID cannot change.')
            return ws(request).save_record(entity,parsed.model_dump(),key,identity)
        @router.delete('/{identity}')
        def delete(request: Request,identity: str,body: Deletion,key: str=Header(...,alias='Idempotency-Key')):
            return ws(request).delete_record(entity,identity,body.revision,body.confirmation,key)
        if entity=='vendors':
            @router.post('/{identity}/block')
            def block(request: Request,identity: str,body: Action,key: str=Header(...,alias='Idempotency-Key')):
                return ws(request).toggle_vendor(identity,True,body.revision,body.reason,key)
            @router.post('/{identity}/unblock')
            def unblock(request: Request,identity: str,body: Action,key: str=Header(...,alias='Idempotency-Key')):
                return ws(request).toggle_vendor(identity,False,body.revision,body.reason,key)
        return router
    app.include_router(management_router('vendors'))
    app.include_router(management_router('purchase-orders'))

    @app.get('/api/v1/activity')
    def activity(request: Request,entity: str='',entity_id: str='',action: str='',start: str='',end: str='',page_number: int=Query(1,alias='page',ge=1),page_size: int=Query(25,ge=1,le=100)):
        return page(ws(request).activity(entity=entity,entity_id=entity_id,action=action,start=start,end=end),page=page_number,page_size=page_size)

    @app.get('/api/v1/developer/runs')
    def developer_runs(request: Request,q: str='',page_number: int=Query(1,alias='page',ge=1),page_size: int=Query(25,ge=1,le=100)):
        return page(ws(request).invoices(),q,page_number,page_size)

    @app.get('/api/v1/developer/runs/{identity}')
    def developer_artifacts(request: Request,identity: str):
        return ws(request).developer(identity)

    app.mount('/assets',StaticFiles(directory=WEB),name='assets')
    @app.get('/{path:path}')
    def frontend(path: str):
        if path.startswith('api/'):
            raise HTTPException(404,'Unknown API route.')
        return FileResponse(WEB/'index.html')
    return app


app = create_app()


def main(argv=None):
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description='Launch the local InvoiceDesk UI.')
    parser.add_argument('--clean-invoices',action='store_true',help='Back up and clear invoice history and ledger postings, preserving vendors and purchase orders.')
    args = parser.parse_args(argv)
    workspace = Workspace()
    if args.clean_invoices:
        try:
            result = workspace.clean_invoices()
        except RuntimeError as exc:
            parser.exit(2,str(exc)+'\n')
        print('Invoices cleared; vendors and purchase orders preserved. Backup: '+result['backup'],flush=True)
    uvicorn.run(create_app(workspace),host='127.0.0.1',port=8000,workers=1)


if __name__=='__main__':
    main()
