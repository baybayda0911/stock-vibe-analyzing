"""Explicit, bounded public-PDF acquisition; supplementary workspace files never become research."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
import re
import socket
from urllib.parse import urlsplit
import urllib.request
import uuid

SCHEMA = 'stock-broker-cache/1'
MAX_PDF_BYTES = 10 * 1024 * 1024
SOURCE_ID = re.compile(r'broker-[0-9a-f]{32}')


def _publication(value, precision, acquired):
    acquired_time = datetime.fromisoformat(acquired.replace('Z', '+00:00'))
    if acquired_time.tzinfo is None:
        raise ValueError('取得时刻须带时区')
    acquired_day = acquired_time.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    if precision == 'date':
        valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    elif precision == 'month':
        valid = isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}', value) and date.fromisoformat(value + '-01')
    else:
        valid = precision == 'unknown' and value is None
    if not valid or value and value > acquired_day[:len(value)]:
        raise ValueError('研报原发布日期精度或取得时序无效')


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def _base(store, security_id):
    store.lab.identifier(security_id, 'security_id')
    return store.lab.inside(store.root, '_workspace', 'platform', 'broker-cache', security_id)


def _public_pdf_url(url, *, resolve=False):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'pdf.dfcfw.com' or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r'/pdf/H3_AP\d+_1\.pdf', parsed.path, re.I)):
        raise ValueError('首版只读取东方财富公开研报PDF链接')
    if resolve:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError('研报目的地不是普通公网地址')
    return url


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _public_pdf_url(newurl, resolve=True)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _download(url):
    _public_pdf_url(url, resolve=True)
    opener = urllib.request.build_opener(_PublicRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'StockResearchLocal/1', 'Accept': 'application/pdf'})
    with opener.open(request, timeout=35) as response:
        _public_pdf_url(response.geturl())
        if response.headers.get('Content-Length') and int(response.headers['Content-Length']) > MAX_PDF_BYTES:
            raise ValueError('PDF超过10MiB上限')
        payload = response.read(MAX_PDF_BYTES + 1)
    if len(payload) > MAX_PDF_BYTES or not payload.startswith(b'%PDF-'):
        raise ValueError('没有取得符合大小限制的PDF原件')
    return payload


def _extract(payload):
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            return [], 'pdf_only', '当前运行环境未配置PDF文字提取；原件已暂存，未读正文。'
    try:
        with fitz.open(stream=payload, filetype='pdf') as document:
            if document.needs_pass or not 1 <= len(document) <= 100:
                raise ValueError('不接受加密或超过100页的研报')
            pages = [{'page': number + 1, 'text': page.get_text()} for number, page in enumerate(document)]
        if sum(len(row['text']) for row in pages) > 2_000_000:
            raise ValueError('PDF文字超过上限')
    except Exception as error:
        raise ValueError('PDF不能安全解析为文字页') from error
    if not any(row['text'].strip() for row in pages):
        return pages, 'needs_ocr', '原件没有可用文字层；未启动OCR。'
    return pages, 'text_extracted', '已读取文字页；观点与预测仍须核对原页，不能替代正式研究。'


def _validate_annotations(views, forecasts, pages):
    available = {row['page'] for row in pages}
    if len(views) > 20 or len(forecasts) > 30:
        raise ValueError('补充资料条目过多')
    for entry in [*views, *forecasts]:
        if not isinstance(entry, dict) or type(entry.get('page')) is not int or entry['page'] not in available:
            raise ValueError('观点与预测必须指向实际提取的PDF页')
        if len(_json_bytes(entry)) > 4096:
            raise ValueError('单项补充超过长度限制')
    for entry in views:
        if not isinstance(entry.get('text'), str) or not entry['text'].strip():
            raise ValueError('观点文本不能为空')
    for entry in forecasts:
        if (type(entry.get('fiscal_year')) is not int or not 1900 <= entry['fiscal_year'] <= 2200
                or any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in ('metric','unit'))
                or type(entry.get('value')) not in (int, float) or not math.isfinite(entry['value'])):
            raise ValueError('预测须有明确年度、指标、单位及有限数值')


def retain_pdf(store, security_id, record, payload, url, *, views=(), forecasts=(), review=None, context=None):
    """Caller supplies a selected, validated index record; annotations are researcher assertions."""
    context = store.context(security_id) if context is None else context
    security = store.lab.security(context['security'])
    if security['security_id'] != security_id or record.get('security_id', security_id) != security_id:
        raise ValueError('研报证券身份不一致')
    _public_pdf_url(url)
    if not isinstance(payload, bytes) or not payload.startswith(b'%PDF-') or len(payload) > MAX_PDF_BYTES:
        raise ValueError('原件须是10MiB内的PDF')
    if not re.fullmatch(r'index-[0-9a-f]{64}', record.get('source_id', '')):
        raise ValueError('须选择可信索引中的研报')
    published = record.get('publication_date')
    precision = record.get('publication_date_precision', record.get('publication_precision', 'unknown'))
    _publication(published, precision, store.lab.now())
    pages, status, note = _extract(payload)
    digest = hashlib.sha256(payload).hexdigest()
    combined = ''.join(row['text'] for row in pages)
    compact = lambda value: re.sub(r'\W+', '', value).casefold()
    match = bool(security['code'] in combined and compact(record.get('title', ''))
                 and compact(record['title']) in compact(combined))
    if precision == 'date':
        year, month, day = map(int, published.split('-'))
        match = match and bool(re.search(rf'{year}\D{{0,3}}0?{month}\D{{0,3}}0?{day}(?!\d)', combined))
    if pages and not match:
        status, note = 'needs_review', '正文证券代码或标题尚未与索引对应；只能查看原件，不采用其观点。'
    if review is not None:
        if (not isinstance(review, dict) or review.get('index_source_id') != record['source_id']
                or review.get('pdf_sha256') != digest or not review.get('reason')
                or not review.get('evidence_pages') or any(page not in {row['page'] for row in pages} for page in review['evidence_pages'])):
            raise ValueError('人工页级核验须绑定原PDF与当前索引身份')
        if review.get('original_report_date'):
            _publication(review['original_report_date'], 'date', store.lab.now())
        status, note = 'text_reviewed', '已按原页核对标题、证券及日期差异；卖方观点仍仅作补充。'
    if (views or forecasts) and status not in {'text_extracted', 'text_reviewed'}:
        raise ValueError('未核对正文不能登记观点或预测')
    _validate_annotations(views, forecasts, pages)
    basis = {'index_source_id': record['source_id'], 'pdf_sha256': digest, 'url': url,
             'views': list(views), 'forecasts': list(forecasts), 'review': review}
    source_id = 'broker-' + hashlib.sha256(_json_bytes(basis)).hexdigest()[:32]
    base = _base(store, security_id)
    destination = store.lab.inside(base, source_id)
    with store.lab.writer(store.root):
        if destination.exists():
            return source_detail(store, security_id, source_id, context=context)
        base.mkdir(parents=True, exist_ok=True)
        staging = store.lab.inside(base, '.' + source_id + '-' + uuid.uuid4().hex)
        staging.mkdir()
        cached = store.lab.now()
        acquired = review.get('acquired_at', cached) if review else cached
        if store.lab.stamp(acquired, 'acquired_at') > store.lab.stamp(cached, 'cached_at'):
            raise ValueError('原件取得时间不能晚于本次暂存')
        _publication(published, precision, acquired)
        page_payload = _json_bytes(pages)
        (staging / 'original.pdf').write_bytes(payload)
        (staging / 'pages.json').write_bytes(page_payload)
        metadata = {'schema_version': SCHEMA, 'source_id': source_id, 'security': security,
            'index_source_id': record['source_id'], 'index_row_sha256': record.get('row_sha256'),
            'title': record.get('title'), 'organization': record.get('organization'),
            'authors': record.get('authors'), 'publication_date': published, 'publication_precision': precision,
            'publication_date_precision': precision,
            'publication_date_source': 'local_report_index', 'source_url': url,
            'original_report_date': review.get('original_report_date') if review else None,
            'date_note': review.get('date_note') if review else '主日期取公开索引；未据此认定封面日或实际取得时刻。',
            'ambiguities': review.get('ambiguities', []) if review else [],
            'page_review': review,
            'acquired_at': acquired, 'cached_at': cached, 'imported_at': None,
            'original_sha256': digest, 'size_bytes': len(payload),
            'pages_sha256': hashlib.sha256(page_payload).hexdigest(), 'page_count': len(pages),
            'content_status': status, 'note': note, 'views': list(views), 'forecasts': list(forecasts),
            'content_label': {'text_reviewed':'已核原页，观点仅作补充', 'text_extracted':'正文已提取，待核观点',
                              'needs_review':'正文对应关系待核', 'needs_ocr':'待OCR', 'pdf_only':'仅PDF原件'}[status],
            'annotation_origin': 'researcher_page_review' if views or forecasts else None,
            'supplement_only': True, 'can_replace_formal': False, 'formal_archive': False}
        store.lab.write_json(staging / 'source.json', metadata)
        store.lab.checked_path(destination, reject_git=True)
        staging.rename(destination)
    return source_detail(store, security_id, source_id, context=context)


def _read(store, security_id, source_id, context):
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
        raise ValueError('无效研报资料身份')
    folder = store.lab.inside(_base(store, security_id), source_id)
    metadata_path = store._regular(folder / 'source.json')
    if metadata_path.stat().st_size > 512 * 1024:
        raise ValueError('研报登记信息超过上限')
    meta = store.lab.read_json(metadata_path)
    if (meta.get('schema_version') != SCHEMA or meta.get('source_id') != source_id
            or store.lab.security(meta.get('security')) != store.lab.security(context['security'])
            or meta.get('can_replace_formal') is not False or meta.get('formal_archive') is not False
            or meta.get('supplement_only') is not True):
        raise ValueError('研报来源身份或补充角色无效')
    for key in ('acquired_at', 'cached_at'):
        if store.lab.stamp(meta[key], key) > store.lab.stamp(store.lab.now(), 'now'):
            raise ValueError('研报取得时点位于未来')
    if store.lab.stamp(meta['acquired_at'], 'acquired_at') > store.lab.stamp(meta['cached_at'], 'cached_at'):
        raise ValueError('研报取得不得晚于暂存')
    _publication(meta.get('publication_date'), meta.get('publication_precision'), meta['acquired_at'])
    if meta.get('original_report_date'):
        _publication(meta['original_report_date'], 'date', meta['acquired_at'])
    if meta.get('content_status') not in {'text_extracted','text_reviewed','needs_review','needs_ocr','pdf_only'}:
        raise ValueError('未知正文状态')
    if meta['content_status'] not in {'text_extracted','text_reviewed'} and (meta.get('views') or meta.get('forecasts')):
        raise ValueError('未核正文不得携带观点或预测')
    _public_pdf_url(meta['source_url'])
    if store._regular(folder / 'original.pdf').stat().st_size > MAX_PDF_BYTES:
        raise ValueError('研报原件超过上限')
    payload = store._bytes(folder / 'original.pdf', meta['original_sha256'])
    if len(payload) != meta['size_bytes'] or not payload.startswith(b'%PDF-'):
        raise ValueError('研报原件不完整')
    if store._regular(folder / 'pages.json').stat().st_size > 8 * 1024 * 1024:
        raise ValueError('提取文字超过上限')
    pages = json.loads(store._bytes(folder / 'pages.json', meta['pages_sha256']))
    if (not isinstance(pages, list) or len(pages) > 100
            or any(not isinstance(row, dict) or type(row.get('page')) is not int or not 1 <= row['page'] <= 100
                   or not isinstance(row.get('text'), str) for row in pages)
            or len({row['page'] for row in pages}) != len(pages)):
        raise ValueError('研报文字页结构无效')
    if len(pages) != meta['page_count']:
        raise ValueError('研报页数与登记不符')
    _validate_annotations(meta['views'], meta['forecasts'], pages)
    return meta, pages, payload


def source_detail(store, security_id, source_id, *, context=None):
    context = store.context(security_id) if context is None else context
    from .broker_archive import registered_source
    meta, pages, _ = registered_source(store, security_id, source_id, context) or _read(store, security_id, source_id, context)
    return {**meta, 'pdf_available': True,
            'page_preview': [{'page': row['page'], 'text': row['text'][:1600]} for row in pages[:4]],
            'text_excerpt': '\n\n'.join(f"第{row['page']}页\n{row['text'][:1600]}" for row in pages[:4]),
            'preview_is_complete': len(pages) <= 4 and all(len(row['text']) <= 1600 for row in pages),
            'archive_note': meta.get('archive_note', '仅暂存于私有工作区，未正式归档，不在完整档案快照源内。')}


def sources(store, security_id, *, context=None):
    context = store.context(security_id) if context is None else context
    base = _base(store, security_id)
    result = []
    from .broker_archive import registered_source
    for record in context.get('roles', {}).get('supplementary', []):
        if record.get('content_role') != 'broker_source' or not record['result_id'].startswith('BRS-'):
            continue
        source_id = 'broker-' + record['result_id'].rsplit('-', 1)[-1]
        registered = registered_source(store, security_id, source_id, context)
        if registered:
            result.append({**registered[0], 'pdf_available': True})
    registered_ids = {row['source_id'] for row in result}
    if base.exists():
        store.lab.checked_path(base, reject_git=True)
    for path in base.iterdir() if base.exists() else []:
        if path.name.startswith('.') or path.name == 'index-checks':
            continue
        if path.name in registered_ids:
            continue
        store.lab.checked_path(path, reject_git=True)
        if not path.is_dir():
            raise ValueError('研报暂存目录存在未知文件')
        meta, _, _ = _read(store, security_id, path.name, context)
        result.append({**meta, 'pdf_available': True})
    return sorted(result, key=lambda row: (row['publication_date'] or '', row['cached_at']), reverse=True)


def source_file(store, security_id, source_id):
    from .broker_archive import registered_source
    context = store.context(security_id)
    _, _, payload = registered_source(store, security_id, source_id, context) or _read(store, security_id, source_id, context)
    return payload


def enrich_updates(store, result, *, context=None):
    security_id = result['security']['security_id']
    cached = sources(store, security_id, context=context)
    result['cached_sources'] = cached
    result['check_status'] = latest_check(store, security_id)
    for record in result['records']:
        candidates = [row for row in cached if row['index_source_id'] == record['source_id']]
        if candidates:
            selected = max(candidates, key=lambda row: (bool(row.get('page_review')), row['cached_at']))
            record.update({key:selected[key] for key in ('content_status','content_label','acquired_at')})
            record.update(cached_source_id=selected['source_id'], pdf_available=True)
    return result


def fetch_report(store, security_id, record_id, url):
    context = store.context(security_id)
    from .analyst_sources import analyst_updates
    index = analyst_updates(store, security_id, store.analyst_data_dir, limit=100, context=context)
    matches = [row for row in index['records'] if row['source_id'] == record_id]
    if len(matches) != 1:
        raise ValueError('须先选择本证券索引中的研报')
    return retain_pdf(store, security_id, matches[0], _download(url), url, context=context)


def refresh_index(store, security_id):
    """Record an explicit check; a first baseline is never called newly published research."""
    from .analyst_sources import analyst_updates
    context = store.context(security_id)
    current = analyst_updates(store, security_id, store.analyst_data_dir, limit=100, context=context)
    if current['status'] != 'ok':
        return {**current, 'check_status': {'state': 'unavailable', 'new_count': 0}}
    folder = store.lab.inside(_base(store, security_id), 'index-checks')
    with store.lab.writer(store.root):
        folder.mkdir(parents=True, exist_ok=True)
        previous = latest_check(store, security_id)
        before = {row['source_id'] for row in previous['records']} if previous else set()
        records = [{'source_id': row['source_id'], 'publication_date': row['publication_date'],
                    'row_sha256': row['row_sha256']} for row in current['records']]
        stamp = store.lab.now()
        state = {'schema_version': 'stock-broker-index-check/1', 'security_id': security_id,
                 'checked_at': stamp, 'records': records, 'index_sha256': current['source']['sha256'],
                 'state': 'checked' if previous else 'baseline',
                 'new_count': len({row['source_id'] for row in records} - before) if previous else 0,
                 'new_count_scope': '本次读取的最近100条资料；不代表新于正式研究或新近发布。'}
        store.lab.write_json(store.lab.inside(folder, uuid.uuid4().hex + '.json'), state)
    current['records'] = current['records'][:20]
    current['summary'] = {**current.get('summary', {}), 'shown':len(current['records'])}
    return enrich_updates(store, current, context=context)


def latest_check(store, security_id):
    folder = store.lab.inside(_base(store, security_id), 'index-checks')
    if not folder.exists():
        return None
    store.lab.checked_path(folder, reject_git=True)
    values = []
    for path in folder.glob('*.json'):
        value = store.lab.read_json(store._regular(path))
        if value.get('schema_version') != 'stock-broker-index-check/1' or value.get('security_id') != security_id:
            raise ValueError('研报检查快照身份无效')
        if store.lab.stamp(value['checked_at'], 'checked_at') > store.lab.stamp(store.lab.now(), 'now'):
            raise ValueError('研报检查快照时点位于未来')
        if any(not re.fullmatch(r'index-[0-9a-f]{64}', row.get('source_id', '')) for row in value['records']):
            raise ValueError('研报检查快照含无效资料身份')
        values.append(value)
    return max(values, key=lambda value: store.lab.stamp(value['checked_at'], 'checked_at')) if values else None
