"""Capture available user-facing health conversation messages without clinical inference."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from common import ROOT, canonical, current_account, digest, local_profile


def _immutable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError('Conflicting conversation source requires review: '+str(path))
        return False
    with path.open('xb') as stream:
        stream.write(content)
    return True


def _write(path, content):
    if path.exists() and path.read_text() == content:
        return
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(content)
    os.replace(temp,path)


CLARIFICATION_TOOLS=('request_user_input_async','request_user_input')


def _session_workspaces():
    """Folders whose local sessions belong to this archive: this checkout, plus any the profile lists."""
    allowed={ROOT.resolve()}
    try:
        allowed|={Path(p).expanduser().resolve() for p in local_profile().get('session_workspaces',[])}
    except RuntimeError:
        pass
    return allowed


def _codex_items(event):
    if event.get('type')!='response_item':
        return []
    payload=event['payload'];role=payload.get('role')
    if payload.get('type')=='message' and role in ('user','assistant'):
        if payload.get('channel') in ('analysis','summary'):
            return []
        blocks=[v for v in payload.get('content',[]) if v.get('type') in ('input_text','output_text','text')]
        text='\n\n'.join(v.get('text','') for v in blocks)
        return [(role,'message',text,blocks,payload.get('id'))] if text else []
    if payload.get('type')=='function_call' and payload.get('name','').split('.')[-1] in CLARIFICATION_TOOLS:
        args=payload.get('arguments','{}')
        parsed=json.loads(args) if isinstance(args,str) else args
        return [('assistant','clarification_prompt',json.dumps(parsed,ensure_ascii=False,indent=2),
                 [{'type':'clarification_prompt','questions':parsed.get('questions',[])}],payload.get('id'))]
    return []


CONNECTOR_SESSIONS='conversations/connector-sessions'


def _connector_items(event):
    if event.get('type')!='connector_message' or not event.get('text','').strip():
        return []
    return [(event['role'],'message',event['text'],[{'type':'text','text':event['text']}],event.get('message_ref'))]


def _same_connector_message(stored, row):
    """Whether a stored connector message differs only by the absolute workspace path earlier captures recorded."""
    path=stored['provenance'].get('original_session_path','')
    if not path.endswith('/'+row['provenance']['original_session_path']):
        return False
    rest=lambda r:{**r,'content_sha256':None,'provenance':{**r['provenance'],'original_session_path':None}}
    return rest(stored)==rest(row)


def append_connector_messages(archive_root, session_id, client, messages, received_at):
    """Append messages a connected client submits to an append-only source session file.

    Connector clients such as claude.ai or ChatGPT expose no transcript file, so
    the submitted messages become the preserved source. A message already in the
    file, identified by its client reference or exact content, is not appended
    again. The OAuth-authenticated owner is the ownership basis.
    """
    if not session_id or any(c not in '0123456789abcdef-' for c in session_id.lower()) or len(session_id)>64:
        raise ValueError('Connector session identifiers use hexadecimal digits and hyphens')
    path=Path(archive_root)/CONNECTOR_SESSIONS/(session_id+'.jsonl')
    path.parent.mkdir(parents=True,exist_ok=True)
    existing=[json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    seen={(e.get('message_ref') or digest([e['role'],e['timestamp'],e['text']])) for e in existing}
    added=[]
    for m in messages:
        if m['role'] not in ('user','assistant') or not m.get('text','').strip():
            raise ValueError('Connector messages need a user or assistant role and text')
        timestamp=m.get('timestamp') or received_at
        datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        key=m.get('message_ref') or digest([m['role'],timestamp,m['text']])
        if key in seen:continue
        seen.add(key)
        added.append({'type':'connector_message','session_id':session_id,'client':client,'role':m['role'],'text':m['text'],
                      'timestamp':timestamp,'message_ref':m.get('message_ref'),'timestamp_basis':'client_supplied' if m.get('timestamp') else 'server_received'})
    with path.open('a') as stream:
        for event in added:stream.write(canonical(event)+'\n')
    return path,len(added)


def capture_pending(archive_root):
    """Capture connector session sources appended since their last checkpoint.

    A connector capture stores submitted messages as source first and defers
    projection, so every write sweeps those sources into archive messages."""
    archive_root=Path(archive_root);captured=[]
    for path in sorted((archive_root/CONNECTOR_SESSIONS).glob('*.jsonl')):
        checkpoint=archive_root/'conversations/sessions'/path.stem/'checkpoint.json'
        if checkpoint.exists() and json.loads(checkpoint.read_text())['source_complete_bytes']==path.stat().st_size:
            continue
        captured.append(capture(path,archive_root=archive_root))
    return captured


def _claude_code_items(event, prompts):
    """Map one Claude Code transcript event to user-facing message items.

    Thinking, tool calls and tool output stay outside the archive, except a
    structured clarification question and the user's recorded answer to it.
    """
    if event.get('type') not in ('user','assistant') or event.get('isSidechain') or event.get('isMeta'):
        return []
    message=event.get('message',{});role=message.get('role');content=message.get('content')
    if isinstance(content,str):
        content=[{'type':'text','text':content}]
    items=[]
    for block in content or []:
        kind=block.get('type')
        if kind=='text' and block.get('text','').strip() and not block['text'].lstrip().startswith('<system-reminder>'):
            items.append((role,'message',block['text'],[block],event.get('uuid')))
        elif role=='assistant' and kind=='tool_use' and block.get('name')=='AskUserQuestion':
            prompts.add(block.get('id'))
            questions=block.get('input',{}).get('questions',[])
            items.append(('assistant','clarification_prompt',json.dumps(block.get('input',{}),ensure_ascii=False,indent=2),
                          [{'type':'clarification_prompt','questions':questions}],event.get('uuid')))
        elif role=='user' and kind=='tool_result' and block.get('tool_use_id') in prompts:
            answer=event.get('toolUseResult') if isinstance(event.get('toolUseResult'),dict) else {}
            result=block.get('content')
            text=result if isinstance(result,str) else '\n\n'.join(v.get('text','') for v in result or [] if v.get('type')=='text')
            if text.strip():
                items.append(('user','clarification_answer',text,
                              [{'type':'clarification_answer','tool_use_id':block['tool_use_id'],'answers':answer.get('answers',{}),
                                'annotations':answer.get('annotations',{})}],event.get('uuid')))
    return items


def capture(session, excluded_ranges=(), archive_root=None):
    """Capture into archive_root, such as a database-materialized workspace. Ownership is checked against this workspace's checkout."""
    archive_root=Path(archive_root or ROOT)
    session=Path(session).resolve()
    raw=session.read_bytes()
    # An actively appended final line is not yet an event. Capture complete lines.
    complete=raw[:raw.rfind(b'\n')+1]
    events=[(n,line,json.loads(line)) for n,line in enumerate(complete.splitlines(keepends=True),1) if line.strip()]
    meta=next((e['payload'] for _,_,e in events if e.get('type')=='session_meta'),None)
    connector=meta is None and any(e.get('type')=='connector_message' for _,_,e in events)
    claude_code=meta is None and not connector
    if connector:
        # Archived connector sources are owned by the authenticated archive owner.
        if session.parent.resolve()!=(archive_root/CONNECTOR_SESSIONS).resolve():
            raise ValueError('Connector sessions are captured only from the archive connector session directory')
        first=next(e for _,_,e in events if e.get('type')=='connector_message')
        meta={'id':first['session_id'],'cwd':'connector:'+str(first.get('client') or 'unspecified'),'timestamp':first['timestamp']}
    elif claude_code:
        # Claude Code transcripts carry session identity and workspace on each message event.
        first=next((e for _,_,e in events if e.get('type') in ('user','assistant') and e.get('sessionId') and e.get('cwd')),None)
        meta={'id':first['sessionId'],'cwd':first['cwd'],'timestamp':first['timestamp']} if first else None
    if not meta or (not connector and Path(meta.get('cwd','')).resolve() not in _session_workspaces()):
        raise ValueError('Only a session explicitly owned by this health workspace can be captured')
    # Connector sources live in the archive, which is materialized in a fresh
    # directory for every write, so their path is recorded relative to it.
    session_path=str(session.relative_to(archive_root.resolve())) if connector else str(session)
    sid=meta['id']
    if not sid or any(c not in '0123456789abcdef-' for c in sid.lower()):
        raise ValueError('Invalid session identifier')
    scope=current_account().patient_scope
    archive=archive_root/'conversations';directory=archive/'sessions'/sid
    directory.mkdir(parents=True,exist_ok=True)
    metadata={'schema_version':'1.0','record_type':'conversation_session','session_id':sid,'patient_scope':scope,
              'source_created_at':meta.get('timestamp'),'source_workspace':meta['cwd'],
              'scope':'Available user and user-facing assistant messages and clarification prompts. Excludes private analysis, system/developer instructions and tool execution output.'}
    _immutable(directory/'session.json',(json.dumps(metadata,indent=2)+'\n').encode())
    existing_path=archive/'messages.jsonl'
    existing=[json.loads(line) for line in existing_path.read_text().splitlines()] if existing_path.exists() else []
    records={r['message_id']:r for r in existing};new=0;observed=[]
    # Persist user-requested privacy gaps so a later unfiltered capture cannot
    # silently backfill a discussion the user asked us not to retain.
    policy_path=directory/'capture-policy.json'
    policy=json.loads(policy_path.read_text()) if policy_path.exists() else {'excluded_ranges':[]}
    def moment(value):
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None:
            raise ValueError('Capture exclusion timestamps require a timezone')
        return result
    for start,end in excluded_ranges:
        if moment(start)>=moment(end):
            raise ValueError('Capture exclusion start must precede end')
        if any(r['session_id']==sid and moment(start)<=moment(r['source_timestamp'])<moment(end) for r in existing):
            raise ValueError('Exclusion overlaps already archived messages; source-preserving review required')
        item={'start':start,'end_exclusive':end,'basis':'User requested that this discussion not be recorded.'}
        if item not in policy['excluded_ranges']:
            policy['excluded_ranges'].append(item)
    windows=[(moment(r['start']),moment(r['end_exclusive'])) for r in policy['excluded_ranges']]
    if windows:
        _write(policy_path,json.dumps(policy,indent=2)+'\n')
    excluded=0;prompts=set()
    for line_no,line,event in events:
        items=_connector_items(event) if connector else _claude_code_items(event,prompts) if claude_code else _codex_items(event)
        if len(items)>1:
            raise ValueError('Multiple user-facing items in one transcript event require review: line '+str(line_no))
        if not items:
            continue
        role,mode,text,blocks,source_message_id=items[0]
        if any(start<=moment(event['timestamp'])<end for start,end in windows):
            excluded+=1
            continue
        ordinal=event.get('ordinal',line_no)
        native='chatmsg_'+digest([sid,ordinal,role])[:24]
        source_path=directory/'messages'/f'{ordinal}.json'
        text_path=directory/'messages'/f'{ordinal}.txt'
        _immutable(source_path,line)
        _immutable(text_path,text.encode())
        row={'schema_version':'1.0','record_type':'conversation_message','message_id':native,'session_id':sid,
             'patient_scope':scope,'role':role,'message_mode':mode,'source_ordinal':ordinal,
             'source_timestamp':event['timestamp'],'source_message_id':source_message_id,
             'message_text':text,'content_blocks':blocks,'clinical_assertion':'not_extracted',
             'provenance':{'source_event_path':str(source_path.relative_to(archive_root)),
                           'source_event_sha256':hashlib.sha256(line).hexdigest(),
                           'text_path':str(text_path.relative_to(archive_root)),
                           'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                           'original_session_path':session_path,'original_line_number':line_no}}
        row['content_sha256']=digest(row)
        if native in records and records[native]!=row and not _same_connector_message(records[native],row):
            raise ValueError('Conflicting conversation message requires review: '+native)
        if native not in records:
            records[native]=row;new+=1
        observed.append(native)
    rows=sorted(records.values(),key=lambda r:(r['source_timestamp'],r['session_id'],r['source_ordinal']))
    from jsonschema import Draft202012Validator, FormatChecker
    validator=Draft202012Validator(json.loads((archive/'message.schema.json').read_text()),format_checker=FormatChecker())
    for row in rows:
        validator.validate(row)
    _write(existing_path,''.join(canonical(r)+'\n' for r in rows))
    sessions=[]
    for path in sorted((archive/'sessions').glob('*/session.json')):
        sessions.append(json.loads(path.read_text()))
    _write(archive/'sessions.jsonl',''.join(canonical(r)+'\n' for r in sessions))
    previous=json.loads((directory/'checkpoint.json').read_text()) if (directory/'checkpoint.json').exists() else None
    checkpoint={'session_id':sid,'source_complete_bytes':len(complete),'source_prefix_sha256':hashlib.sha256(complete).hexdigest(),
                'captured_through_timestamp':max(e['timestamp'] for _,_,e in events if e.get('timestamp')),'available_message_count':len(observed),
                'archived_session_message_count':sum(r['session_id']==sid for r in rows),
                'capture_scope':'Complete available user-facing conversation through this source checkpoint. This is not a claim that future or unavailable messages were captured.'}
    if windows:
        checkpoint.update(excluded_ranges=policy['excluded_ranges'],excluded_available_message_count=excluded,
                          capture_scope='Available user-facing conversation outside the documented user-requested no-record intervals, through this checkpoint. Excluded, future and unavailable messages are not claimed as captured.')
    if previous and all(previous.get(k)==v for k,v in checkpoint.items()):
        checkpoint['captured_at']=previous['captured_at']
    else:
        checkpoint['captured_at']=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    _write(directory/'checkpoint.json',json.dumps(checkpoint,indent=2)+'\n')
    return {'session_id':sid,'new_messages':new,'observed_messages':len(observed),'total_archived_messages':len(rows),
            'captured_through_timestamp':checkpoint['captured_through_timestamp'],'source_scope':checkpoint['capture_scope']}
