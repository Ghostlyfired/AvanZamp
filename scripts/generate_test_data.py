"""Deterministic synthetic documents; offline LLM fixtures are NOT recorded API responses."""
import csv
import hashlib
import json
import argparse
from pathlib import Path
from decimal import Decimal
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import HexColor
except ImportError:
    canvas = None
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
VENDORS = [
 ('V001','Northwind Office Supplies Ltd','approved','Northwind Office Supplies'),
 ('V002','Brightline IT Services Inc','approved',''),('V003','Apex Logistics LLC','approved',''),
 ('V004','Meridian Facilities Co','approved',''),('V005','Orion Consulting Group','blocked',''),
 ('V006','Cobalt Cloud Hosting Inc','approved',''),('V007','Summit Print & Packaging','approved',''),
 ('V008','Greenfield Catering Co','approved','')]
POS = [
 ('PO-1001','V001','Office furniture - Q4 order','4800','0.08'),
 ('PO-1002','V002','Managed IT services - November','12500','0.10'),
 ('PO-1003','V003','Freight - warehouse transfer','2000','0.08'),
 ('PO-1004','V004','Facilities maintenance - Q4','7500','0.08'),
 ('PO-1005','V005','Strategy consulting engagement','18000','0.08'),
 ('PO-2001','V006','Cloud platform migration (3 milestones)','30000','0.08'),
 ('PO-3001','V007','Printed brochures - product launch','2400','0.08'),
 ('PO-4001','V008','Q4 team offsite catering','3000','0.08'),
 ('PO-4002','V008','Monthly pantry supplies - November','1200','0.08'),
 ('PO-9001','V001','Paper replenishment','500','0.08'),
 ('PO-9002','V002','Hardware support','1500','0.10'),
 ('PO-9003','V003','Freight - old order','1000','0.08')]
# filename, vendor index, invoice number, date, PO, net, tax, description, template, outcome, driving
SPECS = [
 ('01_clean_exact',0,'INV-10482','2026-10-14','PO-1001','4800','384','Office furniture - Q4 order','A','AUTO_APPROVE',[]),
 ('02_tax_inclusive',1,'BIT-7731','2026-10-20','PO-1002','12500','1250','Managed IT services - November','B','AUTO_APPROVE',[]),
 ('03_within_tolerance',3,'MFC-5590','2026-10-22','PO-1004','7620','609.60','Facilities maintenance - Q4','A','AUTO_APPROVE',[]),
 ('04_over_tolerance',2,'APX-20931','2026-10-23','PO-1003','2080','166.40','Freight - warehouse transfer','B','NEEDS_REVIEW',['amount_vs_balance']),
 ('05_blocked_vendor',4,'OCG-0192','2026-10-30','PO-1005','18000','1440','Strategy consulting engagement','A','REJECT',['vendor_status']),
 ('06_split_po_1of3',5,'CCH-2026-0412','2026-09-15','PO-2001','10000','800','Milestone 1 - Platform migration: discovery & design','A','AUTO_APPROVE',[]),
 ('09_near_duplicate',5,'CH-88120','2026-09-18','PO-2001','10000','800','Milestone 1 - Platform migration: discovery & design','C','NEEDS_REVIEW',['duplicate_near']),
 ('07_split_po_2of3',5,'CCH-2026-0498','2026-10-15','PO-2001','10000','800','Milestone 2 - Data migration & cutover','A','AUTO_APPROVE',[]),
 ('08_split_po_3of3_overbill',5,'CCH-2026-0511','2026-11-05','PO-2001','12500','1000','Milestone 3 - Hypercare & decommissioning','A','REJECT',['amount_vs_balance']),
 ('10_missing_invoice_number',6,None,'2026-10-27','PO-3001','2400','192','Printed brochures - product launch','B','HOLD_MISSING_INFO',['completeness']),
 ('11_scanned_implied_po',7,'GC-2210','2026-10-29',None,'3000','240','Q4 team offsite catering - 40 guests','S','NEEDS_REVIEW',['stacked_uncertainty'])]

def fields(spec: tuple) -> dict:
    _,vi,num,dt,po,net,tax,desc,template,*_ = spec
    return dict(vendor_name=VENDORS[vi][1],invoice_number=num,invoice_date=dt,po_reference=po,
                currency='USD',subtotal=f'{Decimal(net):.2f}',tax_amount=f'{Decimal(tax):.2f}',
                total=f'{Decimal(net)+Decimal(tax):.2f}',prices_include_tax=template=='B',
                reference_text=desc,line_items=[dict(description=desc,quantity='1',unit_price=net,amount=net)])

def document_lines(f: dict, template: str) -> list[str]:
    number_label = {'A':'Invoice No','B':'Document ref','C':'Bill ref','S':'Invoice No'}[template]
    lines = [f"Vendor: {f['vendor_name']}",'INVOICE | Accounts payable','22 Commerce Avenue, New York, NY 10001',
             'Bill to: Acme Operations Inc',f"Invoice date: {f['invoice_date']}"]
    if f['invoice_number']: lines.append(f"{number_label}: {f['invoice_number']}")
    else: lines += ['Customer account no: 88341','Delivery note: DN-5520']
    if f['po_reference']: lines.append(f"{'Your order' if template=='B' else 'PO Number'}: {f['po_reference']}")
    lines += ['Currency: USD',f"Re: {f['reference_text']}",f"Item: {f['reference_text']} | 1 | {f['subtotal']} | {f['subtotal']}"]
    if template != 'B': lines.append(f"Subtotal: {f['subtotal']}")
    lines.append(f"{'VAT incl.' if template=='B' else 'Tax'}: {f['tax_amount']}")
    lines.append(f"{'Grand total' if template=='C' else 'Amount due' if template=='B' else 'Total Due'}: {f['total']}")
    lines += ['Payment terms: Net 30','Thank you for your business.']
    return lines


def write_fixture(path: Path, f: dict, lines: list[str], cache: Path | None = None) -> Path:
    """Pair existing synthetic PDF bytes with explicitly synthetic offline facts."""
    evidence={k:next((line for line in lines if str(v) in line),'') for k,v in f.items()
              if v is not None and k not in ['line_items','prices_include_tax']}
    evidence['prices_include_tax']=next(line for line in lines if line.startswith(('VAT','Tax')))
    evidence['line_items']=next(line for line in lines if line.startswith('Item:'))
    fixture=dict(provenance='synthetic_fixture',source='scripts/generate_test_data.py; NOT an API recording',
                 extraction={**f,'evidence':evidence})
    cache=cache or ROOT/'.cache/llm'
    cache.mkdir(parents=True,exist_ok=True)
    destination=cache/(hashlib.sha256(path.read_bytes()).hexdigest()+'.json')
    destination.write_text(json.dumps(fixture,indent=2)+'\n',encoding='utf-8')
    return destination


def generate_fixtures() -> list[Path]:
    """Restore offline fixtures without rewriting PDFs, CSVs or the golden oracle."""
    paths=[]
    for spec in SPECS:
        f=fields(spec)
        if spec[8] in {'C','S'} or f['invoice_number'] is None:
            path=ROOT/'data/invoices'/(spec[0]+'.pdf')
            paths.append(write_fixture(path,f,document_lines(f,spec[8])))
    return paths

def generate() -> None:
    data = ROOT/'data'; invoices=data/'invoices'; invoices.mkdir(parents=True,exist_ok=True)
    with (data/'vendors.csv').open('w',newline='') as out:
        w=csv.writer(out); w.writerow(['vendor_id','name','status','aliases_json','notes'])
        for vid,name,status,alias in VENDORS:
            w.writerow([vid,name,status,json.dumps([alias] if alias else []),'Bank details changed 2026-10-28, pending re-verification' if status=='blocked' else ''])
    with (data/'purchase_orders.csv').open('w',newline='') as out:
        w=csv.writer(out); w.writerow(['po_number','vendor_id','description','net_amount','tax_rate','currency','status','issue_date'])
        for po,vid,desc,net,tax in POS:
            w.writerow([po,vid,desc,net,tax,'USD','closed' if po=='PO-9003' else 'open','2026-09-01'])
    golden=[]
    for order,spec in enumerate(SPECS,1):
        stem,*_=spec; template=spec[8]; f=fields(spec); lines=document_lines(f,template); path=invoices/(stem+'.pdf')
        if template=='S':
            img=Image.new('RGB',(1500,1900),'white'); draw=ImageDraw.Draw(img)
            for i,line in enumerate(lines): draw.text((100,150+i*75),line,fill='#202020',font_size=28)
            img=img.rotate(0.8,fillcolor='white').filter(ImageFilter.GaussianBlur(0.35))
            img.save(path,'PDF',resolution=180)
        elif canvas is None:
            from pdf_fallback import write_pdf
            write_pdf(path,lines,template=='B')
        else:
            c=canvas.Canvas(str(path),pagesize=(612,792),invariant=1)
            c.setFillColor(HexColor('#12354a')); c.rect(0,690,612,102,fill=1,stroke=0)
            for i,line in enumerate(lines):
                y=750-i*32; c.setFillColor(HexColor('#ffffff' if i<2 else '#202d38'))
                c.setFont('Helvetica-Bold' if i<2 else 'Helvetica',14 if i<2 else 10)
                c.drawString(35 if template!='B' else 65,y,line)
            c.save()
        route='llm_vision' if template=='S' else 'llm_text' if template=='C' or f['invoice_number'] is None else 'deterministic'
        golden.append(dict(file=path.name,order=order,expected_outcome=spec[9],expected_path=route,expected_driving_checks=spec[10]))
        if route.startswith('llm'):
            write_fixture(path,f,lines)
    duplicate=invoices/'12_exact_duplicate.pdf'; duplicate.write_bytes((invoices/'01_clean_exact.pdf').read_bytes())
    golden.append(dict(file=duplicate.name,order=12,expected_outcome='REJECT',expected_path='deterministic',expected_driving_checks=['duplicate_exact']))
    (data/'golden.json').write_text(json.dumps(golden,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures-only',action='store_true',help='Restore synthetic offline extraction fixtures for existing PDF bytes; never regenerate documents.')
    args=parser.parse_args()
    if args.fixtures_only:
        for path in generate_fixtures():
            print(path.relative_to(ROOT))
    else:
        generate()
