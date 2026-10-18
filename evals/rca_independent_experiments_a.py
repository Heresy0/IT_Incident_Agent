"""Four independent, bounded local fault interventions for RCA evaluation.

Each case executes real standard-library operations in baseline, fault and
restored phases.  Public rows are projections of those observed operations;
the reference interpretation is separate and never constitutes human gold.
No business application or network client is imported.
"""
from contextlib import closing
import gzip
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
from pathlib import Path
import zlib


SCHEMA_VERSION = 'rca-independent-program-experiment-v1'
CASE_IDS = tuple('rca_independent_' + str(index).zfill(3) for index in range(1, 5))


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _check(name, passed):
    return {'name': name, 'passed': bool(passed)}


def _row(observation_id, category, message):
    return {'observation_id': observation_id, 'category': category, 'message': message}


def _finish(case_id, family, parameters, trials, checks, rows, reference):
    return {
        'schema_version': SCHEMA_VERSION,
        'case_id': case_id,
        'family': family,
        'origin': 'executed_synthetic_experiment',
        'human_verified': False,
        'is_gold': False,
        'human_review_status': 'pending',
        'causal_truth_validated': False,
        'status': 'passed' if all(item['passed'] for item in checks) else 'failed',
        'model_calls': 0,
        'business_application_imported': False,
        'original_incident_reproduced': False,
        'parameters': parameters,
        'trials': trials,
        'checks': checks,
        'public_rows': rows,
        'reference': reference,
    }


def _validate_repeats(repeats):
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 5:
        raise ValueError('repeats must be an integer between 1 and 5')


def _sqlite_write(connection, *, held_transaction):
    started = time.perf_counter()
    result = {'holder_transaction_open': held_transaction}
    try:
        connection.execute('UPDATE counters SET value = value + 1 WHERE id = 1')
        connection.commit()
        result.update({'outcome': 'committed', 'error_code': None,
            'value_after': connection.execute('SELECT value FROM counters WHERE id = 1').fetchone()[0]})
    except sqlite3.OperationalError as exc:
        connection.rollback()
        result.update({'outcome': 'failed', 'error_code': getattr(exc, 'sqlite_errorname', None),
            'error_number': getattr(exc, 'sqlite_errorcode', None), 'error_message': str(exc)})
    result['elapsed_ms'] = (time.perf_counter() - started) * 1000
    return result


def reproduce_sqlite_transaction_lock(repeats=3):
    """Two actual SQLite connections contend on one file, then release it."""
    _validate_repeats(repeats)
    parameters = {'database_engine': 'sqlite3', 'journal_mode': 'DELETE',
        'writer_timeout_ms': 20, 'holder_transaction': 'BEGIN IMMEDIATE',
        'statement': 'UPDATE counters SET value = value + 1 WHERE id = 1'}
    trials, checks = [], []
    with tempfile.TemporaryDirectory(prefix='rca-sqlite-lock-') as temporary:
        for repeat in range(1, repeats + 1):
            database = Path(temporary) / ('trial_' + str(repeat) + '.sqlite3')
            with closing(sqlite3.connect(database, timeout=0.02)) as holder, closing(
                    sqlite3.connect(database, timeout=0.02)) as writer:
                journal = holder.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
                holder.execute('CREATE TABLE counters(id INTEGER PRIMARY KEY, value INTEGER)')
                holder.execute('INSERT INTO counters VALUES (1, 0)')
                holder.commit()
                baseline = _sqlite_write(writer, held_transaction=holder.in_transaction)
                try:
                    holder.execute('BEGIN IMMEDIATE')
                    holder.execute('UPDATE counters SET value = value + 10 WHERE id = 1')
                    fault = _sqlite_write(writer, held_transaction=holder.in_transaction)
                finally:
                    holder.rollback()
                restored = _sqlite_write(writer, held_transaction=holder.in_transaction)
                trial = {'repeat': repeat, 'journal_mode_observed': journal,
                    'writer_busy_timeout_ms': writer.execute('PRAGMA busy_timeout').fetchone()[0],
                    'baseline': baseline, 'fault': fault, 'restored': restored,
                    'holder_transaction_closed': not holder.in_transaction,
                    'writer_transaction_closed': not writer.in_transaction}
                trials.append(trial)
                checks.extend([
                    _check(f'repeat_{repeat}_actual_lock_timeout',
                        baseline['outcome'] == 'committed' and baseline['value_after'] == 1
                        and fault['outcome'] == 'failed' and fault.get('error_number') == sqlite3.SQLITE_BUSY
                        and fault['holder_transaction_open'] and fault['elapsed_ms'] >= 10),
                    _check(f'repeat_{repeat}_release_restores_same_write',
                        restored['outcome'] == 'committed' and restored['value_after'] == 2
                        and not restored['holder_transaction_open']),
                    _check(f'repeat_{repeat}_connections_clean',
                        trial['holder_transaction_closed'] and trial['writer_transaction_closed']
                        and trial['writer_busy_timeout_ms'] == parameters['writer_timeout_ms']),
                ])
    rows = [
        _row('row_01', 'configuration', {'writer_busy_timeout_ms': parameters['writer_timeout_ms'],
            'transaction_statement': parameters['holder_transaction'],
            'journal_mode_by_repeat': [trial['journal_mode_observed'] for trial in trials],
            'holder_transaction_open_during_failed_write': [trial['fault']['holder_transaction_open'] for trial in trials]}),
        _row('row_02', 'dependency', {'operation': parameters['statement'],
            'failed_write_results': [{'repeat': trial['repeat'], **trial['fault']} for trial in trials]}),
        _row('row_03', 'dependency', {'intervention': 'rollback holder transaction; retry identical writer statement',
            'phase_results': [{'repeat': trial['repeat'],
                'outcomes': [trial[phase]['outcome'] for phase in ('baseline', 'fault', 'restored')],
                'committed_values': [trial[phase].get('value_after') for phase in ('baseline', 'fault', 'restored')],
                'holder_transaction_open': [trial[phase]['holder_transaction_open'] for phase in ('baseline', 'fault', 'restored')]}
                for trial in trials], 'phase_order': ['baseline', 'fault', 'restored']}),
        _row('row_04', 'dependency', {'holder_transaction_closed_after_trial': [trial['holder_transaction_closed'] for trial in trials],
            'writer_transaction_closed_after_trial': [trial['writer_transaction_closed'] for trial in trials]}),
    ]
    return _finish(CASE_IDS[0], 'sqlite_transaction_write_lock', parameters, trials, checks, rows, {
        'causal_condition': 'An uncommitted BEGIN IMMEDIATE transaction holds the SQLite write lock while a second connection has a 20 ms busy timeout.',
        'mechanism': 'The second connection cannot acquire the database write lock and returns SQLITE_BUSY; rollback of the holder permits the identical write.',
        'reference_explanation': 'The same file and writer statement commit before contention and after releasing the holder; only the held-transaction phase returns SQLITE_BUSY.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'dependency'],
        'acceptable_aliases': ['SQLite写锁被未提交事务持有', 'SQLite写事务锁竞争', '未提交事务导致database is locked', 'SQLite writer lock contention'],
        'forbidden_assertions': ['数据库进程崩溃', '网络超时', '连接池耗尽', 'deadlock confirmed', 'production incident reproduced'],
        'limitations': ['A local SQLite file and two connections are exercised; no remote database or production incident is asserted.',
            'This demonstrates bounded lock contention, not a database deadlock or the business reason the holder transaction was left open.'],
    })


def _indexed_lookup(connection, parameters):
    statement = parameters['statement']
    target = parameters['target_lookup']
    plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + statement, (target,))]
    callbacks = 0
    deadline = time.perf_counter() + 2.0
    interrupt_reason = None

    def progress():
        nonlocal callbacks, interrupt_reason
        callbacks += 1
        if callbacks * parameters['progress_interval_vm_instructions'] >= parameters['vm_instruction_budget']:
            interrupt_reason = 'vm_instruction_budget'
            return 1
        if time.perf_counter() >= deadline:
            interrupt_reason = 'safety_wall_deadline'
            return 1
        return 0

    result = {'query_plan': plan}
    connection.set_progress_handler(progress, parameters['progress_interval_vm_instructions'])
    try:
        records = connection.execute(statement, (target,)).fetchall()
        result.update({'outcome': 'returned', 'returned_payloads': [row[0] for row in records], 'error_code': None})
    except sqlite3.OperationalError as exc:
        result.update({'outcome': 'failed', 'error_code': getattr(exc, 'sqlite_errorname', None),
            'error_number': getattr(exc, 'sqlite_errorcode', None), 'error_message': str(exc)})
    finally:
        connection.set_progress_handler(None, 0)
    result.update({'progress_callbacks': callbacks,
        'observed_vm_progress_instructions': callbacks * parameters['progress_interval_vm_instructions'],
        'interrupt_reason': interrupt_reason})
    return result


def reproduce_sqlite_missing_index(repeats=3):
    """Index removal changes actual lookup VM work and exhausts a fixed budget."""
    _validate_repeats(repeats)
    parameters = {'database_engine': 'sqlite3', 'row_count': 4000, 'target_lookup': 3999,
        'automatic_index': False, 'index_name': 'idx_records_lookup',
        'statement': 'SELECT payload FROM records WHERE lookup = ?',
        'progress_interval_vm_instructions': 100, 'vm_instruction_budget': 1000,
        'safety_wall_timeout_seconds': 2.0}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        # Disable the Python statement cache so repeated EXPLAIN text cannot
        # report a stale cached plan after DROP INDEX.
        with closing(sqlite3.connect(':memory:', timeout=0.02, cached_statements=0)) as connection:
            connection.execute('PRAGMA automatic_index=OFF')
            connection.execute('CREATE TABLE records(lookup INTEGER, payload TEXT)')
            connection.executemany('INSERT INTO records VALUES (?, ?)',
                [(index, 'payload_' + str(index)) for index in range(parameters['row_count'])])
            connection.execute('CREATE INDEX idx_records_lookup ON records(lookup)')
            connection.commit()
            baseline = _indexed_lookup(connection, parameters)
            connection.execute('DROP INDEX idx_records_lookup')
            fault = _indexed_lookup(connection, parameters)
            connection.execute('CREATE INDEX idx_records_lookup ON records(lookup)')
            restored = _indexed_lookup(connection, parameters)
            trial = {'repeat': repeat, 'row_count_observed': connection.execute('SELECT COUNT(*) FROM records').fetchone()[0],
                'baseline': baseline, 'fault': fault, 'restored': restored}
            trials.append(trial)
            checks.extend([
                _check(f'repeat_{repeat}_indexed_baseline_returns', baseline['outcome'] == 'returned'
                    and baseline['returned_payloads'] == ['payload_3999']),
                _check(f'repeat_{repeat}_scan_interrupts_at_vm_budget', fault['outcome'] == 'failed'
                    and fault.get('error_number') == sqlite3.SQLITE_INTERRUPT
                    and fault['interrupt_reason'] == 'vm_instruction_budget'
                    and fault['observed_vm_progress_instructions'] == parameters['vm_instruction_budget']),
                _check(f'repeat_{repeat}_recreated_index_restores_query', restored['outcome'] == 'returned'
                    and restored['returned_payloads'] == baseline['returned_payloads']
                    and restored['observed_vm_progress_instructions'] < parameters['vm_instruction_budget']),
                _check(f'repeat_{repeat}_measured_plans_and_rows',
                    any('SEARCH' in plan and 'idx_records_lookup' in plan for plan in baseline['query_plan'])
                    and any('SCAN records' in plan for plan in fault['query_plan'])
                    and any('SEARCH' in plan and 'idx_records_lookup' in plan for plan in restored['query_plan'])
                    and trial['row_count_observed'] == parameters['row_count']),
            ])
    rows = [
        _row('row_01', 'configuration', {'statement': parameters['statement'],
            'target_lookup': parameters['target_lookup'], 'automatic_index': parameters['automatic_index'],
            'index_operations': ['CREATE INDEX idx_records_lookup ON records(lookup)',
                'DROP INDEX idx_records_lookup', 'CREATE INDEX idx_records_lookup ON records(lookup)'],
            'progress_interval_vm_instructions': parameters['progress_interval_vm_instructions'],
            'vm_instruction_budget': parameters['vm_instruction_budget']}),
        _row('row_02', 'resource', {'row_counts': [trial['row_count_observed'] for trial in trials],
            'failed_lookup_results': [{'repeat': trial['repeat'], **trial['fault']} for trial in trials]}),
        _row('row_03', 'resource', {'intervention': 'recreate idx_records_lookup; retry identical lookup and instruction budget',
            'phase_order': ['baseline', 'fault', 'restored'],
            'phase_results': [{'repeat': trial['repeat'],
                'outcomes': [trial[phase]['outcome'] for phase in ('baseline', 'fault', 'restored')],
                'query_plans': [trial[phase]['query_plan'] for phase in ('baseline', 'fault', 'restored')],
                'vm_progress': [trial[phase]['observed_vm_progress_instructions'] for phase in ('baseline', 'fault', 'restored')]}
                for trial in trials]}),
    ]
    return _finish(CASE_IDS[1], 'sqlite_missing_index_vm_budget', parameters, trials, checks, rows, {
        'causal_condition': 'The lookup index is removed while the lookup, rows and 1000-instruction progress-handler budget remain fixed.',
        'mechanism': 'An unindexed table scan exhausts the deterministic VM progress budget and is interrupted; recreating the index permits the same lookup.',
        'reference_explanation': 'The measured query plan changes from indexed SEARCH to SCAN during failure and back to SEARCH after index creation; only the SCAN phase reaches the instruction cutoff.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'resource'],
        'acceptable_aliases': ['索引缺失导致全表扫描耗尽VM指令预算', '查询索引被删除触发扫描预算中断', 'missing index causes SQLite instruction-budget interruption'],
        'forbidden_assertions': ['数据库死锁', '网络超时', '磁盘损坏', '真实SQL耗时超时', 'production query latency reproduced'],
        'limitations': ['The cutoff is deterministic VM progress, not a measured production response-time deadline.',
            'The in-memory SQLite table and query are synthetic; no network or production database is queried.'],
    })


def _hmac_verify(payload, signature, signing_key_id, verifier_key_id, keys):
    expected = hmac.new(keys[verifier_key_id], payload, hashlib.sha256).digest()
    verified = hmac.compare_digest(expected, signature)
    return {'signing_key_id': signing_key_id, 'verifier_key_id': verifier_key_id,
        'payload_sha256': _sha(payload), 'signature_sha256': _sha(signature),
        'verified': verified, 'outcome': 'accepted' if verified else 'rejected',
        'error_code': None if verified else 'HMAC_SIGNATURE_MISMATCH'}


def reproduce_hmac_key_rotation(repeats=3):
    """Actual HMAC digests reject an unsynchronized verifier key generation."""
    _validate_repeats(repeats)
    parameters = {'algorithm': 'HMAC-SHA256',
        'signing_key_ids_by_phase': ['generation_1', 'generation_2', 'generation_2'],
        'verifier_key_ids_by_phase': ['generation_1', 'generation_1', 'generation_2'],
        'key_material_scope': 'nonsecret local fixture; not production credentials'}
    keys = {'generation_1': b'local-rca-fixture-key-generation-one',
        'generation_2': b'local-rca-fixture-key-generation-two'}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        payload = json.dumps({'request': repeat, 'amount': 17}, sort_keys=True).encode('utf-8')
        first_signature = hmac.new(keys['generation_1'], payload, hashlib.sha256).digest()
        rotated_signature = hmac.new(keys['generation_2'], payload, hashlib.sha256).digest()
        baseline = _hmac_verify(payload, first_signature, 'generation_1', 'generation_1', keys)
        fault = _hmac_verify(payload, rotated_signature, 'generation_2', 'generation_1', keys)
        restored = _hmac_verify(payload, rotated_signature, 'generation_2', 'generation_2', keys)
        trial = {'repeat': repeat, 'baseline': baseline, 'fault': fault, 'restored': restored,
            'payload_identical_across_phases': len({item['payload_sha256'] for item in (baseline, fault, restored)}) == 1,
            'fault_and_restored_signature_identical': fault['signature_sha256'] == restored['signature_sha256']}
        trials.append(trial)
        checks.extend([
            _check(f'repeat_{repeat}_matched_baseline_verifies', baseline['verified']),
            _check(f'repeat_{repeat}_mismatched_rotation_rejected', not fault['verified']
                and fault['error_code'] == 'HMAC_SIGNATURE_MISMATCH'),
            _check(f'repeat_{repeat}_synchronized_rotation_restores', restored['verified']
                and trial['fault_and_restored_signature_identical']),
            _check(f'repeat_{repeat}_payload_unchanged', trial['payload_identical_across_phases']),
        ])
    rows = [
        _row('row_01', 'configuration', {'algorithm': parameters['algorithm'],
            'phase_key_ids': [{'phase': phase, 'signing_key_id': parameters['signing_key_ids_by_phase'][index],
                'verifier_key_id': parameters['verifier_key_ids_by_phase'][index]}
                for index, phase in enumerate(('baseline', 'fault', 'restored'))]}),
        _row('row_02', 'dependency', {'verification_results': [{'repeat': trial['repeat'], **trial['fault']} for trial in trials]}),
        _row('row_03', 'dependency', {'intervention': 'load generation_2 at verifier; verify identical payload and signature',
            'phase_order': ['baseline', 'fault', 'restored'],
            'phase_results': [{'repeat': trial['repeat'],
                'verified': [trial[phase]['verified'] for phase in ('baseline', 'fault', 'restored')],
                'payload_identical_across_phases': trial['payload_identical_across_phases'],
                'fault_and_restored_signature_identical': trial['fault_and_restored_signature_identical']}
                for trial in trials]}),
        _row('row_04', 'dependency', {'payload_identical_across_phases': [trial['payload_identical_across_phases'] for trial in trials],
            'fault_and_restored_signature_identical': [trial['fault_and_restored_signature_identical'] for trial in trials]}),
    ]
    return _finish(CASE_IDS[2], 'hmac_key_rotation_desynchronization', parameters, trials, checks, rows, {
        'causal_condition': 'The signer has rotated to generation_2 while the verifier still uses generation_1.',
        'mechanism': 'HMAC verification recomputes a different digest with the stale key and rejects it; loading generation_2 verifies the identical signature.',
        'reference_explanation': 'Payload bytes remain unchanged and the fault signature is reused after recovery. The verifier key generation is the changed input that restores verification.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'dependency'],
        'acceptable_aliases': ['HMAC验签端密钥轮换未同步', '签名与验签密钥版本不一致', 'stale HMAC verifier key after rotation'],
        'forbidden_assertions': ['签名算法被破解', 'payload篡改已证实', '网络超时', '真实密钥泄露', 'production credential incident'],
        'limitations': ['Keys are nonsecret synthetic fixture bytes; no authentication server or production credential is used.',
            'The experiment identifies an unsynchronized key generation, not an organizational cause of a real deployment failure.'],
    })


def _decode_response(body, content_encoding):
    result = {'content_encoding': content_encoding, 'body_sha256': _sha(body),
        'body_prefix_hex': body[:4].hex(), 'encoded_bytes': len(body)}
    try:
        if content_encoding == 'gzip':
            decoded = gzip.decompress(body)
        elif content_encoding == 'deflate':
            decoded = zlib.decompress(body)
        else:
            raise ValueError('Unsupported content encoding in experiment')
        document = json.loads(decoded.decode('utf-8'))
        result.update({'outcome': 'decoded', 'decoded_document': document,
            'decoded_sha256': _sha(decoded), 'error_code': None})
    except (OSError, EOFError, zlib.error, ValueError) as exc:
        result.update({'outcome': 'failed', 'error_code': 'DECOMPRESSION_FAILED',
            'exception_type': type(exc).__name__, 'error_message': str(exc)})
    return result


def reproduce_gzip_encoding_mismatch(repeats=3):
    """Real gzip bytes fail when the response header selects a deflate decoder."""
    _validate_repeats(repeats)
    parameters = {'body_format': 'gzip', 'gzip_mtime': 0,
        'content_encoding_by_phase': ['gzip', 'deflate', 'gzip'],
        'consumer_decoder_selection': 'Content-Encoding header'}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        document = {'request': repeat, 'value': 'local-observation'}
        plain = json.dumps(document, sort_keys=True).encode('utf-8')
        body = gzip.compress(plain, mtime=parameters['gzip_mtime'])
        baseline, fault, restored = [_decode_response(body, encoding)
            for encoding in parameters['content_encoding_by_phase']]
        trial = {'repeat': repeat, 'baseline': baseline, 'fault': fault, 'restored': restored,
            'body_identical_across_phases': len({item['body_sha256'] for item in (baseline, fault, restored)}) == 1,
            'gzip_roundtrip_matches_original': gzip.decompress(body) == plain}
        trials.append(trial)
        checks.extend([
            _check(f'repeat_{repeat}_actual_gzip_baseline_decodes',
                baseline['outcome'] == 'decoded' and baseline['decoded_document'] == document
                and baseline['body_prefix_hex'].startswith('1f8b')),
            _check(f'repeat_{repeat}_mismatched_header_decoder_fails',
                fault['outcome'] == 'failed' and fault['exception_type'] == 'error'
                and fault['content_encoding'] == 'deflate'),
            _check(f'repeat_{repeat}_corrected_header_restores',
                restored['outcome'] == 'decoded' and restored['decoded_document'] == document
                and baseline['decoded_sha256'] == restored['decoded_sha256']),
            _check(f'repeat_{repeat}_wire_bytes_unchanged',
                trial['body_identical_across_phases'] and trial['gzip_roundtrip_matches_original']),
        ])
    rows = [
        _row('row_01', 'configuration', {'content_encoding_by_phase': parameters['content_encoding_by_phase'],
            'consumer_decoder_selection': parameters['consumer_decoder_selection'],
            'observed_body_prefix_hex': [trial['fault']['body_prefix_hex'] for trial in trials]}),
        _row('row_02', 'dependency', {'decode_failures': [{'repeat': trial['repeat'], **trial['fault']} for trial in trials]}),
        _row('row_03', 'dependency', {'intervention': 'set Content-Encoding from deflate to gzip; decode identical response bytes',
            'phase_order': ['baseline', 'fault', 'restored'],
            'phase_results': [{'repeat': trial['repeat'],
                'outcomes': [trial[phase]['outcome'] for phase in ('baseline', 'fault', 'restored')],
                'content_encodings': [trial[phase]['content_encoding'] for phase in ('baseline', 'fault', 'restored')],
                'body_identical_across_phases': trial['body_identical_across_phases']}
                for trial in trials]}),
        _row('row_04', 'dependency', {'body_identical_across_phases': [trial['body_identical_across_phases'] for trial in trials],
            'gzip_roundtrip_matches_original': [trial['gzip_roundtrip_matches_original'] for trial in trials]}),
    ]
    return _finish(CASE_IDS[3], 'gzip_content_encoding_mismatch', parameters, trials, checks, rows, {
        'causal_condition': 'The Content-Encoding header changes to deflate although the response bytes remain a gzip stream.',
        'mechanism': 'The consumer follows the header and passes gzip bytes to the zlib deflate decoder, which fails; restoring gzip selects the matching decoder.',
        'reference_explanation': 'The encoded body SHA256 is identical in all three phases; changing only the declared encoding causes failure and changing it back restores the same document.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'dependency'],
        'acceptable_aliases': ['Content-Encoding与gzip正文不一致', 'gzip数据被错误声明为deflate', 'wrong content-encoding selects incompatible decoder'],
        'forbidden_assertions': ['正文损坏已证实', '网络截断已证实', 'TLS故障', '数据库崩溃', 'production proxy incident'],
        'limitations': ['This uses local byte buffers and standard-library decoders; no HTTP server, proxy or network transfer is exercised.',
            'It identifies a declared-encoding mismatch, not why a real deployment generated such a header.'],
    })


def run_experiments(repeats=3):
    """Return four measured cases; caller owns source snapshots and persistence."""
    _validate_repeats(repeats)
    return [reproduce_sqlite_transaction_lock(repeats), reproduce_sqlite_missing_index(repeats),
        reproduce_hmac_key_rotation(repeats), reproduce_gzip_encoding_mismatch(repeats)]
