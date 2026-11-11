"""Conservative structural grouping over unchanged paragraph-v3 excerpts."""
from __future__ import annotations
import re


async def annotate_boundaries(db, evidence):
    """Record section boundaries from the same scanned revision, never a guess
    based on a chunk merely ending. No neighboring source text is disclosed.
    """
    for record in evidence:
        for child in record['sources'] if record['kind'] == 'reviewed_claim' else [record]:
            cur = await db.execute(
                'SELECT c.content, c.heading, n.heading FROM source_chunks c '
                'JOIN source_documents d ON d.id=c.document_id '
                'LEFT JOIN source_chunks n ON n.document_id=c.document_id AND n.ordinal=c.ordinal+1 '
                'WHERE c.id=? AND c.document_id=? AND d.revision_id=?',
                (child['chunk_id'], child['document_id'], child['revision_id']))
            row = await cur.fetchone()
            if row is None or row[0] != child['content']:
                raise ValueError('Evidence changed during boundary inspection')
            child['structural_boundary'] = {'version': 'section-end-v1',
                'section_end': row[2] is None or row[2] != row[1]}


def catalog(evidence, snippets, *, version='ordered-list-v1'):
    if version not in ('ordered-list-v1', 'ordered-list-v2'):
        raise ValueError('Invalid structural context version')
    sources = {}
    for record in evidence:
        for child in record['sources'] if record['kind'] == 'reviewed_claim' else [record]:
            cid = child['citation_id']; text = child.get('presented_content', '')
            if len(text) > len(sources.get(cid, {}).get('text', '')):
                sources[cid] = {'text': text, 'cut': len(text) < len(child.get('content', text)),
                    'section_end': child.get('structural_boundary', {}).get('section_end') is True}
    offsets = {}; cursors = {}
    for snippet in snippets:
        cid=snippet['citation_id']; text=sources[cid]['text']; start=cursors.get(cid, 0)
        if text[start:start+len(snippet['quote'])] != snippet['quote']:
            raise ValueError('Invalid source snippet offsets')
        end=start+len(snippet['quote']); offsets[snippet['snippet_id']] = {'citation_id':cid, 'start':start, 'end':end}
        cursors[cid]=end
    blocks=[]
    for cid, source in sources.items():
        text=source['text']; lines=list(re.finditer(r'(?m)^.*(?:\n|$)',text)); fence=None; current=None
        def finish(end, uncertain=False):
            nonlocal current
            if current:
                end = current['last_end']
                current['end']=end; current['uncertain'] |= uncertain
                if len(current['numbers']) >= 2 or current['uncertain']:
                    members=[sid for sid, pos in offsets.items() if pos['citation_id']==cid and pos['start'] < end and pos['end'] > current['start']]
                    covered=bool(members) and offsets[members[0]]['start'] <= current['start'] and offsets[members[-1]]['end'] >= end
                    blocks.append({'citation_id':cid,'start':current['start'],'end':end,'snippet_ids':members,
                                   'complete': covered and not current['uncertain']})
                current=None
        for match in lines:
            line=match.group()
            if not line: continue
            stripped=line.lstrip(); marker=re.match(r'(`{3,}|~{3,})',stripped)
            if marker:
                if fence is None:
                    finish(match.start(), True); fence=marker.group()
                elif marker.group()[0]==fence[0] and len(marker.group())>=len(fence) and not stripped[marker.end():].strip():
                    fence=None
                continue
            if fence is not None: continue
            ordered=re.match(r'^(\d+)[.)]\s+', line)
            if ordered:
                number=int(ordered.group(1))
                nonconsecutive = bool(current and number != current['numbers'][-1]+1)
                adjacent_ambiguous = nonconsecutive
                if nonconsecutive:
                    finish(match.start(), adjacent_ambiguous)
                if current is None:
                    current={'start':match.start(),'numbers':[],'uncertain':number != 1 or adjacent_ambiguous,'last_end':match.end()}
                current['numbers'].append(number); current['last_end']=match.end()
            elif current and line.strip():
                if line[0].isspace():
                    current['last_end']=match.end()
                    if re.match(r'^\s+(?:\d+[.)]|[-*+])\s+', line): current['uncertain']=True
                else:
                    finish(match.start())
        finish(len(text), version == 'ordered-list-v1' or source['cut'] or not source['section_end'])
    return {'version':version,'offsets':offsets,'blocks':blocks}
