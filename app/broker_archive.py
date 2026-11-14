"""Explicit, append-only promotion of a reviewed source; never a formal research conclusion."""

import hashlib
import json
from pathlib import Path
import re
import uuid


ROLE_CAPABILITY = 'stock-research-role-resolver/1'
SKILLS = {'stock-fundamental-analysis', 'stock-technical-analysis', 'stock-research-lab', 'stock-research-archive'}


def compatibility(store):
    """The local installer records explicit hosts; stale or incomplete copies fail closed."""
    unavailable = {'enabled': False, 'reason': '已安装宿主的角色协议尚未完成核验，暂存资料仍可查阅。'}
    if getattr(store.lab, 'CAPABILITIES', {}).get('role_resolution') != ROLE_CAPABILITY:
        return unavailable
    path = store.lab.inside(store.root, '_workspace', 'platform', 'skill-hosts.json')
    if not path.exists():
        return unavailable
    try:
        config = store.lab.read_json(store._regular(path))
        if config.get('schema_version') != 'stock-research-hosts/1' or config.get('role_resolution') != ROLE_CAPABILITY:
            return unavailable
        store.lab.stamp(config['verified_at'], 'verified_at')
        hosts = config['installed_roots']
        if not isinstance(hosts, list) or len(hosts) != 2 or len(set(hosts)) != 2:
            return unavailable
        hosts = [store.lab.checked_path(Path(directory)) for directory in hosts]
        if any(not directory.is_dir() for directory in hosts) or len({str(directory.resolve()).casefold() for directory in hosts}) != 2:
            return unavailable
        repo = Path(__file__).resolve().parents[1]
        members = [row for row in (repo / 'release-files.txt').read_text(encoding='utf-8').splitlines() if row.startswith('skills/')]
        if {Path(row).parts[1] for row in members} != SKILLS or set(config['files']) != set(members):
            return unavailable
        for relative in members:
            if '..' in Path(relative).parts or Path(relative).is_absolute():
                return unavailable
            source = store.lab.checked_path(repo / relative).read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            if config['files'][relative] != digest:
                return unavailable
            for directory in hosts:
                target = store._regular(Path(directory).joinpath(*Path(relative).parts[1:]))
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    return unavailable
        return {'enabled': True, 'reason': '两个明确安装位置的四技能资源已核对，同用角色协议；已打开会话仍可能需要重新加载。'}
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable


def ids(security_id, source_id):
    code = security_id.rsplit('-', 1)[-1]
    suffix = source_id.removeprefix('broker-')
    if not re.fullmatch(r'[0-9a-f]{32}', suffix):
        raise ValueError('Invalid cached source identity')
    return 'BRI-' + code + '-' + suffix, 'BRS-' + code + '-' + suffix


def archive_status(store, security_id, source_id, context):
    import_id, result_id = ids(security_id, source_id)
    row = next((record for record in context['results'] if record['result_id'] == result_id), None)
    if row is None:
        return {'formal_archive': False}
    role = next((entry for entry in context.get('roles', {}).get('classified', []) if entry['result_id'] == result_id), None)
    if not role or role.get('content_role') != 'broker_source' or role.get('source_ref', {}).get('import_id') != import_id:
        raise ValueError('Archived source role or registered original differs')
    return {'formal_archive': True, 'import_id': import_id, 'result_id': result_id,
            'report_id': 'result:' + result_id, 'archived_at': row['generated_at'],
            'archive_note': '补充资料及原PDF已正式归档并进入后续完整快照源；不替代正式基本面报告。'}


def registered_source(store, security_id, source_id, context):
    status = archive_status(store, security_id, source_id, context)
    if not status['formal_archive']:
        return None
    record = next(row for row in context['results'] if row['result_id'] == status['result_id'])
    reader = store._record_reader(record)
    metadata = json.loads(reader('broker-source.json')[0])
    pages = json.loads(reader('pages.json')[0])['pages']
    if (metadata.get('source_id') != source_id or store.lab.security(metadata.get('security')) != context['security']
            or metadata.get('source_ref', {}).get('original_sha256') != record.get('original_sha256')):
        raise ValueError('Registered source metadata differs')
    registration = store.lab.read_json(store._regular(store.lab.inside(store.root, 'securities', security_id,
        'imports', status['import_id'], 'import.json')))
    original = store.lab.inside(store.root, 'securities', security_id, 'imports', status['import_id'],
                                'original', registration['source_name'])
    payload = store._bytes(original, registration['original_sha256'])
    return {**metadata, **status}, pages, payload


def archive_source(store, security_id, source_id):
    if not compatibility(store)['enabled']:
        raise ValueError('Installed hosts have not passed shared role compatibility')
    from .broker_cache import _read
    context = store.context(security_id)
    status = archive_status(store, security_id, source_id, context)
    if status['formal_archive']:
        return {**status, 'status': 'already_archived'}
    meta, pages, payload = _read(store, security_id, source_id, context)
    if meta['content_status'] != 'text_reviewed' or not meta.get('page_review'):
        raise ValueError('Formal source import requires an explicit original-page review')
    import_id, result_id = ids(security_id, source_id)
    _, catalog = store._scan()
    partial = next((row for row in catalog['imports'] if row['import_id'] == import_id), None)
    timestamp = store.lab.now()
    research_asof = partial['research_asof'] if partial else timestamp
    formal = context['roles']['formal_fundamental']
    source_ref = {'security_id': security_id, 'import_id': import_id, 'original_sha256': meta['original_sha256']}
    component = {'schema_version': 'stock-research-components/1', 'security': context['security'], 'result_id': result_id,
        'content_role': 'broker_source', 'source_ref': source_ref, 'source_title': meta['title'],
        'published_date': meta['publication_date'] if meta['publication_precision'] in {'date', 'day'} else None,
        'published_at': None, 'original_publication_date': meta['publication_date'],
        'original_publication_precision': meta['publication_precision'], 'cover_report_date': meta.get('original_report_date'),
        'acquired_at': meta['acquired_at'], 'extracted_at': meta['cached_at'],
        'components': [{'component_id': 'broker-source', 'kind': 'supplementary_views_and_forecasts'}]}
    folder = store.lab.inside(store.root, '_workspace', 'platform', 'archive-preparation', uuid.uuid4().hex)
    folder.mkdir(parents=True)
    original = store.lab.inside(folder, 'original.pdf')
    with original.open('xb') as output:
        output.write(payload)
    metadata = {**meta, 'source_ref': source_ref, 'formal_archive': True, 'result_id': result_id,
                'adaptation_prepared_at': timestamp, 'supplement_only': True}
    for name, value in (('research-components.json', component), ('broker-source.json', metadata), ('pages.json', {'pages': pages})):
        store.lab.write_json(folder / name, value)
    lines = ['# 补充研报资料：' + meta['title'], '',
        '本资料保留券商原观点及预测，仅供后续基本面研究参考；不是新的正式基本面分析。', '',
        '- 索引原发布日期：' + str(meta['publication_date']),
        '- 封面报告日：' + str(meta.get('original_report_date')),
        '- 实际取得：' + meta['acquired_at'], '- 本次适配准备：' + timestamp,
        '- 原PDF SHA256：' + meta['original_sha256'], '- 发布来源：' + meta['source_url'], '', '## 有限页级观点', '']
    lines += ['- 第' + str(row['page']) + '页：' + row['text'] for row in meta['views']]
    lines += ['', '## 原券商预测（并非公司实际业绩）', '', '| 财年 | 指标 | 数值 | 单位 | 原页 |', '|---|---|---|---|---|']
    lines += ['| ' + ' | '.join(str(row[key]) for key in ('fiscal_year', 'metric', 'value', 'unit', 'page')) + ' |' for row in meta['forecasts']]
    lines += ['', '## 原始差异及使用边界', '', meta.get('date_note') or '', *['- ' + str(row) for row in meta.get('ambiguities', [])],
              '', '预测、EPS与股本口径未被自动采纳为正式估值；归档不刷新原报告日期或任何旧判断资格。']
    report = folder / 'report.md'
    report.write_text('\n'.join(lines), encoding='utf-8')
    request = {'security': context['security'], 'module': 'fundamental', 'module_version': '0.3.2',
        'result_id': result_id, 'import_id': import_id, 'source_file': str(original),
        'expected_original_sha256': meta['original_sha256'],
        'original_generated_at': None, 'original_date_evidence': '原发布日期及封面日保存在明示资料附件；无确切发布时间，不补造时刻。',
        'research_asof': research_asof, 'data_asof': None, 'parent_result_ids': [formal['result_id']] if formal else [],
        'report_file': str(report), 'evidence': [], 'items': [],
        'attachments': [{'source_file': str(folder / name), 'relative_path': name, 'media_type': 'application/json'}
                        for name in ('research-components.json', 'broker-source.json', 'pages.json')]}
    result = store.lab.import_report(store.root, request)
    if result.get('partial'):
        return {**result, 'status': 'partial', 'formal_archive': False}
    saved = {'saved': True, 'formal_archive': True, 'status': 'archived', 'import_id': import_id,
             'result_id': result_id, 'report_id': 'result:' + result_id, 'archived_at': None,
             'catalog_updated': result.get('catalog_updated', False)}
    try:
        context = store.context(security_id)
        saved.update(archive_status(store, security_id, source_id, context))
    except Exception:
        saved['warning'] = '原件与补充资料已保存，但归档状态暂未回读；请刷新核查，勿重复提交。'
    return saved
