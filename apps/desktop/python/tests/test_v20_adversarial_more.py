from copy import deepcopy
import json
import pytest
from test_investigation_part_selection_v20 import setup,selection,v8,wire

@pytest.mark.parametrize('bad',[None,True,1,{},'x'])
def test_nonarray_inventory_fails(bad):
    *_,prepared,_=setup()
    with pytest.raises(ValueError):v8.decode_selection(json.dumps({'inventory':bad}),prepared)

@pytest.mark.parametrize('change',['raw','input','source','version','inventory'])
def test_first_and_prepared_tamper_fails(change):
    *_,prepared,first=setup();prepared=deepcopy(prepared);first=deepcopy(first)
    if change=='raw':first['raw']='{}'
    elif change=='input':first['input_sha256']='0'*64
    elif change=='source':prepared['claims'][0]['text']+=' altered'
    elif change=='version':prepared['version']='fixed-part-selection-v18'
    else:first['inventory']['C1:rule']=[]
    with pytest.raises(ValueError):v8.review_messages(prepared,first)


def test_duplicate_keys_extra_fields_and_raw_limit_reject():
    *_,prepared,first=setup()
    with pytest.raises(ValueError):v8.decode_selection('{"inventory":[],"inventory":[]}',prepared)
    with pytest.raises(ValueError):v8.decode_review(' '*60001,prepared,first)
    candidate=json.loads(wire('unknown',['C1:rule']));candidate['parts']['P1']['extra']=True
    with pytest.raises(ValueError):v8.decode_review(json.dumps(candidate),prepared,first)


def test_polar_unknown_and_conflict_remain_scoped_and_check_never_visible():
    *_,prepared,first=setup()
    u=v8.decode_review(wire('unknown',['C1:rule']),prepared,first)
    c=v8.decode_review(wire('conflicting',['C1:rule']),prepared,first)
    assert 'Not established by the selected reviewed statements' in u['text']
    assert 'Selected reviewed statements' in c['text'] and 'no authoritative winner' in c['text']
    assert 'Source states rule.' not in u['text']+c['text']
