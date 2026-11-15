"""Explicit, append-only local financial and daily-data refreshes. No model or network."""
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time as wall_clock
import uuid

from .local_financial import load_financial
from .local_market import load_market, load_calendar
from . import local_market

REPO = Path(__file__).resolve().parents[1]
SCHEMA = 'stock-local-update/1'
TZ = timezone(timedelta(hours=8))
STATES = {'prepared', 'queued', 'running', 'partial', 'interrupted', 'failed', 'completed'}
MEDIA = {'.json': 'application/json', '.png': 'image/png', '.html': 'text/html'}
RUNTIME_PROBE = '''import json,sys,numpy as n,talib,matplotlib,plotly
a=n.arange(1.,81.)
assert talib.get_compatibility()==0 and all(talib.get_unstable_period(k)==0 for k in ('EMA','RSI','ATR'))
assert n.isfinite(talib.SMA(a,5)[-1]) and n.isfinite(talib.RSI(a,14)[-1])
print(json.dumps(dict(probe='talib-sma-rsi/1',python=sys.version.split()[0],numpy=n.__version__,talib=talib.__version__,matplotlib=matplotlib.__version__,plotly=plotly.__version__)))'''


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)+'\n').encode('utf-8')


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _selection(financial, market, index):
    # Source bytes may already contain records whose publication/session has not ended.
    # Bind eligibility, not the moving acquisition timestamp, to the idempotency key.
    return {'market_last_date': market['lastdate'],
            'financial_versions_sha256': _sha(_encoded(financial['provenance']['versions'])),
            'index_last_date': index['bars'][-1]['time'][:10] if index else None}


class LocalUpdates:
    def __init__(self, store):
        self.store = store
        self.folder = store.lab.inside(store.root, '_workspace', 'platform', 'local-data-updates')
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock, self._thread, self._active, self._closing = threading.RLock(), None, None, False
        for job in self._all():
            if job['status'] in {'queued', 'running'}:
                job.update(status='interrupted', stage_label='上次更新中断，请明确恢复；已归档部分保留')
                self._save(job)

    def _config(self):
        path = self.store.lab.inside(self.store.root, '_workspace', 'platform', 'local-data-config.json')
        if not path.exists():
            raise ValueError('本地数据更新尚未配置')
        if self.store._regular(path).stat().st_size > 8192:
            raise ValueError('本地更新配置不合法')
        config = self.store.lab.read_json(path)
        if (set(config) != {'schema_version', 'enabled', 'technical_python'} or
                config.get('schema_version') != 'stock-local-update-config/1' or config.get('enabled') is not True):
            raise ValueError('本地数据更新尚未启用或配置不兼容')
        python = Path(config['technical_python'])
        if not python.is_absolute() or python.name.lower() not in {'python.exe', 'python3.exe', 'python', 'python3'}:
            raise ValueError('需配置独立Python的明确绝对路径')
        if os.name == 'nt' and len(python.drive) != 2:
            raise ValueError('独立Python必须位于本机盘符，不使用网络路径')
        self.store._regular(python)
        if self.store.market_data_dir is None:
            raise ValueError('尚未配置本地日线资料根')
        return config

    def _path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'U-[0-9a-f]{32}', identity):
            raise ValueError('本地更新身份无效')
        return self.store.lab.inside(self.folder, identity)

    def _read(self, identity):
        with self._lock:
            value = self.store.lab.read_json(self.store._regular(self._path(identity)/'job.json'))
            if value.get('schema_version') != SCHEMA or value.get('update_id') != identity or value.get('status') not in STATES:
                raise ValueError('本地更新状态不兼容')
            if value.get('record_sha256') != _sha(_encoded({key:entry for key,entry in value.items() if key!='record_sha256'})):
                raise ValueError('本地更新状态指纹不一致')
            self.store.lab.security(value['security'])
            return value

    def _all(self):
        jobs = []
        for path in self.folder.iterdir():
            self.store.lab.checked_path(path, reject_git=True)
            if path.name.startswith('.preview-'):
                continue
            jobs.append(self._read(path.name))
        return jobs

    def _save(self, job):
        with self._lock:
            destination = self._path(job['update_id'])/'job.json'
            if destination.exists():
                self.store._regular(destination)
            job['updated_at'] = self.store.lab.now()
            job['record_sha256'] = _sha(_encoded({key:value for key,value in job.items() if key!='record_sha256'}))
            temporary = destination.with_name('.job-'+uuid.uuid4().hex+'.tmp')
            self.store.lab.write_json(temporary, job)
            for attempt in range(10):
                try:
                    os.replace(temporary, destination)
                    break
                except OSError as error:
                    # Windows readers/security software can briefly hold the destination.
                    if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 9:
                        raise
                    wall_clock.sleep(.05)

    def _public(self, job):
        return deepcopy({key: job.get(key) for key in ('update_id', 'security', 'status', 'created_at', 'updated_at',
            'stage_label', 'summary', 'results', 'error', 'output_kind')})

    def public(self, sid):
        try:
            self._config()
            self._security(sid)
            available, reason = True, '仅明确预览、开始后读取本地资料与重算；不调用模型或Wind。'
        except (ValueError, OSError, KeyError, TypeError):
            available, reason = False, '本地数据更新未配置、来源缺失或证券身份不支持；原研究保留。'
        jobs = [job for job in self._all() if job['security']['security_id'] == sid]
        latest = max(jobs, key=lambda job: (job['created_at'], job['update_id'])) if jobs else None
        return {'available': available, 'reason': reason, 'latest': self._public(latest) if latest else None}

    def _security(self, sid):
        security = self.store.context(sid)['security']
        if (security['market'] != 'A-share' or security['exchange'] not in {'sh', 'sz'} or
                security['share_class'] != 'A' or security['currency'] != 'CNY' or not re.fullmatch(r'\d{6}', security['code'])):
            raise ValueError('本版仅支持已确认身份的沪深人民币A股一般企业')
        return security

    def _sources(self, security):
        code = security['exchange']+security['code']
        base = self.store.market_data_dir.parent
        paths = {'market': self.store.market_data_dir/(code+'.csv'),
            'financial': base/'stock-fin-data-xbx'/code/(code+'_一般企业.csv'),
            'calendar': base/'period_offset.csv'}
        index = base/'stock-main-index-data/sh000300.csv'
        if index.exists():
            paths['index'] = index
        return paths

    def _runtime_env(self):
        cache = self.store.lab.inside(self.store.root, '_workspace', 'platform', 'matplotlib-cache')
        cache.mkdir(parents=True,exist_ok=True)
        return {**os.environ, 'MPLCONFIGDIR':str(cache), 'MPLBACKEND':'Agg'}

    def _runtime(self, config):
        try:
            result = subprocess.run([config['technical_python'],'-I','-X','utf8','-B','-c',RUNTIME_PROBE],
                cwd=REPO,env=self._runtime_env(),capture_output=True,timeout=60)
            value = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) < 8192 else {}
            if (set(value) != {'probe','python','numpy','talib','matplotlib','plotly'} or value['probe']!='talib-sma-rsi/1'
                    or any(not isinstance(entry,str) or len(entry)>80 for entry in value.values())):
                raise ValueError('Runtime probe did not pass')
            return value
        except (OSError,ValueError,TypeError,subprocess.SubprocessError):
            raise ValueError('独立Python技术依赖未通过；请准备TA-Lib、NumPy及绘图库后重新预览。') from None

    def _algorithm(self, config, runtime=None):
        names = ['app/local_updates.py', 'app/local_financial.py', 'app/local_market.py', 'VERSION',
            'skills/stock-fundamental-analysis/SKILL.md', 'skills/stock-technical-analysis/SKILL.md',
            'skills/stock-technical-analysis/requirements.txt']
        names += [path.relative_to(REPO).as_posix() for path in
            sorted((REPO/'skills/stock-technical-analysis/scripts').glob('*.py'))]
        return _sha(_encoded({'files': {name: _sha((REPO/name).read_bytes()) for name in names},
                              'technical_python': config['technical_python'],
                              'runtime': self._runtime(config) if runtime is None else runtime,
                              'python_sha256': _sha(self.store._regular(Path(config['technical_python'])).read_bytes())}))

    def _freeze(self, folder, name, value, manifest):
        payload = value if isinstance(value, bytes) else _encoded(value)
        destination = self.store.lab.inside(folder, name)
        with destination.open('xb') as output:
            output.write(payload)
        manifest[name] = _sha(payload)

    def preview(self, sid):
        with self._lock:
            if self._closing:
                raise FileExistsError('平台正在停止，请稍后重试')
            config, security = self._config(), self._security(sid)
            observed = self.store.lab.now()
            paths = self._sources(security)
            financial = load_financial(paths['financial'], security, asof=observed, observed_at=observed)
            market = load_market(paths['market'], security, asof=observed, observed_at=observed)
            calendar = load_calendar(paths['calendar'], asof=observed, observed_at=observed)
            index = local_market.load_index(paths['index'],asof=observed,observed_at=observed) if 'index' in paths else None
            source_hashes = {'financial': financial['provenance']['source_sha256'],
                'market': market['source_sha'], 'calendar': calendar['source_sha']}
            if index:
                source_hashes['index'] = index['source_sha']
            raw = {key: self.store._regular(path).read_bytes() for key, path in paths.items()}
            if any(_sha(raw[key]) != sha for key, sha in source_hashes.items()):
                raise ValueError('本地来源在预览读取期间变化，请重新预览')
            runtime = self._runtime(config)
            algorithm = self._algorithm(config,runtime)
            selection = _selection(financial, market, index)
            identity = 'U-'+_sha(_encoded({'security': security, 'sources': source_hashes,
                                         'algorithm': algorithm, 'eligible_selection': selection}))[:32]
            if self._path(identity).exists():
                return self._public(self._read(identity))
            context = self.store.context(sid, observed)
            parents = {module: context['roles']['formal_'+module]['result_id'] if context['roles']['formal_'+module] else None
                       for module in ('fundamental', 'technical')}
            previous = None
            if parents['technical']:
                record = next(row for row in context['results'] if row['result_id'] == parents['technical'])
                if any(row['relative_path'] == 'analysis.json' for row in record.get('attachments', [])):
                    previous = self.store._record_reader(record)('analysis.json')[0]
            manifest = {}
            staging = self.store.lab.inside(self.folder, '.preview-'+uuid.uuid4().hex)
            staging.mkdir()
            for key, value in raw.items():
                self._freeze(staging, 'raw-'+key+'.csv', value, manifest)
            for name, value in [('financial.json', financial), ('market.json', market), ('calendar.json', calendar),
                                ('daily-bars.json', market['bars'])]:
                self._freeze(staging, name, value, manifest)
            if index:
                self._freeze(staging,'index.json',index,manifest)
            if previous is not None:
                self._freeze(staging, 'previous-analysis.json', previous, manifest)
            summary = {'financial_period': financial['latest_ytd']['report_date'],
                'financial_versions': financial['provenance']['visible_versions'], 'market_last_date': market['lastdate'],
                'daily_bars': len(market['bars']), 'calendar_scope': calendar.get('2026_crosscheck'),
                'limitations': financial['provenance']['limitations']+market['provenance']['limitations']+calendar['limitations']+
                    (index['provenance']['limitations'] if index else ['未提供同口径市场背景，相关层缺失；不拿个股代替基准。'])+
                    ['本次只形成财务数据更新简报和技术重算，不刷新全公司商业观点或关闭原关注项。',
                     '本次没有新行业组合；市场原价背景不是行业排名或含息总收益。']}
            job = {'schema_version': SCHEMA, 'update_id': identity, 'security': security, 'status': 'prepared',
                'created_at': observed, 'updated_at': observed, 'observed_at': observed, 'stage_label': '本地快照已冻结，尚未开始重算',
                'summary': summary, 'results': [], 'error': None, 'output_kind': 'quantitative_update',
                'source_paths': {key: str(path) for key, path in paths.items()}, 'source_hashes': source_hashes,
                'algorithm': algorithm, 'eligible_selection': selection, 'parents': parents, 'files': manifest,
                'runtime': runtime,
                'result_ids': {module: 'U-'+('F-' if module == 'fundamental' else 'T-')+security['code']+'-'+identity[2:22]
                               for module in ('fundamental', 'technical')}}
            job['record_sha256'] = _sha(_encoded(job))
            self.store.lab.write_json(staging/'job.json', job)
            staging.rename(self._path(identity))
            return self._public(job)

    def _verify(self, job, *, current=True):
        folder = self._path(job['update_id'])
        sources = {key: str(path) for key, path in self._sources(job['security']).items()}
        if sources != job['source_paths']:
            raise ValueError('本地来源路径与固定证券合同不一致')
        expected = 'U-'+_sha(_encoded({'security':job['security'],'sources':job['source_hashes'],
                                     'algorithm':job['algorithm'],'eligible_selection':job.get('eligible_selection')}))[:32]
        if job['update_id'] != expected:
            raise ValueError('更新身份与来源版本不一致')
        if self._algorithm(self._config()) != job['algorithm']:
            raise ValueError('更新算法或配置已变化，请重新预览')
        for name, sha in job['files'].items():
            if Path(name).name != name or _sha(self.store._regular(folder/name).read_bytes()) != sha:
                raise ValueError('冻结材料指纹不一致，请重新预览')
        frozen_selection = _selection(self._read_frozen(job, 'financial.json'), self._read_frozen(job, 'market.json'),
            self._read_frozen(job, 'index.json') if 'index.json' in job['files'] else None)
        if frozen_selection != job.get('eligible_selection'):
            raise ValueError('冻结材料可用版本与更新身份不一致，请重新预览')
        if current and any(_sha(self.store._regular(Path(path)).read_bytes()) != job['source_hashes'][key]
                           for key, path in job['source_paths'].items()):
            raise ValueError('预览后本地来源已变化，请重新预览')

    def start(self, sid, update_id):
        with self._lock:
            if self._closing:
                raise FileExistsError('平台正在停止，请稍后重试')
            job = self._read(update_id)
            if job['security']['security_id'] != sid:
                raise ValueError('更新不属于当前证券')
            if job['status'] == 'completed' or self._active == update_id:
                return self._public(job)
            if self._thread and self._thread.is_alive():
                raise FileExistsError('另一项本地重算正在进行，请等待完成')
            self._verify(job)
            job.update(status='queued', stage_label='已排入本地重算', error=None)
            self._save(job)
            self._active = update_id
            self._thread = threading.Thread(target=self._run, args=(update_id,), name='local-data-update', daemon=False)
            self._thread.start()
            return self._public(job)

    def _stage(self, job, label):
        with self._lock:
            job.update(stage_label=label)
            self._save(job)

    def _read_frozen(self, job, name):
        payload = self.store._regular(self._path(job['update_id'])/name).read_bytes()
        if _sha(payload) != job['files'][name]:
            raise ValueError('Frozen source changed')
        return json.loads(payload.decode('utf-8-sig'))

    def _component(self, job, module):
        return {'schema_version': 'stock-research-components/1', 'security': job['security'],
            'result_id': job['result_ids'][module], 'content_role': 'incremental_research', 'components': [],
            'inheritance_only': False, 'new_quantitative_evidence': True,
            'local_data_update': {'schema_version': 'stock-local-data-update/1', 'update_id': job['update_id']},
            'limits': ['这是本地供应商数据更新及重算，未冒称重读一手原件或新的全公司深度观点。']}

    def _existing(self, job, module):
        context = self.store.context(job['security']['security_id'])
        found = next((row for row in context['results'] if row['result_id'] == job['result_ids'][module]), None)
        if found:
            reader = self.store._record_reader(found)
            reader()
            component = json.loads(reader('research-components.json')[0])
            provenance = json.loads(reader('local-update-provenance.json')[0])
            if (found['module'] != module or component != self._component(job,module) or
                    provenance.get('update_id') != job['update_id'] or provenance.get('source_hashes') != job['source_hashes'] or
                    provenance.get('algorithm') != job['algorithm']):
                raise ValueError('Existing result does not bind this update')
        return found

    def _archive(self, job, module, directory, data_asof):
        names = [path for path in sorted(directory.iterdir()) if path.suffix in MEDIA and path.name != 'archive-status.json']
        self.store.lab.archive(self.store.root, {'security': job['security'], 'module': module,
            'module_version': '0.3.2' if module == 'fundamental' else self._technical_version(),
            'result_id': job['result_ids'][module], 'generated_at': self.store.lab.now(),
            'research_asof': job['observed_at'], 'data_asof': data_asof,
            'parent_result_ids': [job['parents'][module]] if job['parents'][module] else [],
            'report_file': str(directory/'report.md'), 'attachments': [
                {'relative_path': path.name, 'source_file': str(path), 'media_type': MEDIA[path.suffix]} for path in names],
            'evidence': [], 'items': []})

    def _technical_version(self):
        text = (REPO/'skills/stock-technical-analysis/SKILL.md').read_text(encoding='utf-8-sig')
        values = re.findall(r'^  version: ["\x27]?(\d+\.\d+\.\d+)', text, re.MULTILINE)
        if len(values) != 1:
            raise ValueError('Technical Skill version is not unique')
        return values[0]

    def _financial_output(self, job, folder):
        finance, market = self._read_frozen(job, 'financial.json'), self._read_frozen(job, 'market.json')
        directory = folder/'fundamental-output'
        directory.mkdir(exist_ok=True)
        document = deepcopy(finance['document'])
        document.update(result_id=job['result_ids']['fundamental'], local_data_update=self._component(job,'fundamental')['local_data_update'])
        for rows in document['tables'].values():
            for row in rows:
                if row['period_basis'] in {'annual', 'single_quarter'}:
                    prices = [bar for bar in market['bars'] if bar['time'][:10] <= row['report_date']]
                    row.update(raw_close=prices[-1]['close'] if prices else None, price_date=prices[-1]['time'][:10] if prices else None,
                               pe_known=None, known_period=None)
        latest = finance['latest_ytd']
        previous = next((row for row in document['tables']['cumulative']
                         if row['report_date'] == str(int(latest['report_date'][:4])-1)+latest['report_date'][4:]), None)
        calculations = {'schema_version':'stock-local-financial-calculations/1', 'security':job['security'],
            'result_id':job['result_ids']['fundamental'], 'generated_at':self.store.lab.now(),
            'unit':'CNY unless field is ratio/date/share/price',
            'latest_ytd':latest,'latest_quarter':finance['latest_quarter'],'ttm':finance['ttm'],'prior_ytd':previous,
            'market':{'raw_close':market['bars'][-1]['close'],'date':market['lastdate']},
            'ratios':{'ttm_pe_parent_profit':None}, 'limits':job['summary']['limitations']}
        for name,value in [('financial-bottom-tables.json',document),('calculations.json',calculations),
                           ('research-components.json',self._component(job,'fundamental')),
                           ('local-update-provenance.json',{'update_id':job['update_id'],'observed_at':job['observed_at'],
                             'source_hashes':job['source_hashes'],'algorithm':job['algorithm'],
                             'runtime':job['runtime'],
                             'financial':finance['provenance'],'market':market['provenance']})]:
            (directory/name).write_bytes(_encoded(value))
        amount = lambda value: '缺失' if value is None else f'{value/1e8:.4f}亿元'
        text = '# '+job['security']['company']+' 本地财务数据更新简报\n\n'
        text += '**研究结论：本次更新财务底表和计算；没有形成新的全公司商业观点或投资动作。**\n\n'
        text += f"财务所属期：{latest['report_date']}；本地所选公开版本：{latest['publish_date']}；实际取得：{job['observed_at']}。\n\n"
        text += '|指标|累计|最新单季|\n|---|---|---|\n'
        for label,key in [('营收','revenue'),('归母净利','parent_profit'),('合并净利','consolidated_profit'),('经营现金流','cfo'),('购建长期资产现金','capex'),('简化自由现金流','fcf')]:
            text += '|'+label+'|'+amount(latest.get(key))+'|'+amount(finance['latest_quarter'].get(key))+'|\n'
        text += '\n现金转化＝CFO／同期合并净利润；简化FCF＝CFO−购建长期资产现金，不等于可分配股东现金。单季由同年累计差分；缺值不填0。\n\n'
        text += '## 与原研究对账\n\n原正式基本面：'+str(job['parents']['fundamental'])+'。原报告、关注项和观点保留；本次按新取得供应商版本重算，并不自动证明旧判断成功或失败。版本明细及操作数见底表provenance。\n\n'
        text += '## 价格、估值与资料边界\n\n原价日期：'+market['lastdate']+'；收盘价：'+str(market['bars'][-1]['close'])+'元。股本／股类口径未统一，不重算PE或目标价；报告期末原价不等于当时已公开财报。\n\n'
        text += '\n'.join('- '+item for item in job['summary']['limitations'])+'\n'
        (directory/'report.md').write_text(text,encoding='utf-8')
        public_date = max(row['publish_date'] for row in document['tables']['cumulative'])
        public_day = datetime.combine(datetime.fromisoformat(public_date).date(), time.max, TZ).isoformat()
        self._archive(job,'fundamental',directory,public_day)

    def _calculate_technical(self, job, folder):
        market, calendar = self._read_frozen(job,'market.json'), self._read_frozen(job,'calendar.json')
        request = {'security':job['security'],'asof':job['observed_at'],'market_cutoff':market['market_cutoff'],
            'input_file':str(folder/'daily-bars.json'),'timeframe':'1d','timeframes':['1d','1w','1mo','6mo','1y'],
            'source':'frozen local stock-trading-data-pro raw CSV','source_family':'stock-trading-data-pro/raw',
            'snapshot_available_at':job['observed_at'],'data_kind':'market','bar_time_semantics':'bar_end_time',
            'completion_evidence':'Vendor date rows frozen after declared conservative full-day session upper bound',
            'session_evidence':calendar['session_source'],'adjustment':{'mode':'raw','verified':False,
                'evidence':'Nominal raw prices; full corporate-action qualification is not certified'},
            'validation':{'price_basis':False,'session':True,'completion':True,'source_identity':True,
                'evidence':'Exact security/header/source SHA and declared 15:30 conservative completion bound'},'calendar':calendar}
        if 'previous-analysis.json' in job['files']:
            request['previous_technical'] = {'input_file':str(folder/'previous-analysis.json'), 'result_id':job['parents']['technical'],
                'price_basis_compatible':False,'comparison_evidence':'Previous lineage and period-label compatibility not independently certified; historical comparison only'}
        if 'index.json' in job['files']:
            index = self._read_frozen(job,'index.json')
            basis = 'CNY nominal raw close diagnostic'
            request['market'] = {'id':'CSI300','label':'沪深300','currency':'CNY','comparable':True,'comparison_basis':basis,
                'snapshot_available_at':job['observed_at'],'source_sha256':job['source_hashes']['index'],'bars':index['bars']}
            request['background_comparison_basis'] = basis
        path = folder/'technical-request.json'
        path.write_bytes(_encoded(request))
        output = folder/('technical-output-'+uuid.uuid4().hex)
        completed = subprocess.run([self._config()['technical_python'],'-X','utf8','-B',
            str(REPO/'skills/stock-technical-analysis/scripts/technical_analysis.py'),'--mode','analyze',
            '--request-json',str(path),'--output-dir',str(output)],cwd=REPO,env=self._runtime_env(),capture_output=True,timeout=900)
        if completed.returncode:
            raise ValueError('Technical calculation did not complete')
        self._verify(job,current=False)
        analysis = json.loads(self.store._regular(output/'analysis.json').read_bytes())
        if analysis.get('security') != job['security'] or not analysis.get('timeframes',{}).get('1d',{}).get('bars'):
            raise ValueError('Technical output identity or daily bars invalid')
        if analysis['timeframes']['1d'].get('metadata',{}).get('input_sha256') != job['files']['daily-bars.json']:
            raise ValueError('Technical output does not bind the frozen daily bars')
        for frame in analysis['timeframes'].values():
            conventional = frame.get('topics',{}).get('conventional',{})
            version = conventional.get('backend_version')
            if frame.get('bars') and (conventional.get('status') not in {'ok','partial','insufficient_history'} or
                    conventional.get('backend') != 'talib' or not isinstance(version,str) or not version.strip() or
                    conventional.get('error') or conventional.get('errors')):
                raise ValueError('Required technical backend did not actually run')
        (output/'research-components.json').write_bytes(_encoded(self._component(job,'technical')))
        (output/'local-update-provenance.json').write_bytes(_encoded({'update_id':job['update_id'],'source_hashes':job['source_hashes'],
            'algorithm':job['algorithm'],'runtime':job['runtime'],'market':market['provenance'],'calendar':calendar}))
        self._archive(job,'technical',output,market['market_cutoff'])

    def _run(self, identity):
        job = self._read(identity)
        try:
            self._verify(job)
            job['status'] = 'running'
            self._stage(job,'正在核对冻结来源并重算财务')
            for module in ('fundamental','technical'):
                if not self._existing(job,module):
                    if module == 'fundamental':
                        self._financial_output(job,self._path(identity))
                    else:
                        self._stage(job,'正在重算五周期技术指标与图表')
                        self._calculate_technical(job,self._path(identity))
                row = {'result_id':job['result_ids'][module],'module':module,'report_id':'result:'+job['result_ids'][module]}
                job['results'] = [value for value in job['results'] if value['module'] != module]+[row]
                self._stage(job,'已追加归档'+('财务更新' if module=='fundamental' else '技术重算'))
            job.update(status='completed',stage_label='本地更新已完成，旧研究及关注项保留',error=None)
        except Exception:
            job.update(status='partial' if job['results'] else 'failed',stage_label='更新未全部完成，已归档部分和旧研究保留',
                error='来源、计算或归档未通过核验；请核对本地配置、资料和依赖后明确重试，不要删除旧档。')
        finally:
            with self._lock:
                try:
                    self._save(job)
                finally:
                    self._active = None

    def close(self):
        with self._lock:
            self._closing = True
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join()
