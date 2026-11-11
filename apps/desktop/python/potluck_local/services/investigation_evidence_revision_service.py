"""Bound experimental source-unit decisions without changing saved investigations."""
import asyncio
from copy import deepcopy
import time

from . import investigation_draft_verification as common
from . import investigation_evidence_revision as contract
from . import investigation_revision as original_revision

PROFILE = original_revision.PROFILE
OUTPUT_TOKENS = 700
TIMEOUT_SECONDS = 180


async def generate(prepared, runtime, *, model, model_sha, seed=7):
    """Make one measured local call; failed or partial output remains a failure."""
    from . import prompt_cache

    if type(seed) is not int or not 0 <= seed <= 4294967295:
        raise ValueError('Invalid revision seed')
    if prepared.get('version') != contract.VERSION:
        raise ValueError('Invalid evidence revision version')
    if runtime.current_model_id() != model or runtime.current_model_sha256() != model_sha:
        raise ValueError('Requested model is not loaded locally')
    schema = contract.response_schema(prepared)
    messages = contract.messages(prepared)
    attempt = {
        'version': contract.VERSION, 'input': deepcopy(prepared),
        'input_sha256': common.digest(prepared),
        'settings': {'model': model, 'model_sha256': model_sha, 'profile': PROFILE,
                     'seed': seed, 'output_tokens': OUTPUT_TOKENS,
                     'timeout_seconds': TIMEOUT_SECONDS},
        'generation_calls': 0, 'input_budget': None,
        'status': 'not_generated', 'reason': 'inference_failed',
    }
    started = time.monotonic()
    token = prompt_cache.request_is_local.set(False)
    stage = 'inference'
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            await runtime.validate_investigation_profile(
                PROFILE, expected_model=model, expected_sha256=model_sha)
            measurement = await runtime.measure_investigation_prompt(
                messages, response_format={'type': 'json_object', 'schema': schema},
                expected_model=model, expected_sha256=model_sha, generation_profile=PROFILE)
            count, capacity = measurement['input_tokens'], measurement['context_tokens']
            if type(count) is not int or count <= 0 or type(capacity) is not int or capacity <= 0:
                raise ValueError('Invalid input measurement')
            attempt['input_budget'] = {
                'input_tokens': count, 'context_tokens': capacity,
                'messages_sha256': common.digest(messages), 'schema_sha256': common.digest(schema),
            }
            if count + OUTPUT_TOKENS > capacity:
                attempt['reason'] = 'capacity'
            else:
                attempt['generation_calls'] = 1
                reply = await runtime.chat(
                    messages, temperature=0, max_tokens=OUTPUT_TOKENS,
                    expected_model=model, expected_sha256=model_sha, bounded_generation=True,
                    expected_context_tokens=capacity,
                    response_format={'type': 'json_object', 'schema': schema},
                    generation_profile=PROFILE, seed=seed)
                stage = 'decode'
                if runtime.current_model_id() != model or runtime.current_model_sha256() != model_sha:
                    attempt['reason'] = 'model_changed'
                else:
                    choice = reply['choices'][0]
                    if choice.get('finish_reason') != 'stop':
                        raise ValueError('Incomplete revision output')
                    attempt['result'] = contract.decode(choice['message']['content'], prepared)
                    attempt.update(status='proposal', reason=None)
    except asyncio.TimeoutError:
        attempt['reason'] = 'timeout'
    except (ValueError, KeyError, TypeError, IndexError):
        attempt['reason'] = 'invalid_output' if stage == 'decode' else 'inference_failed'
    except Exception:
        attempt['reason'] = 'inference_failed'
    finally:
        prompt_cache.request_is_local.reset(token)
    attempt['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return attempt


async def propose(scope, iid, group_id, *, seed=7):
    """Keep the experimental result detached and recheck the original's scoped state."""
    from . import investigations as inv

    item = await inv.get_investigation(scope, iid)
    if item['status'] != 'draft':
        raise ValueError('Only unreviewed drafts can receive proposals')
    envelopes = original_revision.prepare_original(item)
    envelope = next((e for e in envelopes if e['group']['id'] == group_id), None)
    if envelope is None:
        raise ValueError('Unknown or inapplicable revision group')
    attempt = await generate(contract.prepare(envelope), inv.get_runtime(),
                             model=item['model_slug'], model_sha=item['model_sha'], seed=seed)
    current = await inv.get_investigation(scope, iid)
    fields = ('status', 'brief', 'evidence', 'provenance', 'review', 'model_slug', 'model_sha')
    if any(current.get(key) != item.get(key) for key in fields):
        raise ValueError('Investigation changed during revision')
    return {'investigation_id': iid, 'original_sha256': common.digest(item), 'attempt': attempt}
