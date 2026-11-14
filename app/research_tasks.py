"""User-started durable research jobs; models never write the authoritative archive."""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import uuid


SCHEMA = 'stock-research-task/1'
ACTIVE = {'queued', 'running', 'waiting_input', 'cancelling', 'archiving', 'partial'}
RECOVER = {'queued', 'running', 'cancelling', 'archiving'}
START_KEYS = {'security_id', 'module', 'provider', 'mode', 'research_cutoff', 'question', 'scope',
              'source_ids', 'item_ids', 'idempotency_key'}
STATES = ACTIVE | {'cancelled', 'interrupted', 'failed', 'completed'}
SHANGHAI = timezone(timedelta(hours=8))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class ResearchTasks:
    def __init__(self, store, project_dir, *, config=None, runner=None, materials=None, autostart=True):
        self.store, self.project_dir = store, store.lab.checked_path(Path(project_dir))
        self.folder = store.lab.inside(store.root, '_workspace', 'platform', 'tasks')
        self.folder.mkdir(parents=True, exist_ok=True)
        self.config_override, self.runner, self.materials = config, runner, materials
        self._lock, self._queue = threading.RLock(), queue.Queue()
        self._closing, self._cancel = threading.Event(), threading.Event()
        self._active_id, self._thread = None, None
        self.autostart = autostart
        with self._lock:
            for record in self._all():
                if record['status'] in RECOVER:
                    record['status'] = 'interrupted'
                    self._event(record, '等待恢复', '上次执行已中断；请确认后恢复，不会自动再次调用模型。')
                    self._save(record)
        if autostart:
            self._thread = threading.Thread(target=self._loop, daemon=True, name='research-worker')
            self._thread.start()

    def _config(self):
        if self.config_override is not None:
            value = dict(self.config_override)
        else:
            path = self.store.lab.inside(self.store.root, '_workspace', 'platform', 'research-engine.json')
            if not path.exists():
                return {}
            if self.store._regular(path).stat().st_size > 65536:
                raise ValueError('Engine configuration exceeds its limit')
            value = self.store.lab.read_json(path)
        if not value:
            return {}
        if str(self.store.lab.checked_path(Path(value['project_dir'])).resolve()).casefold() != str(self.project_dir.resolve()).casefold():
            raise ValueError('Research engine must use the configured dedicated project')
        return value

    def providers(self):
        try:
            config = self._config()
            configured = bool(config.get('project_id') and config.get('codex_executable'))
            if configured:
                self.store._regular(Path(config['codex_executable']))
            secret_file = config.get('deepseek_key_file')
            deepseek = bool(configured and (config.get('deepseek_key_env') and os.environ.get(config['deepseek_key_env'])
                                           or secret_file and self.store._regular(Path(secret_file)).is_file()))
        except (OSError, ValueError, KeyError, TypeError):
            configured, deepseek = False, False
        return [{'provider': name, 'label': label, 'available': available,
                 'reason': '已配置，启动时核对账户与受控运行条件；费用沿原服务，未知。' if available else '尚未完成本机研究引擎配置。'}
                for name, label, available in [('codex', 'Codex 执行', configured), ('deepseek', 'DeepSeek API', deepseek)]]

    def _path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'R4-[0-9a-f]{32}', task_id):
            raise ValueError('Invalid task identity')
        return self.store.lab.inside(self.folder, task_id, 'task.json')

    def _read(self, task_id):
        value = self.store.lab.read_json(self.store._regular(self._path(task_id)))
        if (value.get('schema_version') != SCHEMA or value.get('task_id') != task_id
                or value.get('status') not in STATES or self.store.lab.security(value.get('security'))['security_id'] != value.get('security_id')):
            raise ValueError('Task identity or state is invalid')
        if value.get('revision_token') != digest({key: row for key, row in value.items() if key != 'revision_token'}):
            raise ValueError('Task record fingerprint differs')
        return value

    def _all(self):
        records = []
        self.store.lab.checked_path(self.folder, reject_git=True)
        for path in self.folder.iterdir():
            self.store.lab.checked_path(path, reject_git=True)
            if not path.is_dir():
                raise ValueError('Unexpected task directory entry')
            if (path / 'task.json').exists():
                records.append(self._read(path.name))
        return sorted(records, key=lambda row: (row['created_at'], row['task_id']), reverse=True)

    def _event(self, record, label, text=''):
        record['stage_label'] = label
        record['updated_at'] = self.store.lab.now()
        record['events'].append({'at': record['updated_at'], 'label': label, 'text': text})

    def _save(self, record):
        record['revision_token'] = digest({key: value for key, value in record.items() if key != 'revision_token'})
        destination = self._path(record['task_id'])
        with self.store.lab.writer(self.store.root):
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.store.lab.inside(destination.parent, '.task-' + uuid.uuid4().hex + '.json')
            try:
                self.store.lab.write_json(temporary, record)
                self.store.lab.checked_path(destination, reject_git=True)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _public(self, record):
        fields = ('task_id', 'security', 'security_id', 'module', 'provider', 'mode', 'research_cutoff', 'research_asof',
                  'question', 'scope', 'source_ids', 'item_ids', 'status', 'stage_label', 'created_at', 'updated_at',
                  'revision_token', 'events', 'questions', 'results', 'review_ids', 'failure_reason', 'attempt_count', 'warning', 'source_usage')
        return {key: record[key] for key in fields if key in record} | {
            'can_cancel': record['status'] in {'queued', 'running', 'waiting_input'},
            'can_resume': record['status'] in {'cancelled', 'interrupted', 'failed', 'partial'},
            'fee_status': 'unknown', 'conversation_visible': False, 'error': record.get('failure_reason')}

    def list(self):
        with self._lock:
            return {'tasks': [self._public(row) for row in self._all()], 'providers': self.providers()}

    def get(self, task_id):
        with self._lock:
            return {'task': self._public(self._read(task_id))}

    def _request(self, body):
        if not isinstance(body, dict) or set(body) != START_KEYS:
            raise ValueError('Research start requires its exact declared fields')
        result = dict(body)
        self.store.lab.identifier(result['security_id'], 'security_id')
        if result['module'] not in {'fundamental', 'technical', 'joint'} or result['mode'] not in {'full', 'continue', 'broker_update'}:
            raise ValueError('Unsupported research scope or mode')
        if result['provider'] not in {'codex', 'deepseek'}:
            raise ValueError('Unsupported model source')
        for key in ('question', 'scope'):
            if not isinstance(result[key], str) or len(result[key]) > 2000:
                raise ValueError('Research text exceeds its limit')
            result[key] = result[key].strip()
        token = result['idempotency_key']
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,96}', token):
            raise ValueError('Use a stable submission identity')
        for key, limit in [('source_ids', 5), ('item_ids', 50)]:
            values = result[key]
            if not isinstance(values, list) or len(values) > limit or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
                raise ValueError('Invalid selected research references')
            for value in values:
                self.store.lab.identifier(value, key)
            result[key] = sorted(values)
        if result['mode'] == 'broker_update' and (not result['source_ids'] or result['module'] != 'fundamental'):
            raise ValueError('Broker updates require a selected source and fundamental scope')
        day = date.fromisoformat(result['research_cutoff'])
        if day.isoformat() != result['research_cutoff']:
            raise ValueError('Use a complete research date')
        now = self.store.lab.stamp(self.store.lab.now(), 'now').astimezone(SHANGHAI)
        if day > now.date():
            raise ValueError('Research cutoff cannot be in the future')
        cutoff = now if day == now.date() else datetime.combine(day, time.max, SHANGHAI)
        return result, cutoff.isoformat()

    def start(self, body):
        body, asof = self._request(body)
        fingerprint = digest({key: value for key, value in body.items() if key != 'idempotency_key'})
        with self._lock:
            if self._closing.is_set():
                raise FileExistsError('Service is stopping; wait for the current archive commit')
            for old in self._all():
                if old['idempotency_key'] == body['idempotency_key']:
                    if old['request_fingerprint'] != fingerprint:
                        raise FileExistsError('Submission identity belongs to a different request')
                    return {'task': self._public(old), 'reused': True}
                if old['request_fingerprint'] == fingerprint and old['status'] in ACTIVE:
                    return {'task': self._public(old), 'reused': True}
            if not next(row for row in self.providers() if row['provider'] == body['provider'])['available']:
                raise ValueError('Research source is not configured')
            context = self.store.context(body['security_id'], asof)
            modules = ['fundamental', 'technical'] if body['module'] == 'joint' else [body['module']]
            allowed = {item['item_id'] for result in context['results'] if result['module'] in modules for item in result['item_states']}
            if not set(body['item_ids']) <= allowed:
                raise ValueError('A selected old item is not visible for this security and cutoff')
            if not body['item_ids']:
                from .research_materials import require_review_batch
                require_review_batch(context['pending_items'], modules)
            if body['source_ids']:
                from .broker_cache import source_detail
                for source_id in body['source_ids']:
                    source = source_detail(self.store, body['security_id'], source_id, context=context)
                    if source['content_status'] != 'text_reviewed' or not source.get('formal_archive'):
                        raise ValueError('Selected source must first be reviewed and formally archived')
                    if (not source.get('publication_date') or len(source['publication_date']) != 10
                            or source['publication_date'] > self.store.lab.stamp(asof, 'asof').astimezone(SHANGHAI).date().isoformat()):
                        raise ValueError('Selected source publication date exceeds the research cutoff or remains unknown')
            identity, stamp = uuid.uuid4().hex, self.store.lab.now()
            record = {**body, 'schema_version': SCHEMA, 'task_id': 'R4-' + identity, 'security': context['security'],
                'research_asof': asof, 'request_fingerprint': fingerprint, 'status': 'queued',
                'created_at': stamp, 'updated_at': stamp, 'stage_label': '已加入研究任务', 'events': [],
                'questions': [], 'results': [], 'review_ids': [], 'attempt_count': 0,
                'result_ids': {name: 'R4-' + ('F-' if name == 'fundamental' else 'T-') + context['security']['code'] + '-' + identity[:20] for name in modules},
                'review_id': 'R4-RV-' + identity, 'engine_fingerprint': digest(self._config()),
                'session': None, 'output_file': None, 'output_sha256': None, 'input_fingerprint': None, 'input_attempt': None}
            record.update(consumed_turn_id=None, consumed_turn_intent=None)
            self._event(record, '已加入研究任务', '研究对象、来源、日期和范围已冻结；只有本次明确启动才调用模型。')
            self._save(record)
            if self.autostart:
                self._queue.put(record['task_id'])
            return {'task': self._public(record), 'reused': False}

    def _selected(self, body, fields):
        if not isinstance(body, dict) or set(body) != fields:
            raise ValueError('Invalid research operation')
        record = self._read(body['task_id'])
        if record['revision_token'] != body['revision_token']:
            raise FileExistsError('Task changed; read its latest state before continuing')
        return record

    def cancel(self, body):
        with self._lock:
            record = self._selected(body, {'task_id', 'revision_token'})
            if record['status'] in {'cancelled', 'completed'}:
                return {'task': self._public(record)}
            if record['status'] == 'archiving':
                raise FileExistsError('Archive commit is in progress; saved records cannot be cancelled')
            if record['status'] not in {'queued', 'running', 'waiting_input'}:
                raise ValueError('This task is not running')
            record['status'] = 'cancelling' if record['status'] == 'running' else 'cancelled'
            if self._active_id == record['task_id']:
                self._cancel.set()
            self._event(record, '正在取消' if record['status'] == 'cancelling' else '已取消',
                        '保留任务进展和已有成果；取消不删除原研究。')
            self._save(record)
            return {'task': self._public(record)}

    def resume(self, body):
        with self._lock:
            if self._closing.is_set():
                raise FileExistsError('Service is stopping')
            record = self._selected(body, {'task_id', 'revision_token'})
            if record['status'] not in {'cancelled', 'interrupted', 'failed', 'partial'}:
                raise ValueError('Only a stopped or incomplete task can be resumed')
            if digest(self._config()) != record['engine_fingerprint']:
                raise ValueError('Research configuration changed; start a new task instead of switching its source')
            record['status'] = 'waiting_input' if any(row['status'] == 'pending' for row in record['questions']) else 'queued'
            record.pop('failure_reason', None)
            self._event(record, '等待补充信息' if record['status'] == 'waiting_input' else '等待继续',
                        '复用原任务、问题、输入和会话；已有模型结果时仅继续缺失的归档。')
            self._save(record)
            if self.autostart and record['status'] == 'queued':
                self._queue.put(record['task_id'])
            return {'task': self._public(record)}

    def answer(self, body):
        with self._lock:
            if self._closing.is_set():
                raise FileExistsError('Service is stopping')
            record = self._selected(body, {'task_id', 'question_id', 'answers', 'revision_token'})
            if record['status'] != 'waiting_input':
                raise ValueError('This task is not waiting for an answer')
            text = body['answers']
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise ValueError('Answer must contain 1–4000 characters')
            question = next((row for row in record['questions'] if row['question_id'] == body['question_id'] and row['status'] == 'pending'), None)
            if question is None:
                raise FileExistsError('Question already answered or belongs to another task')
            question.update(status='answered', answer=text.strip(), answered_at=self.store.lab.now())
            if not any(row['status'] == 'pending' for row in record['questions']):
                record['status'] = 'queued'
                record['output_file'], record['output_sha256'] = None, None
            self._event(record, '已记录补充信息', '答案只用于本任务；不会改写原研究截止或已归档事实。')
            self._save(record)
            if self.autostart and record['status'] == 'queued':
                self._queue.put(record['task_id'])
            return {'task': self._public(record)}

    def _update(self, task_id, change):
        with self._lock:
            record = self._read(task_id)
            change(record)
            self._save(record)
            return record

    def source_status(self, security_id):
        with self._lock:
            result = {}
            for task in self._all():
                if task['security_id'] != security_id or task['status'] != 'completed':
                    continue
                for usage in task.get('source_usage', []):
                    result.setdefault(usage['source_id'], []).append({
                        'task_id': task['task_id'], 'completed_at': task['updated_at'],
                        'result_ids': usage['result_ids'], 'status': usage['status'],
                        'coverage': usage.get('coverage'), 'formal_replaced': usage.get('formal_replaced', False)})
            return result

    def _loop(self):
        while not self._closing.is_set():
            task_id = self._queue.get()
            if task_id is None:
                break
            try:
                self.run_one(task_id)
            except Exception:
                with self._lock:
                    self._active_id = None
                try:
                    def failed_before_turn(record):
                        record['status'] = 'failed'
                        self._event(record, '任务暂不可执行', '任务状态未能保存，未自动重试；请核对后恢复。')
                    self._update(task_id, failed_before_turn)
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def run_one(self, task_id):
        with self._lock:
            record = self._read(task_id)
            if record['status'] != 'queued' or self._active_id:
                return
            self._active_id, self._cancel = task_id, threading.Event()
            record['status'] = 'running'
            record['attempt_count'] += 1
            self._event(record, '准备研究资料', '核对原研究、旧关注项、所选资料与日期口径。')
            try:
                self._save(record)
            except Exception:
                self._active_id = None
                raise
        try:
            from . import research_materials
            materials = self.materials or research_materials
            attempt = self.store.lab.inside(self._path(task_id).parent, 'attempts', f"attempt-{record['attempt_count']:04d}")
            attempt.mkdir(parents=True, exist_ok=False)
            if record.get('input_attempt'):
                previous = self.store.lab.inside(self._path(task_id).parent, *Path(record['input_attempt']).parts)
                snapshot = previous / 'frozen-input.json'
                if snapshot.exists():
                    shutil.copyfile(self.store._regular(snapshot), attempt / 'frozen-input.json')
                    inputs = self.store.lab.inside(previous, 'input')
                    if inputs.exists():
                        for folder, directories, filenames in os.walk(inputs, followlinks=False):
                            self.store.lab.checked_path(Path(folder), reject_git=True)
                            for name in directories:
                                self.store.lab.checked_path(Path(folder)/name, reject_git=True)
                            for name in filenames:
                                path = self.store._regular(Path(folder)/name)
                                destination = self.store.lab.inside(attempt, 'input', *path.relative_to(inputs).parts)
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(path, destination)
            prepared = materials.prepare(self.store, record, attempt)
            if record['input_fingerprint'] and prepared['input_fingerprint'] != record['input_fingerprint']:
                raise ValueError('Frozen research inputs changed')
            record = self._update(task_id, lambda row: row.update(input_fingerprint=prepared['input_fingerprint'],
                input_attempt=row.get('input_attempt') or attempt.relative_to(self._path(task_id).parent).as_posix()))
            output = None
            if record['output_file']:
                saved = self.store.lab.inside(self._path(task_id).parent, *Path(record['output_file']).parts)
                output = json.loads(self.store._bytes(saved, record['output_sha256']))
            if output is None and not self._cancel.is_set() and not self._closing.is_set():
                from .research_provider import run_turn, recover_turn
                prompt = prepared['prompt']
                if record.get('validation_feedback'):
                    prompt += '\n上轮结构校验未通过，请只修复结构并完整返回，勿请求工具：' + record['validation_feedback']
                answered = [row for row in record['questions'] if row['status'] == 'answered']
                if answered:
                    prompt += '\n本任务补充回答（用户信息，不自动视为已核实财务事实）：\n' + json.dumps(answered, ensure_ascii=False)
                def emit(event):
                    if not isinstance(event, dict):
                        return
                    kind = event.get('kind')
                    def changed(row):
                        if kind == 'session':
                            row['session'] = event['session']
                        elif kind == 'stage':
                            self._event(row, str(event.get('label', '正在研究'))[:100], str(event.get('text', ''))[:500])
                    if kind in {'session', 'stage'}:
                        self._update(task_id, changed)
                config = self._config()
                if digest(config) != record['engine_fingerprint']:
                    raise ValueError('Frozen research engine changed')
                result = None
                consumed = bool(record['session'] and (
                    record.get('consumed_turn_id') and record['session'].get('turn_id') == record['consumed_turn_id'] or
                    record.get('consumed_turn_intent') and record['session'].get('pending_turn_intent') == record['consumed_turn_intent']))
                if record['session'] and not consumed and self.runner is None:
                    recovered = recover_turn(config, record['provider'], attempt, record['session'], emit=emit, cancel=self._cancel)
                    if recovered['status'] == 'completed':
                        result = recovered
                    elif recovered['status'] != 'retryable':
                        self._update(task_id, lambda row: row.update(warning='上次模型请求状态尚未核清，本次未重发。请稍后恢复以继续核对，避免重复调用。'))
                        raise RuntimeError('Previous turn outcome is not confirmed; no new model request was made')
                if result is None:
                    result = (self.runner or run_turn)(config, record['provider'], prompt, attempt,
                        session=record['session'], emit=emit, cancel=self._cancel, output_schema=prepared['output_schema'])
                if result.get('session'):
                    record = self._update(task_id, lambda row: row.update(session=result['session']))
                if result['status'] == 'completed':
                    try:
                        output = result.get('json')
                        if output is None:
                            output = json.loads(result['final_text'])
                        if not isinstance(output, dict) or output.get('status') not in {'completed', 'needs_input'}:
                            raise ValueError('Research response must contain a valid structured status')
                        if hasattr(materials, 'validate_output'):
                            snapshot = self.store.lab.read_json(attempt / prepared['frozen_input_file'])
                            materials.validate_output(record, snapshot, output)
                        payload = json.dumps(output, ensure_ascii=False, allow_nan=False).encode('utf-8')
                        if len(payload) > 2 * 1024 * 1024:
                            raise ValueError('Research response exceeds its limit')
                    except (ValueError, TypeError, UnicodeError) as error:
                        def invalid_output(row):
                            row['consumed_turn_id'] = (row.get('session') or {}).get('turn_id')
                            row['consumed_turn_intent'] = (row.get('session') or {}).get('pending_turn_intent')
                            row['validation_feedback'] = str(error)[:1000]
                            row['warning'] = '上轮已完成，但研究内容或结构未通过校验，尚未归档。显式恢复会在原会话修正，可能继续消耗所选服务。'
                        self._update(task_id, invalid_output)
                        raise
                    destination = self.store.lab.inside(attempt, 'model-output.json')
                    with destination.open('xb') as handle:
                        handle.write(payload)
                    relative = destination.relative_to(self._path(task_id).parent).as_posix()
                    record = self._update(task_id, lambda row: row.update(output_file=relative,
                        output_sha256=hashlib.sha256(payload).hexdigest(), validation_feedback=None,
                        consumed_turn_id=(row.get('session') or {}).get('turn_id'),
                        consumed_turn_intent=(row.get('session') or {}).get('pending_turn_intent')))
                elif result['status'] != 'cancelled':
                    raise RuntimeError('Research provider did not complete')
            if self._cancel.is_set() or self._closing.is_set() or output is None:
                def stopped(row):
                    row['status'] = 'interrupted' if self._closing.is_set() else 'cancelled'
                    self._event(row, '等待恢复' if self._closing.is_set() else '已取消', '原研究和已保存任务进展保留。')
                self._update(task_id, stopped)
                return
            if not isinstance(output, dict) or output.get('status') not in {'completed', 'needs_input'}:
                raise ValueError('Research response status is invalid')
            if output['status'] == 'needs_input':
                questions = output.get('questions')
                if not isinstance(questions, list) or not 1 <= len(questions) <= 3 or output.get('reports'):
                    raise ValueError('A waiting task must contain questions rather than final reports')
                normalized = []
                for index, value in enumerate(questions):
                    if not isinstance(value, dict) or not isinstance(value.get('question'), str) or not 1 <= len(value['question']) <= 2000:
                        raise ValueError('Invalid research question')
                    options = value.get('options', [])
                    if not isinstance(options, list) or len(options) > 6 or any(not isinstance(item, str) or len(item) > 300 for item in options):
                        raise ValueError('Invalid research question choices')
                    normalized.append({'question_id': task_id + '-Q' + str(record['attempt_count']) + '-' + str(index + 1),
                        'prompt': value['question'], 'options': options, 'required': True, 'status': 'pending'})
                def waiting(row):
                    row['questions'].extend(normalized)
                    stopped = self._cancel.is_set() or self._closing.is_set() or row['status'] == 'cancelling'
                    row['status'] = ('interrupted' if self._closing.is_set() else 'cancelled') if stopped else 'waiting_input'
                    self._event(row, '已保留问题并停止' if stopped else '等待补充信息',
                                '恢复后保留原问题；等待期间不继续调用模型。' if stopped else '回答后在原会话继续；等待期间不继续调用模型。')
                self._update(task_id, waiting)
                return
            with self._lock:
                record = self._read(task_id)
                if self._cancel.is_set() or record['status'] == 'cancelling':
                    record['status'] = 'cancelled'
                    self._event(record, '已取消', '分析结果暂存，恢复后才继续归档。')
                    self._save(record)
                    return
                record['status'] = 'archiving'
                self._event(record, '核验并归档', '按预分配成果ID追加保存，不覆盖旧版本。')
                self._save(record)
            archived = materials.validate_and_archive(self.store, record, attempt, output, cancel=None)
            def completed(row):
                row['results'], row['review_ids'] = archived['results'], archived.get('review_ids', [])
                row['source_usage'] = archived.get('source_usage', [])
                if archived.get('warnings'):
                    row['warning'] = '；'.join(archived['warnings'])
                row['status'] = 'completed'
                self._event(row, '研究已归档', '新成果可在个股看板和档案室查阅，原报告和原冻结项保留。')
            self._update(task_id, completed)
        except Exception as error:
            from .research_materials import ResearchSelectionRequired
            def failed(row):
                _, catalog = self.store._scan()
                found = [entry for entry in catalog['results'] if entry['result_id'] in row['result_ids'].values()]
                row['results'] = [{'result_id': entry['result_id'], 'report_id': 'result:' + entry['result_id'], 'module': entry['module']} for entry in found]
                row['status'] = 'partial' if found else 'failed'
                row['failure_reason'] = str(error) if isinstance(error, ResearchSelectionRequired) else (
                    '已有部分成果保存；请恢复以核对和补齐，原研究保留。' if found else
                    '研究未完成，已保留任务和旧档。请核对资料或引擎配置后恢复。')
                row['failure_type'] = type(error).__name__
                self._event(row, '待恢复完成' if found else '研究暂未完成', row['failure_reason'])
            try:
                self._update(task_id, failed)
            except Exception:
                # The durable pre-commit state is recovered on the next start;
                # never retry a paid turn merely because status persistence failed.
                pass
        finally:
            with self._lock:
                self._active_id = None

    def close(self):
        self._closing.set()
        self._cancel.set()
        self._queue.put(None)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
