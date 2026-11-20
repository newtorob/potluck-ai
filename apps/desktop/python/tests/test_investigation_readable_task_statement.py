from copy import deepcopy

from potluck_local.services.investigation_readable_task_statement import project_task_statement


def fixture():
    common = {'kind': 'source_chunk', 'collection_id': 'c', 'document_id': 'd', 'revision_id': 'r', 'relative_path': 'Tasks/T.md'}
    metadata = '---\nid: TASK-12\ntitle: Prepare kit\ntype: task\nstatus: open\n---'
    first = 'Before use, obtain approval.\n\n**Fix.** Prepare kit carefully.'
    second = 'Exception: wait when approval is unavailable.'
    sources = [
        {**common, 'chunk_id': 'a', 'citation_id': 'ca', 'heading': '', 'anchor': 'intro', 'content': metadata},
        {**common, 'chunk_id': 'b', 'citation_id': 'cb', 'heading': 'TASK-12 Prepare kit', 'content': first},
        {**common, 'chunk_id': 'c', 'citation_id': 'cc', 'heading': 'TASK-12 Prepare kit', 'content': second},
    ]
    text = 'Document: Collection / Tasks/T.md\n\n' + '\n\n'.join(
        (s['heading'] + '\n\n' if s['heading'] else '') + s['content'] for s in sources)
    return text, sources


def test_shuffled_sources_preserve_body_and_exact_reconstruction():
    text, sources = fixture()
    result = project_task_statement(text, list(reversed(sources)))
    assert result is not None
    assert [x['citation_id'] for x in result['body_chunks']] == ['cb', 'cc']
    assert 'Before use' in result['body_chunks'][0]['text']
    assert 'Prepare kit carefully' in result['body_chunks'][0]['text']
    assert 'Exception' in result['body_chunks'][1]['text']
    assert result['status_line'] == 'status: open'
    assert ''.join(x['text'] for x in result['segments']) == text
    assert all(text[x['start']:x['end']] == x['text'] for x in result['segments'])


def test_rejects_unrecognized_or_conditional_metadata():
    text, sources = fixture()
    for old, new in [('status: open', 'status: open if approved'),
                     ('type: task', 'type: task\nnotes: optional'),
                     ('status: open', 'status: open\nstatus: closed')]:
        changed = deepcopy(sources)
        changed[0]['content'] = changed[0]['content'].replace(old, new)
        assert project_task_statement(text.replace(old, new), changed) is None


def test_rejects_ambiguous_identity_and_content():
    text, sources = fixture()
    for key, value in [('revision_id', 'other'), ('citation_id', 'cb'), ('chunk_id', 'b')]:
        changed = deepcopy(sources)
        changed[2][key] = value
        assert project_task_statement(text, changed) is None
    changed = deepcopy(sources)
    changed[2]['content'] = changed[1]['content']
    assert project_task_statement(text, changed) is None
    assert project_task_statement(text + '\n\n' + sources[1]['content'], sources) is None


def test_conditional_title_remains_in_proven_visible_heading():
    text, sources = fixture()
    title = 'Prepare kit after approval unless emergency work is authorized'
    changed = deepcopy(sources)
    changed[0]['content'] = changed[0]['content'].replace('title: Prepare kit', 'title: ' + title)
    for source in changed[1:]:
        source['heading'] = 'TASK-12 ' + title
    complete = 'Document: Collection / Tasks/T.md\n\n' + '\n\n'.join(
        (s['heading'] + '\n\n' if s['heading'] else '') + s['content'] for s in changed)
    result = project_task_statement(complete, changed)
    assert result is not None and result['heading'] == 'TASK-12 ' + title
    assert ''.join(x['text'] for x in result['segments']) == complete
    changed[1]['heading'] = 'TASK-12 Prepare kit'
    assert project_task_statement(complete, changed) is None
