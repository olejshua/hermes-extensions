"""Explicitly authorized remaining-Vault context; never confirmed by indexing."""
import json,re,time,platform,io
from pathlib import Path
from .ole_policy import atomic,digest,status_of
from .scanner import _excluded,_safe_child
from .manifest import Source,SUPPORTED_EXTENSIONS,DEFAULT_EXCLUDES,BINARY_EXTENSIONS

SECRET=re.compile(r'(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{35}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:access_token|refresh_token|client_secret|api[_-]?key|password)\s*[=:]\s*["\']?[A-Za-z0-9/+_=-]{20,})',re.I)

def status(path, raw=None):
 if path.suffix.lower()!='.md':return 'unknown'
 # Header-only status extraction, matching exact status values, no namespace promotion.
 with (io.StringIO(raw.decode('utf-8')) if raw is not None else path.open(encoding='utf-8'))as f:
  if f.readline().strip()!='---':return 'unknown'
  header=[]
  for _ in range(512):
   line=f.readline()
   if not line:return 'unknown'
   if line.strip()=='---':break
   header.append(line)
  else:return 'unknown'
 import yaml
 try:data=yaml.safe_load(''.join(header))or{}
 except yaml.YAMLError:return 'unknown'
 tags=data.get('tags',[])if isinstance(data,dict)else[];tags=[tags]if isinstance(tags,str)else tags
 statuses=[t for t in tags if isinstance(t,str)and t.startswith('status/')]if isinstance(tags,list)else[]
 if len(statuses)!=1:return 'unknown'
 return {'status/bestätigt':'confirmed','status/entwurf':'hypothesis','status/veraltet':'superseded'}.get(statuses[0],'unknown')

def excluded_context_path(path,rel):
 if any(part in DEFAULT_EXCLUDES or part.lower() in ('.ssh','.config') for part in path.parts):return True
 if any(part.lower().startswith('.env')for part in path.parts):return True
 if re.fullmatch(r'(?:tokens?|passwords?|credentials?|secrets?|private[-_]keys?)',path.stem,re.I):return True
 return path.suffix.lower()in BINARY_EXTENSIONS

def text_of(path,raw=None):
 raw=path.read_bytes()if raw is None else raw
 if path.suffix.lower()=='.pdf':
  from pypdf import PdfReader
  return '\n\n'.join(p.extract_text()or''for p in PdfReader(io.BytesIO(raw)).pages)
 if path.suffix.lower()=='.docx':
  from docx import Document
  d=Document(io.BytesIO(raw));return '\n'.join(p.text for p in d.paragraphs)+'\n'+'\n'.join(' | '.join(c.text for c in row.cells)for t in d.tables for row in t.rows)
 return raw.decode('utf-8')

def export_context(vault: Path, output: Path, approved_receipts: Path, tokenizer_path: Path):
 from tokenizers import Tokenizer
 tokenizer=Tokenizer.from_file(str(tokenizer_path));tokenizer.no_truncation()
 if not vault.is_dir():raise FileNotFoundError('Vault unavailable; no replacement export')
 explicit_approved=json.loads(approved_receipts.read_text());skip={v['original_path']for v in explicit_approved['records'].values()}
 source=Source('allvault',vault,'ole_unconfirmed');inventory=[]
 for p in sorted(vault.rglob('*')):
  if p.is_symlink()or not p.is_file()or not _safe_child(vault,p):continue
  rel=p.relative_to(vault).as_posix()
  if str(p.resolve())in skip or excluded_context_path(p,rel)or p.suffix.lower()not in SUPPORTED_EXTENSIONS or any(x in p.parts for x in ('landingpage','runtime','logs','hermes-agent','vendor'))or p.stat().st_size>source.max_bytes:continue
  inventory.append((p,status(p)))
 records={};excluded=[];documents=0;now=time.time()
 for p,evidence_status in inventory:
  raw=p.read_bytes();text=text_of(p,raw);evidence_status=status(p,raw)
  if p.read_bytes()!=raw:raise RuntimeError("source changed during export; no receipt published")
  if SECRET.search(text):excluded.append({'original_path':str(p),'reason':'secret-like credential material; entire document excluded','pattern_categories':dict(__import__('collections').Counter(('private-key'if 'PRIVATE KEY' in m.group() else 'credential-assignment'if re.match(r'(access_token|refresh_token|client_secret|api[_-]?key|password)',m.group(),re.I)else'token-prefix')for m in SECRET.finditer(text)))});continue
  if not text.strip():excluded.append({'original_path':str(p),'reason':'no extractable text'});continue
  documents+=1
  # Exact model-tokenizer chunks: <=350 tokens with 50-token overlap, no long-document truncation.
  offsets=tokenizer.encode(text,add_special_tokens=False).offsets
  for i,token_start in enumerate(range(0,len(offsets),300)):
   start=offsets[token_start][0];end=offsets[min(len(offsets)-1,token_start+349)][1]
   piece=text[start:end]
   key=digest((str(p)+'\0'+str(i)).encode())[:32];name=key+'.md';data=piece.encode();atomic(output/'documents'/name,data)
   records[key]={'original_path':str(p),'export_path':name,'sha256':digest(raw),'export_sha256':digest(data),'status':evidence_status,'role':'unconfirmed_context'if evidence_status!='confirmed'else'confirmed_note_context','authority':'historical_evidence','approved_at':now,'char_start':start,'char_end':start+len(piece),'line_start':1+text[:start].count('\n'),'source_extension':p.suffix.lower()}
 for stale in (output/'documents').glob('*.md'):
  if stale.stem not in records:stale.unlink()
 receipt={'version':1,'source_host':platform.node(),'grant_id':'remaining-vault-context-2026-10-08','generated_at':now,'records':records,'source_documents':documents,'excluded':excluded,'source_instructions_authoritative':False,'dreaming_enabled':False}
 atomic(output/'receipts.json',json.dumps(receipt,ensure_ascii=False,indent=2).encode());return receipt
