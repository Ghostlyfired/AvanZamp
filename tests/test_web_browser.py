"""Real Chromium DOM acceptance; all HTTP responses below are synthetic and offline."""
import html
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_operator_views_in_chromium(tmp_path):
    chrome = os.getenv('INVOICEDESK_TEST_CHROME') or shutil.which('chromium') or shutil.which('google-chrome')
    if not chrome:
        candidate = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        chrome = str(candidate) if candidate.is_file() else None
    if not chrome:
        pytest.skip('Chromium is required for browser acceptance.')
    source = (Path(__file__).resolve().parents[1]/'invoicedesk/interfaces/web/app.js').read_text(encoding='utf-8')
    # Exercise actual production rendering functions without its automatic initial
    # navigation: this test document is file:// and has no application route.
    source = source.rsplit('\n(async()=>', 1)[0]
    checks = r"""
    (async()=>{
      const assert=(condition,message)=>{if(!condition)throw new Error(message);};
      assert(sourceVerification({verification:'processing'}).includes('Counts unavailable'),'Legacy reports cannot display undefined counts');
      assert(sourceVerification({grounded_count:4,unverified_count:2})==='4 verified · 2 unverified','Source counts are displayed accurately');
      let response={vendor_id:'V-023'},ok=true,request;
      window.fetch=async(url,options)=>{request={url,options};return {ok,json:async()=>response};};
      config={csrf_token:'csrf-test'};
      await vendorDialog();
      const id=document.querySelector('input[name="vendor_id"]');
      assert(id.value==='V-023' && !id.readOnly,'New vendor ID must be populated and editable');
      assert(request.url.endsWith('/vendors/reserve-id'),'Vendor ID comes from server');
      assert(request.options.headers.get('X-CSRF-Token')==='csrf-test','Authenticated mutations include CSRF');
      id.value='MY-CUSTOM-ID';assert(id.value==='MY-CUSTOM-ID','Custom vendor ID supported');dialog.close();
      const fields={section:'invoice_details',status:'PARSED',arithmetic_status:'PASS',verification_status:'GROUNDED',
        fields:{invoice_number:{value:'INV-1',status:'PARSED'},sender:{value:'{"name":"Supplier <script>"}',status:'PARSED'}}};
      const panel=sectionPanel({sections:[fields]});main.replaceChildren(panel);
      assert(main.textContent.includes('Invoice Details'),'Section heading must be readable');
      assert(main.textContent.includes('Invoice Number'),'Field labels must be readable');
      assert(main.textContent.includes('Supplier <script>') && !main.querySelector('script'),'Document text is escaped');
      assert(!main.textContent.includes('[object Object]') && !main.textContent.includes('{"name"'),'No JSON/object leakage');
      response={detail:[{loc:['body','vendor_id'],msg:'Field required'}]};ok=false;
      try {await api('/vendors');throw new Error('Expected error');}catch(error){
        assert(error.message.includes('Vendor ID') && error.message.includes('Field required'),'Readable structured API errors');
      }
      ok=true;
      response={id:'invoice',state:'completed',revision:1,status:'Needs review',invoice_number:'INV-1',filename:'invoice.pdf',
        media_type:'application/pdf',document_available:false,events:[],activity:[],attempts:[],review_available:false,
        report:{summary_reasons:[],sections:[fields],normalized:{currency:'USD'},checks:[],
          decision:{outcome:'NEEDS_REVIEW',summary:'Additional charge needs review.',reasons:['Delivery fee is not authorized.'],recommended_action:'Obtain an amended PO.'}}};
      await renderInvoice('invoice',0);
      assert(main.textContent.includes('Delivery fee is not authorized.'),'Empty presentation reasons must retain actual reasons');
      assert(main.textContent.includes('Obtain an amended PO.'),'Next action must be displayed');
      response.report.sections[0].fields.invoice_number.value='INV-UPDATED';
      await renderInvoice('invoice',0,true);
      assert(main.textContent.includes('INV-UPDATED'),'Polling must update sections even when revision and decision are unchanged');
      const coverage=fulfillmentView({fulfillment_status:'Partially fulfilled',fulfillment_percent:'50.00',approved_amount:'100',remaining_amount:'100',currency:'USD'});
      assert(coverage.textContent.includes('50.00% fulfilled'),'PO coverage is visible');
      const editor=poLinesEditor([{description:'Services',quantity:'2',unit_price:'50',amount:'100'}]);
      assert(editor.read().line_items[0].amount==='100','PO line money remains decimal text');
      document.querySelector('#result').textContent='PASS';
    })().catch(error=>{document.querySelector('#result').textContent='FAIL: '+error.stack;});
    """
    fixture = tmp_path/'browser.html'
    fixture.write_text('<!doctype html><html><body><main id="main"></main><dialog id="dialog"></dialog>'
                       '<div id="notice"></div><output id="result">PENDING</output><script>'
                       + source + '\n' + checks + '</script></body></html>', encoding='utf-8')
    result = subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-sandbox',
                             '--disable-background-networking', '--disable-extensions', '--no-first-run',
                             '--no-default-browser-check', '--host-resolver-rules=MAP * ~NOTFOUND',
                             '--user-data-dir='+str(tmp_path/'chrome'), '--virtual-time-budget=3000',
                             '--dump-dom', fixture.as_uri()], capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=40)
    match = re.search(r'<output id="result">(.*?)</output>', result.stdout, re.S)
    assert match and html.unescape(match.group(1)) == 'PASS', (html.unescape(match.group(1)) if match else result.stderr[-2000:])
