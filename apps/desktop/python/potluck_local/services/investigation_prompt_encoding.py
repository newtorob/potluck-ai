"""Compact calculation metadata without dropping evidence or extraction warnings."""
from copy import deepcopy
import hashlib
import json

COMPACT = 'compact-calculation-v1'


def encode(data: dict) -> tuple[str, dict]:
    original = json.dumps(data, ensure_ascii=False)
    packed = deepcopy(data)
    notices = packed.get('extraction_notices', [])
    reasons = []
    if notices:
        for notice in notices:
            reason = notice.pop('reason')
            if reason not in reasons:
                reasons.append(reason)
            notice['reason_index'] = reasons.index(reason)
            # The snippet ID already binds the warning to its stored citation.
            notice.pop('citation_id')
        packed['extraction_notice_reasons'] = reasons
        encoded = json.dumps(packed, ensure_ascii=False, separators=(',', ':'))
    else:
        # Factual-only requests keep the qualified prompt bytes unchanged.
        encoded = original
    return encoded, {
        'version': COMPACT,
        'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
        'encoded_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
        'original_characters': len(original),
        'encoded_characters': len(encoded),
        'notice_count': len(notices),
        'reason_count': len(reasons),
    }
