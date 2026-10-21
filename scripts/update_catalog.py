#!/usr/bin/env python3
"""Official Taiwanese Melaleuca PDF catalog updater.

No AI/API keys. Fail closed on extraction abnormalities. On a failure the
previous verified dataset and storefront remain unchanged; only the status is
updated to show an error. The GitHub workflow still deploys the safe version.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import fitz
import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_URL = 'https://cdnsc1.melaleuca.com/tw-hk/documents/help-center/tw/Pricelist-zhtw.pdf'
PAGES = {2:'生命活力營養',3:'生命活力營養',4:'水貝娜肌膚保養',5:'水貝娜肌膚保養',6:'身體保養',7:'專業護理',8:'純質精油',9:'居家清潔',10:'產品套裝／輔銷'}


def read_json(p:Path):
    return json.loads(p.read_text('utf-8'))


def save_json(p:Path,data):
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def money(t):
    if t is None or str(t).strip()=='' :return None
    s=str(t).strip().replace(',','')
    if not re.fullmatch(r'[0-9]+',s):return None
    return int(s)


def guess_name(row):
    # Normal catalogs put the name in the longest textual table cell.
    candidates=[str(s).strip() for s in row[1:] if s and re.search(r'[\u4e00-\u9fffA-Za-z]',str(s)) and '產品名稱' not in str(s)]
    if not candidates:return ''
    raw=max(candidates,key=len)
    return re.sub(r'\s+',' ',raw.split('\n')[0]).strip()


def extract_table_rows(pdf_bytes,old):
    parsed={}; warnings=[]
    import io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n=len(pdf.pages)
        if not 9<=n<=25:raise ValueError(f'頁數異常：{n}')
        for page_number,page in enumerate(pdf.pages,1):
            if page_number==1:continue
            for table in page.extract_tables() or []:
                for row in table:
                    if not row or not row[0]:continue
                    code_match=re.fullmatch(r'(?:[●■▲\s]*)?(\d{2,6})\s*',str(row[0]).strip())
                    if not code_match:continue
                    code=code_match.group(1)
                    if code in parsed:raise ValueError(f'重複商品編號：{code}')
                    # PDF table layout has three common configurations:
                    # - numbered product: [code, (empty), name, pts, price, msrp]
                    # - accessory: [code, (empty), name, price, (empty), (empty)]
                    # - page10 product: [code, (empty), name, pts, price]
                    vals=[money(c) for c in row[1:]]
                    numbers=[v for v in vals if v is not None]
                    if len(numbers)>=3: points,price,msrp=numbers[-3:]
                    elif len(numbers)==2:
                        if page_number==10 and numbers[0]<=500 and numbers[1]>=numbers[0]:points,price=numbers;msrp=None
                        else: # One price + suggested price, with no membership points
                            # Most two-number rows are point + price in all sections.
                            if numbers[0]<=150 and numbers[1]>numbers[0]*4:points,price=numbers;msrp=None
                            else:points,price,msrp=0,numbers[0],numbers[1]
                    elif len(numbers)==1:points,price,msrp=0,numbers[0],None
                    else:
                        warnings.append(f'缺價：頁{page_number} #{code}')
                        continue
                    name=guess_name(row)
                    if code in old:
                        # The official PDF sometimes displays a short promotional label
                        # instead of a full product name. Prefer the established name
                        # for obviously truncated labels; accept clear official renames.
                        previous_name=old[code]['name']
                        if (not name or name in ('日常套組','推薦套組','產品名稱')
                                or len(name)<max(6,len(previous_name)*0.68)):
                            name=previous_name
                        category=old[code]['category'];subcategory=old[code].get('subcategory','')
                    else:
                        category=PAGES.get(page_number,'其他');subcategory=''
                    if not name:raise ValueError(f'缺商品名稱：#{code}')
                    old_active=(True if old.get(code,{}).get('discontinued') else old.get(code,{}).get('active',not ('僅生活館' in str(row) or '■' in str(row[0]))))
                    old_online=old.get(code,{}).get('availableOnline',True)
                    product={'code':code,'name':name,'points':points,'price':price,'msrp':msrp,'category':category,'subcategory':subcategory,'accessory':points==0,'page':page_number,'source':'台灣美樂家官方產品價目表','active':old_active,'availableOnline':old_online}
                    parsed[code]=product
    # OCR-free fallback: some valid bottom-of-page rows are excluded by
    # pdfplumber's table boundary, e.g. item #70197 on page 4.
    doc=fitz.open(stream=pdf_bytes,filetype='pdf')
    for page_no,page in enumerate(doc,1):
        w=page.get_text('words')
        for item in w:
            x,y,_,_,word,*_=item
            if not (45<=x<=83 and re.fullmatch(r'\d{2,6}',word)) or word in parsed:continue
            # Header numbers are outside the data columns; require matching prices.
            price_column=[q for q in w if abs(q[1]-y)<3.6 and q[0]>445 and q[0]<558 and re.fullmatch(r'\d+',q[4])]
            price_column.sort(key=lambda q:q[0])
            values=[int(q[4]) for q in price_column]
            if len(values)<2 or len(values)>3:continue
            if len(values)==3:points,price,msrp=values
            else:points,price=values;msrp=None
            if word not in old:
                # Unknown truncated rows aren't accepted without a table-extracted name.
                warnings.append(f'新商品可能超出表格頁尾：頁{page_no} #{word}')
                continue
            p=copy.deepcopy(old[word]);p.update(points=points,price=price,msrp=msrp,page=page_no)
            parsed[word]=p
    doc.close()
    return parsed,warnings


def catalog_version(pdf_bytes):
    doc=fitz.open(stream=pdf_bytes,filetype='pdf')
    first=' '.join(p.get_text() for p in doc[:2]);doc.close()
    m=re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月',first)
    if not m:raise ValueError('找不到官方 PDF 的年份與月份')
    y,month=int(m[1]),int(m[2])
    if not (2025<=y<=2100 and 1<=month<=12):raise ValueError('價目表月份不合理')
    return f'{y}-{month:02d}'


def validate(candidate,old):
    # Missing lots of familiar SKUs usually means the PDF layout changed, not
    # that dozens of products were actually removed.
    missing=set(old)-set(candidate)
    if len(missing)>3:raise ValueError(f'有 {len(missing)} 項舊商品未成功解析，暫停更新避免誤下架：{sorted(missing)[:12]}')
    if len(candidate)<260:raise ValueError(f'可辨識品項不足：{len(candidate)}')
    if len(candidate)>2000:raise ValueError(f'品項數量異常：{len(candidate)}')
    for p in candidate.values():
        if not isinstance(p['points'],int) or p['points']<0 or p['points']>500:raise ValueError(f"點數異常：#{p['code']}")
        if not isinstance(p['price'],int) or p['price']<=0 or p['price']>100000:raise ValueError(f"價格異常：#{p['code']}")
        if not p['name'].strip():raise ValueError(f"商品名稱異常：#{p['code']}")
        before=old.get(p['code'])
        if before:
            if before['price']>0 and (p['price']/before['price']>1.8 or p['price']/before['price']<0.35):raise ValueError(f"商品 #{p['code']} 價格突變需人工核對")
            if abs(p['points']-before['points'])>25:raise ValueError(f"商品 #{p['code']} 點數變化過大")


def replace_seed(index_text,catalog,version):
    raw=json.dumps(catalog,ensure_ascii=False,separators=(',',':'))
    raw=raw.replace('</','<\\/') # Prevent script end tag injection
    revision=hashlib.sha256(raw.encode()).hexdigest()[:12]
    patt=r'(<script id="seed" type="application/json" data-version=")[^"]+(" data-revision=")[^"]+(">)(.*?)(</script>)'
    updated,n=re.subn(patt,lambda m:m[1]+version+m[2]+revision+m[3]+raw+m[5],index_text,count=1,flags=re.S)
    if n!=1:raise ValueError('找不到內建商品資料標籤，停止覆寫 HTML')
    return updated


def fetch_pdf(url):
    req=Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; PersonalCatalogUpdater/1.3)','Accept':'application/pdf,*/*'})
    with urlopen(req,timeout=40) as resp:
        data=resp.read(15_000_001)
    if len(data)>15_000_000:raise ValueError('PDF 大於 15 MB，停止解析')
    if not data.startswith(b'%PDF-'):raise ValueError('官方網址沒有回傳 PDF 檔案')
    return data


def run(args):
    status_path=ROOT/'update-status.json';catalog_path=ROOT/'catalog.json';html_path=ROOT/'index.html'
    old_status=read_json(status_path);old_products=read_json(catalog_path)
    old={x['code']:x for x in old_products}
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    status=copy.deepcopy(old_status);status.update(last_checked_at=now,source_url=OFFICIAL_URL)
    try:
        pdf_bytes=Path(args.pdf).read_bytes() if args.pdf else fetch_pdf(OFFICIAL_URL)
        version=catalog_version(pdf_bytes)
        if version<old_status['catalog_version']:
            raise ValueError(f'官方回傳舊版 {version}，本站保留 {old_status["catalog_version"]}')
        candidate,warnings=extract_table_rows(pdf_bytes,old)
        validate(candidate,old)
        missing=set(old)-set(candidate)
        new_codes=set(candidate)-set(old)
        # Only tolerate <=3 removals: old rows may really be discontinued.
        for code in sorted(missing):
            # Keep out-of-catalog SKUs out of the active algorithm but retain
            # them in history for visibility and user comparison.
            p=copy.deepcopy(old[code]);p['active']=False;p['availableOnline']=False;p['discontinued']=True
            candidate[code]=p
        result_products=sorted(candidate.values(),key=lambda p:(p['page'],p['code'].zfill(7)))
        sums={'added':len(new_codes),'removed':len(missing),'price_changes':sum(1 for k,p in candidate.items() if k in old and p['price']!=old[k]['price']),'point_changes':sum(1 for k,p in candidate.items() if k in old and p['points']!=old[k]['points'])}
        # Real catalog update if month differs or actual names/points/prices change.
        old_identity={k:(p['name'],p['points'],p['price'],p['msrp'],p['active']) for k,p in old.items()}
        new_identity={k:(p['name'],p['points'],p['price'],p['msrp'],p['active']) for k,p in candidate.items()}
        changed=version!=old_status['catalog_version'] or old_identity!=new_identity
        if changed:
            index=replace_seed(html_path.read_text('utf-8'),result_products,version)
            if not args.dry_run:
                html_path.write_text(index,'utf-8');save_json(catalog_path,result_products)
            status.update(catalog_version=version,catalog_count=len(result_products),last_updated_at=now,result='updated',message=f'已完成 {version} 商品校對，異動：{sums}',change_summary=sums)
        else:
            status.update(result='same',message=f'確認官方仍為 {version}，商品資料無變更。',change_summary={'added':0,'removed':0,'price_changes':0,'point_changes':0})
        if warnings:status['warnings']=warnings[:12]
        print(status['message'])
    except Exception as exc:
        status.update(result='error',message=f'更新失敗，仍保留上次驗證版本。原因：{exc}')
        print(status['message'],file=sys.stderr)
    if not args.dry_run:save_json(status_path,status)
    else:print('[DRY-RUN] Files kept unchanged.')
    return 0 if status['result']!='error' else 2

if __name__=='__main__':
    a=argparse.ArgumentParser(description='Extract, verify and refresh latest official monthly PDF catalog.')
    a.add_argument('--pdf',help='Use a local official PDF for regression tests; do not download.')
    a.add_argument('--dry-run',action='store_true',help='Validate and report without modifying any file.')
    args=a.parse_args()
    sys.exit(run(args))
