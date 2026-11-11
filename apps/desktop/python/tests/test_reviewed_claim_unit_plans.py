import asyncio,sqlite3
import pytest
import aiosqlite
from potluck_local.services import reviewed_claim_unit_plans as plans, reviewed_knowledge as review, source_collections as sources
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.reviewed_source_unit_plan import text_hash

def test_lifecycle_binding_tamper_and_revocation(tmp_path,monkeypatch):
 path=tmp_path/'db';monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH',str(path));folder=tmp_path/'docs';folder.mkdir();file=folder/'a.md';file.write_text('# Policy\n\nSource evidence')
 async def run():
  await init_memory_db();col=await sources.register_collection('w','docs',folder);await sources.scan_collection('w',col)
  with sqlite3.connect(path) as db:
   doc,rev=db.execute('SELECT id,revision_id FROM source_documents').fetchone();chunk=db.execute('SELECT id FROM source_chunks').fetchone()[0]
  ref=dict(collection_id=col,document_id=doc,revision_id=rev,chunk_id=chunk);scope=[col];claim=await review.propose_claim('w',scope,'key','Rule applies.',[ref]);await review.approve_claim('w',scope,claim,'claim reviewer','checked')
  assert await plans.get_approved_plan('w',scope,claim) is None
  p={'source_sha256':text_hash('Rule applies.'),'units':[{'id':'rule','ranges':[[0,13]],'required_unit_ids':[]}]}
  first=await plans.propose_plan('w',scope,claim,p);second=await plans.propose_plan('w',scope,claim,p)
  with pytest.raises(ValueError):await plans.approve_plan('w',scope,first,'','reason')
  await plans.approve_plan('w',scope,first,'reviewer','checked');receipt=await plans.get_approved_plan('w',scope,claim)
  assert receipt['binding']['claim_reviewer']=='claim reviewer' and receipt['binding']['source_refs']==[ref]
  with pytest.raises(ValueError):await plans.approve_plan('w',scope,second,'reviewer','checked')
  with pytest.raises(LookupError):await plans.get_approved_plan('wrong',scope,claim)
  async with aiosqlite.connect(path) as db:
   await db.execute('BEGIN IMMEDIATE');assert await plans.validate_receipt_in_transaction(db,'w',scope,claim,receipt)==receipt;await db.rollback()
  await plans.supersede_plan('w',scope,first,second,'reviewer','replace')
  async with aiosqlite.connect(path) as db:
   db.row_factory=aiosqlite.Row;await db.execute('BEGIN IMMEDIATE')
   assert (await plans.get_approved_plan_in_transaction(db,'w',scope,claim))['id']==second
   await db.rollback()
  async with aiosqlite.connect(path) as db:
   await db.execute('BEGIN IMMEDIATE')
   with pytest.raises(ValueError):await plans.validate_receipt_in_transaction(db,'w',scope,claim,receipt)
   await db.rollback()
  with sqlite3.connect(path) as db:db.execute("UPDATE reviewed_claim_unit_plans SET plan_sha256='tampered' WHERE id=?",(second,))
  with pytest.raises(ValueError):await plans.get_approved_plan('w',scope,claim)
  file.write_text('# Changed\n\nNew evidence');await sources.scan_collection('w',col)
  with pytest.raises(LookupError):await plans.get_approved_plan('w',scope,claim)
 asyncio.run(run())

def test_atomic_approval_and_metadata_integrity(tmp_path, monkeypatch):
 path=tmp_path/'db';monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH',str(path));folder=tmp_path/'docs';folder.mkdir();file=folder/'a.md';file.write_text('# Source\n\nEvidence')
 async def run():
  await init_memory_db();col=await sources.register_collection('w','docs',folder);await sources.scan_collection('w',col)
  with sqlite3.connect(path) as db:
   doc,rev=db.execute('SELECT id,revision_id FROM source_documents').fetchone();chunk=db.execute('SELECT id FROM source_chunks').fetchone()[0]
  scope=[col];ref=dict(collection_id=col,document_id=doc,revision_id=rev,chunk_id=chunk);claim=await review.propose_claim('w',scope,'key','Rule applies.',[ref]);await review.approve_claim('w',scope,claim,'reviewer','valid')
  plan={'source_sha256':text_hash('Rule applies.'),'units':[{'id':'rule','ranges':[[0,13]],'required_unit_ids':[]}]};a=await plans.propose_plan('w',scope,claim,plan);b=await plans.propose_plan('w',scope,claim,plan)
  outcomes=await asyncio.gather(plans.approve_plan('w',scope,a,'reviewer','ok'),plans.approve_plan('w',scope,b,'reviewer','ok'),return_exceptions=True);assert sum(isinstance(x,Exception) for x in outcomes)==1
  current=await plans.get_approved_plan('w',scope,claim)
  async with aiosqlite.connect(path) as db:
   with pytest.raises(ValueError,match='transaction'):await plans.get_approved_plan_in_transaction(db,'w',scope,claim)
  with pytest.raises(LookupError):await plans.get_approved_plan('w',['wrong'],claim)
  with sqlite3.connect(path) as db:db.execute("UPDATE reviewed_claim_unit_plans SET status='superseded' WHERE id=?",(current['id'],))
  with pytest.raises(ValueError):await plans.get_approved_plan('w',scope,claim)
 asyncio.run(run())

def test_claim_supersession_and_failed_scan(tmp_path,monkeypatch):
 path=tmp_path/'db';monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH',str(path));folder=tmp_path/'docs';folder.mkdir();file=folder/'a.md';file.write_text('# Source\n\nEvidence')
 async def run():
  await init_memory_db();col=await sources.register_collection('w','docs',folder);await sources.scan_collection('w',col)
  with sqlite3.connect(path) as db:
   doc,rev=db.execute('SELECT id,revision_id FROM source_documents').fetchone();chunk=db.execute('SELECT id FROM source_chunks').fetchone()[0]
  scope=[col];ref=dict(collection_id=col,document_id=doc,revision_id=rev,chunk_id=chunk);old=await review.propose_claim('w',scope,'key','Rule applies.',[ref]);await review.approve_claim('w',scope,old,'r','ok');plan={'source_sha256':text_hash('Rule applies.'),'units':[{'id':'rule','ranges':[[0,13]],'required_unit_ids':[]}]};pid=await plans.propose_plan('w',scope,old,plan);await plans.approve_plan('w',scope,pid,'r','ok')
  new=await review.propose_claim('w',scope,'key','New rule.',[ref]);await review.supersede_claim('w',scope,old,new,'r','replace')
  with pytest.raises(LookupError):await plans.get_approved_plan('w',scope,old)
  file.write_bytes(b'\xff')
  with pytest.raises(UnicodeError):await sources.scan_collection('w',col)
  with pytest.raises(LookupError):await plans.get_approved_plan('w',scope,new)
 asyncio.run(run())
