'use strict';
const main = document.querySelector('#main');
const dialog = document.querySelector('#dialog');
let config = {}, current = null, routeVersion = 0, pollBusy = false;
let activeTab = 'sections', timer;
const labels = {'invoices':'Invoices','purchase-orders':'Purchase orders','vendors':'Vendors','activity':'Activity','developer':'Developer'};
const can = (...roles) => !config.roles || config.roles.some(role=>roles.includes(role));
const available = value => value === null || value === undefined || value === '' ? 'Unavailable' : typeof value==='object' ? 'See details' : String(value);
const dateText = value => {
  if(!value)return 'Unavailable';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?'Unavailable':date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});
};
// All document, model, and operator text goes through textContent / text nodes.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(),value);
    else if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = available(value);
    else if (key === 'value') node.value = value ?? '';
    else if (key === 'checked' || key === 'disabled' || key === 'required' || key === 'readOnly') node[key] = value;
    else if (value !== null && value !== undefined) node.setAttribute(key,String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : typeof child==='object' ? valueView(child) : document.createTextNode(String(child)));
  }
  return node;
}
function button(text, onClick, cls = '') { return el('button',{type:'button',class:cls,onClick},text); }
function stateLabel(value) {
  const text=available(value);
  const outcomes={AUTO_APPROVE:'Approved by checks',NEEDS_REVIEW:'Needs review',HOLD_MISSING_INFO:'Missing information',REJECT:'Rejected by checks'};
  if(outcomes[text])return outcomes[text];
  return ['approved','blocked','open','closed','pending','queued','running','completed','errored'].includes(text.toLowerCase()) ? text[0].toUpperCase()+text.slice(1).toLowerCase() : text;
}
function badge(text) {
  const state=available(text),key=state.toLowerCase();
  const tone=['rejected','errored','fail','invalid','blocked','closed'].includes(key)?'red':
    ['needs review','hold_missing_info','partial','unverified','missing','warn','review recommended','missing information','rejection recommended'].includes(key)?'amber':
    ['approved','pass','verified','grounded','parsed','open','approval recommended'].includes(key)?'teal':
    ['processing','started','running','queued'].includes(key)?'blue':'';
  return el('span',{class:'badge '+tone},stateLabel(state));
}
function humanLabel(value) {
  return String(value).replace(/_json$/, '').split(/[_.]/).map(word=>({po:'PO',id:'ID',usd:'USD',sha256:'SHA-256',ms:'ms'}[word] || word.charAt(0).toUpperCase()+word.slice(1))).join(' ');
}
function structuredValue(value) {
  if(typeof value!=='string' || !/^[\[{]/.test(value.trim()))return value;
  try {const parsed=JSON.parse(value);return parsed && typeof parsed==='object'?parsed:value;}catch{return value;}
}
function valueView(value) {
  value=structuredValue(value);
  if(value===null || value===undefined)return el('span',{class:'muted'},'Unavailable');
  if(typeof value==='boolean')return el('span',{},value?'Yes':'No');
  if(Array.isArray(value)) {
    if(!value.length)return el('span',{class:'muted'},'None');
    if(value.every(row=>row && typeof row==='object' && !Array.isArray(row) && Object.values(row).every(part=>part===null || typeof part!=='object'))) {
      const keys=[...new Set(value.flatMap(row=>Object.keys(row)))];
      return makeTable(keys.map(humanLabel),value.map(row=>tableRow(keys.map(key=>valueView(row[key])))));
    }
    return el('ul',{class:'structured-list'},value.map(part=>el('li',{},valueView(part))));
  }
  if(typeof value==='object')return el('dl',{class:'structured-fields'},Object.entries(value).map(([key,part])=>{
    if(key.endsWith('_json') && typeof part==='string') {try{part=JSON.parse(part);}catch{}}
    return el('div',{},el('dt',{},humanLabel(key)),el('dd',{},valueView(part)));
  }));
  return el('span',{class:'rendered-value'},stateLabel(String(value)));
}
function detailsView(value,title='Details') {return el('details',{},el('summary',{},title),valueView(value));}
function jsonView(value, title = 'Raw JSON') { return el('details',{},el('summary',{},title),el('pre',{text:JSON.stringify(value,null,2)})); }
function sourceVerification(metadata) {
  return Number.isInteger(metadata?.grounded_count) && Number.isInteger(metadata?.unverified_count)
    ? metadata.grounded_count+' verified · '+metadata.unverified_count+' unverified'
    : 'Counts unavailable; recheck saved extraction.';
}
function fact(label, value) {
  const parsed=structuredValue(value);
  return el('div',{class:'fact'},el('span',{class:'fact-label'},label),typeof parsed==='object' && parsed!==null?valueView(parsed):el('strong',{text:parsed}));
}
function notify(message) {
  const node = document.querySelector('#notice'); node.textContent = message;
  clearTimeout(timer); timer = setTimeout(()=>node.textContent='',6000);
}
async function api(path, options = {}) {
  const headers=new Headers(options.headers);
  if(config.csrf_token && options.method && !['GET','HEAD','OPTIONS'].includes(options.method.toUpperCase()))headers.set('X-CSRF-Token',config.csrf_token);
  const response = await fetch('/api/v1'+path,{...options,headers,credentials:'same-origin'});
  let body;
  try { body = await response.json(); } catch { throw new Error('The server returned an unreadable response.'); }
  if (!response.ok) {
    const error = body.error;
    const details=error?.fields || (Array.isArray(body.detail)?body.detail:[]);
    const message=error?.message || (typeof body.detail==='string'?body.detail:'Unable to complete the request.');
    throw new Error(message+(details.length?'\n'+details.map(field=>humanLabel(field.field || (field.loc || []).join('.'))+': '+(field.message || field.msg || 'Invalid value')).join('\n'):''));
  }
  return body;
}
function mutate(path, body, key, method = 'POST') {
  return api(path,{method,headers:{'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify(body)});
}
function navigate(path, replace = false) {
  if (replace) history.replaceState({},'',path); else history.pushState({},'',path);
  renderRoute();
}
document.addEventListener('click',event=>{
  const anchor = event.target.closest('a');
  if (!anchor || anchor.target==='_blank' || anchor.hasAttribute('download') || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
  const url = new URL(anchor.href,location.href);
  if (url.origin !== location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/assets/') || url.pathname.startsWith('/auth/')) return;
  event.preventDefault(); navigate(url.pathname+url.search);
});
window.addEventListener('popstate',renderRoute);
function pageHead(title, subtitle, action) {
  return el('div',{class:'page-head'},el('div',{},el('div',{class:'eyebrow'},'Accounts payable'),el('h1',{},title),el('p',{class:'muted'},subtitle)),action);
}
function empty(text = 'No records match your filters.') { return el('div',{class:'empty'},el('strong',{},'Nothing here yet'),text); }
function makeTable(headers, rows) {
  return el('div',{class:'card table-card'},el('table',{},el('thead',{},el('tr',{},headers.map(h=>el('th',{},h)))),el('tbody',{},rows)));
}
function tableRow(values) { return el('tr',{},values.map(value=>el('td',{},value))); }
function detailLink(section,id) { return el('a',{class:'arrow',href:'/'+section+'/'+encodeURIComponent(id),'aria-label':'Open detail'},'→'); }
function selectFilter(label, options, value, change) {
  const input = el('select',{'aria-label':label,onChange:()=>change(input.value)},el('option',{value:''},label),options.map(option=>el('option',{value:typeof option==='string'?option:option.value},typeof option==='string'?stateLabel(option):option.label)));
  input.value = value || ''; return input;
}
async function allRecords(entity) {
  let rows = [], page = 1, total = 1;
  while (rows.length < total) {
    const result = await api('/'+entity+'?page_size=100&page='+page++); rows.push(...result.items); total=result.total;
  }
  return rows;
}
function makeDialog(title, description, fields, submitLabel, onSubmit, danger=false) {
  dialog.replaceChildren();
  const error = el('div',{class:'form-error',role:'alert'});
  const submit = el('button',{type:'submit',class:danger?'danger':'primary'},submitLabel);
  const form = el('form',{},el('h2',{id:'dialog-title'},title),el('p',{class:'muted'},description),el('div',{class:'form-fields'},fields),error,
    el('div',{class:'dialog-actions'},button('Cancel',()=>dialog.close()),submit));
  const key = crypto.randomUUID();
  form.addEventListener('submit',async event=>{
    event.preventDefault(); if (!form.reportValidity()) return;
    submit.disabled = true; error.textContent='';
    try { await onSubmit(new FormData(form),key); dialog.close(); }
    catch (err) { error.textContent = err.message; }
    finally { submit.disabled = false; }
  });
  dialog.append(form); dialog.showModal();
}
function inputField(title,name,value='',extra={}) {
  return el('label',{},title,el('input',{name,value,...extra}));
}
function textareaField(title,name,value='',required=false) { return el('label',{},title,el('textarea',{name,required},value)); }
function poLinesEditor(lines=[], allowances=[]) {
  const items=el('div',{class:'po-editor-rows'}),charges=el('div',{class:'po-editor-rows'});
  const addRow=(container,row,fields)=>{
    const inputs=fields.map(([key,title])=>inputField(title,key,row[key] ?? '',{inputmode:key==='description' || key==='part_number'?'text':'decimal'}));
    const element=el('div',{class:'po-editor-row'},inputs,button('Remove',()=>element.remove()));
    container.append(element);
  };
  const itemFields=[['description','Description'],['part_number','Part number'],['quantity','Quantity'],['unit_price','Unit price'],['amount','Net amount']];
  const chargeFields=[['description','Charge description'],['amount','Allowed amount per invoice']];
  for(const line of lines)addRow(items,line,itemFields);
  for(const charge of allowances)addRow(charges,charge,chargeFields);
  const read=container=>[...container.children].map(row=>Object.fromEntries([...row.querySelectorAll('input')].map(input=>[input.name,input.value.trim() || null])));
  return {node:el('section',{class:'po-lines-editor'},el('h3',{},'Ordered items (optional)'),el('p',{class:'inline-help'},'Item details enable quantity, rate and description checks.'),items,button('+ Add ordered item',()=>addRow(items,{},itemFields)),
    el('h3',{},'Approved additional charges (optional)'),charges,button('+ Add charge allowance',()=>addRow(charges,{},chargeFields))),
    read:()=>({line_items:read(items),charge_allowances:read(charges)})};
}
async function uploadDialog() {
  const input = el('input',{name:'document',type:'file',accept:'.pdf,.png,.jpg,.jpeg,.webp,application/pdf,image/png,image/jpeg,image/webp',required:true});
  makeDialog('Upload an invoice','Add a document to the processing queue. You can keep working while extraction runs.',[
    el('div',{class:'callout'},el('strong',{},'Live extraction'),el('div',{},available(config.provider)+' · '+available(config.model)),el('small',{},'Every uploaded invoice is sent to the configured model for structured extraction.')),
    el('label',{class:'upload-zone'},'Choose a document',input,el('span',{class:'inline-help'},'PDF, PNG, JPEG or WebP · up to 20 MB'))
  ],'Upload and process',async (form,key)=>{
    const file = form.get('document');
    if (!file.size || file.size>20*1024*1024) throw new Error('Choose a non-empty document of at most 20 MB.');
    const body = new FormData(); body.append('file',file);
    const job = await api('/invoices/upload',{method:'POST',headers:{'Idempotency-Key':key},body});
    notify('Document queued. Processing continues if you leave this page.'); navigate('/invoices/'+job.id);
  });
}
async function vendorDialog(record = null) {
  let vendorId=record?.vendor_id;
  if(!record) {
    try {vendorId=(await mutate('/vendors/reserve-id',{},crypto.randomUUID())).vendor_id;}
    catch(error) {notify('Unable to prepare a vendor ID. '+error.message);return;}
  }
  makeDialog(record?'Edit vendor':'New vendor','Approved vendor details are used again whenever an invoice is approved.',[
    inputField('Vendor ID','vendor_id',vendorId,{required:true,pattern:'[A-Za-z0-9_\\-]+',maxLength:100,readOnly:!!record}),
    !record?el('p',{class:'inline-help'},'A unique ID has been generated. You can enter a custom ID before saving.'):null,
    inputField('Vendor name','name',record?.name,{required:true,maxLength:300}),
    textareaField('Aliases (one per line)','aliases',record?.aliases?.join('\n') || ''),
    textareaField('Notes','notes',record?.notes || '')
  ],record?'Save changes':'Create vendor',async (form,key)=>{
    const payload = {vendor_id:form.get('vendor_id'),name:form.get('name'),aliases:String(form.get('aliases')).split('\n').map(s=>s.trim()).filter(Boolean),notes:form.get('notes'),status:record?.status || 'approved',revision:record?.revision};
    const result = await mutate('/vendors'+(record?'/'+encodeURIComponent(record.vendor_id):''),payload,key,record?'PUT':'POST');
    notify('Vendor saved.'); navigate('/vendors/'+encodeURIComponent(result.vendor_id));
  });
}
async function poDialog(record = null) {
  try {
    const vendors = await allRecords('vendors');
    const linesEditor=poLinesEditor(record?.line_items || [],record?.charge_allowances || []);
    const vendorSelect = el('select',{name:'vendor_id',required:true},el('option',{value:''},'Select a vendor'),vendors.map(v=>el('option',{value:v.vendor_id},v.name+' ('+v.vendor_id+')')));
    vendorSelect.value=record?.vendor_id || '';
    const state = el('select',{name:'status'},['open','closed'].map(s=>el('option',{value:s},stateLabel(s))));
    state.value = record?.status || 'open';
    makeDialog(record?'Edit purchase order':'New purchase order','PO amounts are net of tax. Receipt activity and authorized postings are tracked separately.',[
      inputField('PO number','po_number',record?.po_number,{required:true,pattern:'[A-Za-z0-9_\\-]+',maxLength:100,readOnly:!!record}),
      el('label',{},'Vendor',vendorSelect),textareaField('Description','description',record?.description || '',true),
      el('div',{class:'grid2'},inputField('Net amount','net_amount',record?.net_amount,{required:true,inputmode:'decimal',pattern:'[0-9]+([.][0-9]{1,6})?'}),inputField('Currency','currency',record?.currency || 'USD',{required:true,pattern:'[A-Za-z]{3}',maxLength:3})),
      el('div',{class:'grid2'},inputField('Tax rate (decimal, e.g. 0.08)','tax_rate',record?.tax_rate || '0',{required:true,inputmode:'decimal'}),inputField('Issue date','issue_date',record?.issue_date || new Date().toISOString().slice(0,10),{required:true,type:'date'})),
      el('label',{},'Procurement state',state),linesEditor.node
    ],record?'Save changes':'Create purchase order',async (form,key)=>{
      const payload={};
      for(const name of ['po_number','vendor_id','description','net_amount','currency','tax_rate','issue_date','status'])payload[name]=form.get(name);
      Object.assign(payload,linesEditor.read()); if(record) payload.revision=record.revision;
      const result=await mutate('/purchase-orders'+(record?'/'+encodeURIComponent(record.po_number):''),payload,key,record?'PUT':'POST');
      notify('Purchase order saved.'); navigate('/purchase-orders/'+encodeURIComponent(result.po_number));
    });
  } catch(error) { notify(error.message); }
}
function deleteDialog(entity,record) {
  const id = entity==='vendors'?record.vendor_id:record.po_number;
  makeDialog('Delete unused record','Referenced records cannot be deleted. This action remains in activity history.',[
    inputField('Type '+id+' to confirm','confirmation','',{required:true})
  ],'Delete record',async(form,key)=>{
    await mutate('/'+entity+'/'+encodeURIComponent(id),{revision:record.revision,confirmation:form.get('confirmation')},key,'DELETE');
    notify('Unused record deleted.'); navigate('/'+entity);
  },true);
}
function blockDialog(record) {
  const block = record.status!=='blocked';
  makeDialog(block?'Block vendor':'Unblock vendor',block?'Blocking prevents approval of invoices from this vendor.':'Unblocking allows the vendor to pass the vendor status check.',[
    textareaField('Reason (optional)','reason','')
  ],block?'Block vendor':'Unblock vendor',async(form,key)=>{
    await mutate('/vendors/'+encodeURIComponent(record.vendor_id)+(block?'/block':'/unblock'),{revision:record.revision,reason:form.get('reason')},key);
    notify(block?'Vendor blocked.':'Vendor unblocked.'); navigate('/vendors/'+encodeURIComponent(record.vendor_id));
  },block);
}
function reviewDialog(record,action) {
  const approve=action==='approve';
  makeDialog(approve?'Approve invoice':'Reject invoice',approve?'Approval reruns procurement checks against current vendor, PO, duplicate and balance state. It creates one authorized net ledger posting.':'A rejection is recorded with your reason and consumes no PO balance.',[
    textareaField(approve?'Exception reason (required for review exceptions)':'Rejection reason','reason','',!approve),
    ...(approve?[el('label',{class:'checkbox'},el('input',{type:'checkbox',name:'confirmed_source'}),'I checked the printed invoice and its line items against the source document.')]:[])
  ],approve?'Approve and post':'Reject invoice',async(form,key)=>{
    await mutate('/invoices/'+record.id+'/'+action,{revision:record.revision,reason:form.get('reason'),confirmed_source:form.has('confirmed_source')},key);
    notify(approve?'Invoice approved. Net amount posted.':'Invoice rejected.'); navigate('/invoices/'+record.id);
  },!approve);
}
function retryDialog(record) {
  makeDialog('Process a new attempt','The previous attempt and artifacts are preserved. This sends the document to the configured model again.',[], 'Process again',async(form,key)=>{
    const result=await mutate('/invoices/'+record.id+'/retry',{revision:record.revision},key);
    navigate('/invoices/'+result.id);
  });
}
function invoiceRows(items, developer=false) {
  return items.map(row=>tableRow(developer?[
    el('div',{},row.filename,el('span',{class:'sub'},row.run_id || row.id)),dateText(row.created_at),badge(row.status),row.imported?'Imported history':'Local UI',detailLink('developer',row.id)
  ]:[
    el('div',{},available(row.invoice_number || row.filename),el('span',{class:'sub'},row.invoice_number?row.filename:available(row.document_type))),
    row.matched_po?el('a',{href:'/purchase-orders/'+encodeURIComponent(row.matched_po)},row.matched_po):'Unavailable',available(row.issuer),dateText(row.created_at),
    el('div',{class:'money'},available(row.gross_amount)+' '+available(row.currency)),badge(row.status),detailLink('invoices',row.id)
  ]));
}
function fulfillmentView(row) {
  const status=row.fulfillment_status || (row.fully_approved?'Fulfilled':'Unfulfilled');
  const percent=row.fulfillment_percent;
  return el('div',{},badge(status),percent!==null && percent!==undefined?el('span',{class:'sub'},available(percent)+'% fulfilled'):null,
    el('span',{class:'sub'},'Approved: '+row.approved_amount+' '+row.currency),el('span',{class:'sub'},'Remaining: '+row.remaining_amount+' '+row.currency),
    row.accepted_invoice_count!==undefined?el('span',{class:'sub'},row.accepted_invoice_count+' approved invoices'):null,
    row.pending_amount!==undefined?el('span',{class:'sub'},'Pending review: '+row.pending_amount+' '+row.currency):null);
}
async function renderList(section,version) {
  const params=new URLSearchParams(location.search);
  const state={section,page:Number(params.get('page') || 1),q:params.get('q') || '',status:params.get('status') || '',vendor_id:params.get('vendor_id') || '',entity:params.get('entity') || '',action:params.get('action') || '',start:params.get('start') || '',end:params.get('end') || '',version};
  current=state;
  const subtitles={invoices:'Every document, from arrival to authorized approval.','purchase-orders':'Track procurement, invoice receipts and approved net balances.',vendors:'Maintain the vendor details used by your approval checks.',activity:'A chronological record of system and operator actions.',developer:'Stored run diagnostics, extraction provenance and stage events.'};
  const add = section==='invoices' && can('admin','uploader')?button('+ Upload invoice',uploadDialog,'primary'):section==='vendors' && can('admin')?button('+ New vendor',()=>vendorDialog(),'primary'):section==='purchase-orders' && can('admin')?button('+ New PO',()=>poDialog(),'primary'):null;
  const head=pageHead(labels[section],subtitles[section],add);
  const updateFilters=()=>{
    const next=new URLSearchParams();
    for(const key of ['q','status','vendor_id','entity','action','start','end']) if(state[key]) next.set(key,state[key]);
    navigate('/'+section+(next.size?'?'+next:''));
  };
  let debounce;
  const search=el('input',{type:'search',placeholder:'Search '+labels[section].toLowerCase()+'…','aria-label':'Search',value:state.q,onInput:()=>{
    state.q=search.value; state.page=1; clearTimeout(debounce); debounce=setTimeout(()=>{
      const url=new URL(location.href); if(state.q) url.searchParams.set('q',state.q); else url.searchParams.delete('q'); url.searchParams.delete('page'); history.replaceState({},'',url); refreshList(state);
    },300);
  }});
  const toolbar=el('div',{class:'toolbar'},...(section!=='activity'?[search]:[]));
  if(section==='invoices') toolbar.append(selectFilter('All statuses',['Not started','Processing','Needs review','Approved','Rejected','Errored'],state.status,value=>{state.status=value;updateFilters();}));
  if(section==='vendors' || section==='purchase-orders') toolbar.append(selectFilter('All states',section==='vendors'?['approved','blocked']:['open','closed'],state.status,value=>{state.status=value;updateFilters();}));
  if(section==='purchase-orders') {
    const vendors=await allRecords('vendors'); if(version!==routeVersion)return;
    toolbar.append(selectFilter('All vendors',vendors.map(v=>({value:v.vendor_id,label:v.name})),state.vendor_id,value=>{state.vendor_id=value;updateFilters();}));
  }
  if(section==='activity') {
    toolbar.append(selectFilter('All entities',['invoices','purchase-orders','vendors'],state.entity,value=>{state.entity=value;updateFilters();}),
      selectFilter('All actions',['imported','submitted','processing','processed','rechecked','errored','interrupted','approved','rejected','retried','created','updated','blocked','unblocked','deleted'],state.action,value=>{state.action=value;updateFilters();}),
      inputField('From','start',state.start,{type:'date',onChange:event=>{state.start=event.target.value;updateFilters();}}),inputField('Through','end',state.end,{type:'date',onChange:event=>{state.end=event.target.value;updateFilters();}}));
  }
  state.table=el('div'); state.pager=el('div',{class:'pager'});state.metrics=el('div');
  main.replaceChildren(head,state.metrics,toolbar,state.table,state.pager);
  await refreshList(state);
}
async function refreshList(state) {
  const query=new URLSearchParams({page:state.page,page_size:25});
  for(const key of ['q','status','vendor_id','entity','action','start','end']) if(state[key])query.set(key,state[key]);
  const endpoint=state.section==='developer'?'developer/runs':state.section;
  const result=await api('/'+endpoint+'?'+query);
  if(current!==state)return;
  const signature=JSON.stringify(result);
  if(state.lastResult===signature)return;
  state.lastResult=signature;
  let headers,rows;
  if(state.section==='invoices' || state.section==='developer') {
    headers=state.section==='developer'?['Document / run ID','Received','Status','Origin','']:['Invoice / file','Matched PO','Issuer','Received','Gross amount','Status',''];
    rows=invoiceRows(result.items,state.section==='developer');
  } else if(state.section==='vendors') {
    headers=['Vendor','ID','Aliases','State','Activity',''];
    rows=result.items.map(row=>tableRow([row.name,row.vendor_id,row.aliases.join(', ') || '—',badge(row.status),row.invoice_count+' invoices · '+row.po_count+' POs',detailLink('vendors',row.vendor_id)]));
  } else if(state.section==='purchase-orders') {
    headers=['Purchase order','Issued','Vendor','Net amount','State','Receipt activity','Fulfillment',''];
    rows=result.items.map(row=>tableRow([el('div',{},row.po_number,el('span',{class:'sub'},row.description)),row.issue_date,row.vendor_name,row.net_amount+' '+row.currency,badge(row.status),row.receipt_count+' attempts',fulfillmentView(row),detailLink('purchase-orders',row.po_number)]));
  } else {
    headers=['Time','Actor','Entity','Action','Details'];
    rows=result.items.map(row=>tableRow([dateText(row.created_at),row.actor,el('a',{href:'/'+row.entity+'/'+encodeURIComponent(row.entity_id)},row.entity_id),badge(row.action),detailsView(row.details,'View recorded details')]));
  }
  state.table.replaceChildren(result.items.length?makeTable(headers,rows):el('div',{class:'card'},empty()));
  const pages=Math.max(1,Math.ceil(result.total/25));
  const prev=button('← Previous',()=>changePage(state,state.page-1));prev.disabled=state.page<=1;
  const next=button('Next →',()=>changePage(state,state.page+1));next.disabled=state.page>=pages;
  state.pager.replaceChildren(el('span',{class:'count'},result.total+' records'),prev,el('span',{},'Page '+state.page+' of '+pages),next);
}
function changePage(state,value){const url=new URL(location.href);url.searchParams.set('page',value);navigate(url.pathname+url.search);}
function activityTable(rows) {
  return rows.length?makeTable(['Time','Actor','Action','Details'],rows.map(row=>tableRow([dateText(row.created_at),row.actor,badge(row.action),detailsView(row.details,'Recorded details')]))):el('p',{class:'muted'},'No actions recorded.');
}
async function renderRecord(section,id,version) {
  const row=await api('/'+section+'/'+encodeURIComponent(id)); if(version!==routeVersion)return;
  current={section,id,version};
  const isVendor=section==='vendors';
  const content=el('div',{},el('a',{class:'back',href:'/'+section},'← Back to '+labels[section].toLowerCase()),
    pageHead(isVendor?row.name:row.po_number,isVendor?'Vendor master · '+row.vendor_id:row.description,
      can('admin')?button('Edit '+(isVendor?'vendor':'PO'),()=>isVendor?vendorDialog(row):poDialog(row)):null),
    el('div',{class:'card'},el('div',{class:'section-head'},el('h2',{},isVendor?'Vendor particulars':'Purchase order particulars'),badge(row.status)),
      el('div',{class:'fact-grid'},...(isVendor?[fact('Vendor ID',row.vendor_id),fact('Aliases',row.aliases.join(', ') || 'None'),fact('Notes',row.notes || 'None'),fact('Linked activity',row.invoice_count+' invoices · '+row.po_count+' POs')]:[
        fact('Vendor',row.vendor_name),fact('Issued',row.issue_date),fact('Net PO amount',row.net_amount+' '+row.currency),fact('Tax rate',row.tax_rate),fact('Approved net postings',row.approved_amount+' '+row.currency),fact('Remaining net',row.remaining_amount+' '+row.currency),fact('Invoice receipt activity',row.receipt_count+' matched attempts'),el('div',{},el('span',{class:'muted'},'Fulfillment'),fulfillmentView(row)),fact('Net overage',row.overage_amount+' '+row.currency)
      ])),isVendor?(can('admin')?el('div',{class:'action-row'},button(row.status==='blocked'?'Unblock vendor':'Block vendor',()=>blockDialog(row),row.status==='blocked'?'':'danger')):null):el('p',{class:'source-foot'},'Approved invoices determine fulfillment. Pending, rejected and superseded attempts consume no PO balance.')),
    !isVendor && row.line_items?.length?el('section',{class:'card'},el('h2',{},'Ordered items'),valueView(row.line_items)):null,
    !isVendor && row.charge_allowances?.length?el('section',{class:'card'},el('h2',{},'Approved additional charges'),el('p',{class:'inline-help'},'Maximum approved amount per invoice.'),valueView(row.charge_allowances)):null,
    el('h2',{},'Linked invoice attempts'),row.invoices.length?makeTable(['Invoice / file','Matched PO','Issuer','Received','Gross amount','Status',''],invoiceRows(row.invoices)):el('div',{class:'card'},empty('No matched invoice attempts.')),
    el('h2',{class:'all-activity'},'Action history'),activityTable(row.activity),
    can('admin')?el('details',{class:'card danger-zone'},el('summary',{},'Danger section'),el('p',{class:'muted'},'Only unused records can be deleted. Referenced vendors can be blocked; referenced POs can be closed using Edit PO.'),button('Delete unused '+(isVendor?'vendor':'PO'),()=>deleteDialog(section,row),'danger')):null);
  main.replaceChildren(content);
}
function timeline(events) {
  return el('ol',{class:'timeline'},events.map(event=>el('li',{},el('span',{class:'time'},(event.elapsed_ms/1000).toFixed(1)+' s'),el('div',{},el('strong',{},event.title),el('p',{text:event.detail || event.stage})),badge(event.state))));
}
function checksPanel(report) {
  return el('div',{class:'checks'},report.checks.map(check=>el('div',{class:'check'},el('div',{class:'check-head'},el('strong',{},check.title),badge(check.status)),el('p',{},check.detail),
    Object.keys(check.evidence || {}).length?detailsView(check.evidence,'Evidence and check inputs'):null)));
}
function sectionPanel(report) {
  const sections=report.sections || [];
  if(!sections.length)return el('div',{class:'card'},el('p',{class:'muted'},'Section validation was not retained for this historical run.'),detailsView(report.extraction,'Extracted fields'));
  return el('div',{},sections.map(section=>{
    const card=el('section',{class:'card'},el('div',{class:'section-head'},el('h2',{},humanLabel(section.section)),badge(section.status)),
      el('p',{class:'muted'},'Arithmetic ',badge(section.arithmetic_status),'  Source ',badge(section.verification_status)));
    if(section.verification_note)card.append(el('p',{class:'inline-help'},section.verification_note));
    if(section.section==='line_items') {
      const rows=(section.row_validation || []).map(row=>tableRow([
        el('div',{class:'wrap'},available(row.fields.description?.value)),available(row.fields.quantity?.value),available(row.fields.unit_price?.value),available(row.fields.amount?.value),badge(row.status),el('div',{},badge(row.arithmetic_status),row.arithmetic_note?el('span',{class:'sub'},row.arithmetic_note):null),badge(row.verification_status || section.verification_status)
      ]));
      card.append(rows.length?makeTable(['Description','Quantity','Unit rate','Amount','Parsing','Arithmetic','Source'],rows):el('p',{class:'muted'},'No line items available.'));
    } else if(section.section==='evidence') {
      card.append(detailsView(section.fields,'Evidence paths and printed snippets'));
    } else {
      const grid=el('div',{class:'fact-grid'});
      for(const [name,field] of Object.entries(section.fields || {})) {
        if(name==='line_items' || name==='evidence')continue;
        grid.append(el('div',{},fact(humanLabel(name),field.value),el('span',{class:'sub'},badge(field.status))));
      }
      card.append(grid);
    }
    if(section.issues?.length)card.append(detailsView(section.issues,'Validation issues ('+section.issues.length+')'));
    if(section.arithmetic?.length)card.append(detailsView(section.arithmetic,'Arithmetic details'));
    return card;
  }));
}
async function renderInvoice(id,version,refresh=false) {
  const row=await api('/invoices/'+encodeURIComponent(id)); if(version!==routeVersion)return;
  const signature=JSON.stringify(row);
  if(refresh && current?.signature===signature)return;
  current={section:'invoices',id,version,signature};
  const report=row.report;
  const preview=el('div',{class:'preview'});
  const source='/api/v1/invoices/'+encodeURIComponent(id)+'/document';
  if(row.document_available)preview.append(row.media_type==='application/pdf'?el('iframe',{src:source,title:'Invoice source document'}):el('img',{src:source,alt:'Uploaded invoice source'}));
  else preview.append(el('div',{class:'preview-note'},el('strong',{},'Source unavailable'),el('p',{class:'muted'},'This historical source document was not retained.')));
  const summary=el('div',{class:'card'},el('div',{class:'section-head'},el('h2',{},'Decision summary'),badge(row.status)));
  if(row.state==='queued' || row.state==='running')summary.append(el('p',{class:'decision'},row.state==='queued'?'Waiting to process':'Processing document'),el('p',{class:'muted'},row.events.at(-1)?.title || 'Your document is safely queued.'),el('div',{class:'progress-track'},el('span')),el('p',{class:'source-foot'},'Progress refreshes every two seconds. Navigation or closing this tab does not cancel the job.'));
  if(row.error)summary.append(el('div',{class:'callout error'},row.error.message));
  if(report) {
    const recommendation=report.decision.outcome==='AUTO_APPROVE'?'Approval recommended':report.decision.outcome==='HOLD_MISSING_INFO'?'Missing information':report.decision.outcome==='REJECT'?'Rejection recommended':'Review recommended';
    const reasons=report.summary_reasons?.length?report.summary_reasons:(report.decision.reasons || []);
    summary.append(...(['Approved','Rejected'].includes(row.status)?[]:[badge(recommendation)]),el('p',{class:'decision'},report.summary || report.decision.summary),el('h3',{},'Justification'),
      reasons.length?el('ul',{class:'reason-list'},reasons.map(reason=>el('li',{},valueView(reason)))):el('p',{},'All applicable validation checks passed.'),
      el('p',{},el('strong',{},'Next action'),el('span',{class:'sub'},row.status==='Approved'?'Approved net amount posted. No further approval is required.':report.decision.recommended_action)),
      el('div',{class:'fact-grid'},fact('Issuer',row.issuer),fact('Invoice number',row.invoice_number),fact('Gross invoice amount',available(row.gross_amount)+' '+available(report.normalized?.currency || row.currency)),fact('Matched PO',row.matched_po),fact('Document type',row.document_type),fact('Received',dateText(row.created_at)),...(report.source_validation?[fact('Source verification',sourceVerification(report.source_validation))]:[]),...(row.status==='Approved'?[fact('Approved net amount',available(report.normalized.net_amount)+' '+available(report.normalized.currency))]:[])));
    if(row.review_available)summary.append(el('div',{class:'action-row'},button('Approve invoice',()=>reviewDialog(row,'approve'),'primary'),button('Reject invoice',()=>reviewDialog(row,'reject'),'danger'),button('Recheck saved extraction',async()=>{try{await mutate('/invoices/'+row.id+'/recheck',{revision:row.revision},crypto.randomUUID());notify('Saved extraction rechecked against the source and current procurement data.');await renderInvoice(row.id,routeVersion);}catch(error){notify(error.message);}})));
    else if(row.imported)summary.append(el('p',{class:'source-foot'},'Imported historical record. Review actions follow its original backend state; process a new attempt when review is unavailable.'));
    if(report.decision.comms && row.status!=='Approved')summary.append(el('section',{class:'communication'},el('h3',{},'Suggested communication'),el('span',{class:'sub'},'Audience: '+report.decision.comms.audience),el('h4',{},report.decision.comms.subject),el('p',{class:'communication-body'},report.decision.comms.body)));
  }
  if(row.retry_available!==false && !['queued','running'].includes(row.state) && row.status!=='Approved' && row.document_available)summary.append(el('div',{class:'action-row'},button('Process new attempt',()=>retryDialog(row))));
  const top=el('div',{class:'detail-grid'},el('div',{},preview,row.document_available?el('a',{class:'back',href:source,target:'_blank',rel:'noopener'},'Open source document ↗'):null),summary);
  const tabs=el('div',{class:'tabs',role:'tablist','aria-label':'Invoice details'}),panel=el('div',{role:'tabpanel',id:'invoice-panel'});
  const renderTab=()=>{
    tabs.replaceChildren();
    for(const [key,title] of [['sections','Extracted sections & items'],['checks','Checks'],['timeline','Processing timeline'],['history','Action history']]) {
      const tab=button(title,()=>{activeTab=key;renderTab();});tab.className=activeTab===key?'selected':'';tab.setAttribute('role','tab');tab.setAttribute('aria-selected',activeTab===key);tab.setAttribute('aria-controls','invoice-panel');
      tab.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const buttons=[...tabs.children];const index=buttons.indexOf(tab);buttons[(index+(event.key==='ArrowRight'?1:3))%4].click();tabs.children[(index+(event.key==='ArrowRight'?1:3))%4].focus();}});
      tabs.append(tab);
    }
    panel.replaceChildren(activeTab==='timeline'?el('div',{class:'card'},timeline(row.events)):activeTab==='history'?activityTable(row.activity):!report?el('div',{class:'card'},el('p',{class:'muted'},'Results will appear after processing.')):activeTab==='checks'?checksPanel(report):sectionPanel(report));
  };
  renderTab();
  main.replaceChildren(el('a',{class:'back',href:'/invoices'},'← Back to invoices'),pageHead(row.invoice_number || row.filename,'Invoice detail · '+(row.imported?'Imported historical run':'Live extraction'),null),top,tabs,panel,
    el('details',{class:'card'},el('summary',{},'Processing attempts ('+row.attempts.length+')'),row.attempts.map(attempt=>el('p',{},el('a',{href:'/invoices/'+attempt.id},attempt.id), ' · '+attempt.state+' · '+dateText(attempt.created_at)))));
}
async function renderDeveloper(id,version) {
  const row=await api('/developer/runs/'+encodeURIComponent(id));if(version!==routeVersion)return;
  current={section:'developer',id,version};
  const metadata=row.extraction_metadata || {};
  const provider=row.artifacts.provider;
  main.replaceChildren(el('a',{class:'back',href:'/developer'},'← Back to developer runs'),pageHead('Run diagnostics',row.run_id || row.id,el('a',{class:'button',href:'/invoices/'+row.id},'Open invoice →')),
    el('div',{class:'card'},el('h2',{},'Extraction metadata'),el('div',{class:'fact-grid'},fact('Requested model',provider?.requested_model || metadata.requested_model || metadata.model),fact('Resolved model',provider?.resolved_model || metadata.resolved_model),fact('Provider',provider?.provider || metadata.provider),fact('Provenance',provider?.provenance || metadata.provenance),fact('Cache hit',provider?String(provider.cache_hit):metadata.cache_hit===undefined?null:String(metadata.cache_hit)),fact('API response time (ms)',provider?.response_ms ?? metadata.api_response_ms)),detailsView(provider?.token_usage || metadata.token_usage || {},'Token usage'),detailsView(provider?.usage || metadata.usage || {},'Provider usage'),detailsView(row.error,'Structured error')),
    el('div',{class:'card'},el('h2',{},'Stage events'),timeline(row.events),jsonView(row.events,'Event payloads and timings')),
    el('div',{class:'card'},el('h2',{},'Raw extraction response'),row.raw_available?jsonView(provider.raw_response,'Raw provider JSON'):el('p',{class:'muted'},'Raw provider response unavailable for this attempt.')),
    el('div',{class:'card'},detailsView(row.context,'Recorded decision context'),detailsView(row.artifacts.approval_recheck || null,'Approval recheck')));
}
async function renderRoute() {
  const version=++routeVersion;
  const [section='invoices',encodedId] = location.pathname.split('/').filter(Boolean);
  for(const anchor of document.querySelectorAll('[data-section]'))anchor.classList.toggle('active',anchor.dataset.section===section);
  main.setAttribute('aria-busy','true');
  try {
    if(!labels[section]){navigate('/invoices',true);return;}
    if(encodedId) {
      const id=decodeURIComponent(encodedId);
      if(section==='invoices')await renderInvoice(id,version);
      else if(section==='developer')await renderDeveloper(id,version);
      else if(['vendors','purchase-orders'].includes(section))await renderRecord(section,id,version);
      else throw new Error('Page not found.');
    } else await renderList(section,version);
  } catch(error) {
    if(version===routeVersion)main.replaceChildren(pageHead('Unable to load this view',error.message,button('Try again',renderRoute)),el('a',{href:'/invoices'},'Back to invoices'));
  } finally {if(version===routeVersion)main.setAttribute('aria-busy','false');}
}
setInterval(async()=>{
  if(pollBusy || document.hidden || !current)return;
  pollBusy=true;
  try {
    if(current.id && current.section==='invoices')await renderInvoice(current.id,current.version,true);
    else if(!current.id && ['invoices','developer','purchase-orders','vendors'].includes(current.section))await refreshList(current);
  } catch(error) {notify('Progress refresh failed. '+error.message);}finally{pollBusy=false;}
},2000);
(async()=>{try{
  config=await api('/config');
  if(config.demo_notice)document.querySelector('.content-shell').prepend(el('div',{class:'demo-banner',role:'note'},config.demo_notice));
  if(!can('admin','reviewer'))for(const link of document.querySelectorAll('[data-section="vendors"],[data-section="purchase-orders"],[data-section="developer"]'))link.hidden=true;
  await renderRoute();
}catch(error){main.replaceChildren(pageHead('Server unavailable',error.message,button('Try again',()=>location.reload())),el('a',{href:'/auth/login'},'Sign in'));}})();
