"""Read-only bridge from retained-unit packets to the existing coverage selector."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import sys

from potluck_local.services import investigation_evidence_coverage_v3 as coverage_v3


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('invalid packet fields')
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ValueError('invalid packet text')
    return value


def _ids(value):
    if not isinstance(value, list):
        raise ValueError('invalid IDs')
    ids = [_text(x) for x in value]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate ID')
    return ids


def prepare(envelope, packet_jsonl):
    original = deepcopy(envelope)
    if not isinstance(original, dict) or not isinstance(original.get('group'), dict):
        raise ValueError('invalid envelope')
    original['group'].pop('draft_sentences', None)
    prepared = coverage_v3.prepare(original)
    if not isinstance(packet_jsonl, str) or len(packet_jsonl.encode("utf-8")) > 65536:
        raise ValueError('invalid packet')
    lines = packet_jsonl.splitlines()
    if len(lines) < 3 or any(not line.strip() for line in lines):
        raise ValueError('invalid packet lines')
    try:
        rows = [json.loads(line, object_pairs_hook=_unique) for line in lines]
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid packet JSON') from exc
    header = _fields(rows[0], ['record', 'question', 'verdict', 'selected_ids', 'rule', 'covered_by_exact_binding_ids'])
    if header['record'] != 'answer' or header['verdict'] != 'source_rule' or _text(header['question']) != prepared['selection_scope']['focus']:
        raise ValueError('packet question or verdict mismatch')
    _text(header['rule'])
    selected, retained = _ids(header['selected_ids']), _ids(header['covered_by_exact_binding_ids'])
    if set(selected) & set(retained):
        raise ValueError('conflicting states')
    if len(prepared['units']) > 16 or any(len(u['quote'].encode('utf-8')) > 1024 for u in prepared['units']):
        raise ValueError('unit limit')
    pos = 1
    seen = []
    for index, context in enumerate(prepared['contexts'], 1):
        if pos >= len(rows):
            raise ValueError('missing source')
        source = _fields(rows[pos], ['record', 'id', 'headings', 'retained_bindings']); pos += 1
        headings = list(dict.fromkeys(c['heading'] for c in context['source_contexts']))
        if source['record'] != 'source' or source['id'] != f'C{index}' or source['headings'] != headings or not isinstance(source['retained_bindings'], list):
            raise ValueError('source grouping mismatch')
        bindings = []
        for item in source['retained_bindings']:
            item = _fields(item, ['start', 'end', 'quote'])
            start, end, quote = item['start'], item['end'], _text(item['quote'])
            if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(context['text']) or context['text'][start:end] != quote:
                raise ValueError('invalid source binding')
            if item in bindings:
                raise ValueError('duplicate binding')
            bindings.append(item)
        context_units = [u for u in prepared['units'] if u['source_ids'] == context['source_ids']]
        expected = []
        for unit in context_units:
            if unit['id'] not in selected:
                continue
            for source_context in unit['source_contexts']:
                intro = source_context['list_introduction']
                if intro is None:
                    continue
                a, b = intro.get('start'), intro.get('end')
                if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(context['text']) or context['text'][a:b] != intro.get('quote'):
                    raise ValueError('invalid original binding')
                binding = {'start': a, 'end': b, 'quote': context['text'][a:b]}
                if binding not in expected:
                    expected.append(binding)
        if bindings != expected:
            raise ValueError('unjustified retained bindings')
        for unit in context_units:
            if pos >= len(rows):
                raise ValueError('missing unit')
            record = _fields(rows[pos], ['id', 'state', 'text']); pos += 1
            uid = unit['id']; state = record['state']
            if record['id'] != uid or record['text'] != unit['quote'] or not _text(record['text']):
                raise ValueError('unit mismatch')
            if uid in seen or state not in ('selected', 'retained_binding', 'supplied_only'):
                raise ValueError('invalid unit state')
            if (state == 'selected') != (uid in selected) or (state == 'retained_binding') != (uid in retained):
                raise ValueError('state mismatch')
            left = unit['start'] + len(unit['quote']) - len(unit['quote'].lstrip())
            right = unit['end'] - len(unit['quote']) + len(unit['quote'].rstrip())
            bound = left < right and any(b['start'] <= left < right <= b['end'] for b in bindings)
            expected_state = 'selected' if uid in selected else 'retained_binding' if bound else 'supplied_only'
            if state != expected_state:
                raise ValueError('unit binding state mismatch')
            seen.append(uid)
    if pos != len(rows) or set(selected + retained) - set(seen):
        raise ValueError('extra packet records or unknown IDs')
    return {'version': 'coverage-bridge-v1', 'packet_jsonl': packet_jsonl, 'prepared': prepared, 'selected_ids': selected, 'retained_ids': retained, 'max_findings': 3, 'max_words': 140}


def _check_binding(binding):
    try:
        canonical = prepare(binding['prepared']['original'], binding['packet_jsonl'])
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('invalid bridge binding') from exc
    if canonical != binding:
        raise ValueError('invalid bridge binding')


def request(binding):
    _check_binding(binding)
    p = binding['prepared']
    return {'messages': coverage_v3.messages(p), 'response_format': {'type': 'json_object', 'schema': coverage_v3.response_schema(p)}}


def _words(value):
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, list):
        return sum(_words(v) for v in value)
    if isinstance(value, dict):
        return sum(_words(k) + _words(v) for k, v in value.items())
    return 0


def decode(raw, binding):
    _check_binding(binding)
    result = coverage_v3.decode(raw, binding['prepared'])
    parsed = json.loads(raw, object_pairs_hook=_unique)
    words = _words(parsed)
    if words > binding['max_words'] or result['verdict'] != 'source_rule' or result['uncertain']:
        raise ValueError('coverage incomplete or exceeds budget')
    covered = set(binding['selected_ids'] + binding['retained_ids'])
    findings = [{'unit_id': unit['id'], 'quote': unit['quote']} for unit in result['selected'] if unit['id'] not in covered]
    if len(findings) > binding['max_findings']:
        raise ValueError('finding limit')
    return {'status': 'needs_review', 'findings': findings, 'assessment': result, 'generated_words': words}


def identity():
    """Hash loaded package implementation files under stable package-relative names."""
    import potluck_local
    root = Path(potluck_local.__file__).resolve().parent
    files = {Path(__file__).resolve()}
    for name, module in tuple(sys.modules.items()):
        if name == 'potluck_local' or name.startswith('potluck_local.'):
            path = getattr(module, '__file__', None)
            if path and path.endswith('.py'):
                path = Path(path).resolve()
                if not path.is_relative_to(root):
                    raise ValueError('invalid module path')
                files.add(path)
    entries = [(p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
    return hashlib.sha256(json.dumps(sorted(entries), separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def main():
    try:
        initial_identity = identity()
        raw = sys.stdin.buffer.read(262145)
        if len(raw) > 262144:
            raise ValueError('input limit')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise ValueError('input object')
        operation = value.get('operation')
        if operation == 'prepare':
            _fields(value, ['operation', 'envelope_json', 'packet'])
            if not isinstance(value['envelope_json'], str):
                raise ValueError('invalid envelope JSON')
            envelope = json.loads(value['envelope_json'], object_pairs_hook=_unique)
            binding = prepare(envelope, value['packet'])
            result = {'binding': binding, 'request': request(binding)}
        elif operation == 'decode':
            _fields(value, ['operation', 'binding', 'raw'])
            result = {'result': decode(value['raw'], value['binding'])}
        else:
            raise ValueError('unsupported operation')
        result['identity'] = identity()
        if result['identity'] != initial_identity:
            raise RuntimeError('bridge implementation changed')
        encoded = (json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(encoded) > 262144:
            raise ValueError('output limit')
        sys.stdout.buffer.write(encoded)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        sys.stderr.write('Invalid bridge request.\n')
        return 2
    except Exception:
        sys.stderr.write('Bridge unavailable.\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
