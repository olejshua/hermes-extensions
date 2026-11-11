"""Bounded loopback CPU vectors with exact-token chunking; no silent long-source truncation."""
import json,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
import numpy as np
from fastembed import TextEmbedding
from tokenizers import Tokenizer
MODEL='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
cache=Path(os.environ['BRAIN_MODEL_CACHE']);model=TextEmbedding(MODEL,cache_dir=str(cache),threads=2)
tokenizer_path=next(cache.glob('**/tokenizer.json'));tokenizer=Tokenizer.from_file(str(tokenizer_path));tokenizer.no_truncation()

def complete_embedding(texts):
 results=[];token_count=0;segment_count=0
 for text in texts:
  encoded=tokenizer.encode(text,add_special_tokens=False);offsets=encoded.offsets;token_count+=len(offsets)
  pieces=[text[offsets[i][0]:offsets[min(i+349,len(offsets)-1)][1]]for i in range(0,len(offsets),300)]if offsets else['']
  if len(pieces)>256:raise ValueError('tokenized input exceeds complete-vector bound')
  vectors=list(model.embed(pieces,batch_size=8));vector=np.mean(vectors,axis=0);norm=np.linalg.norm(vector);results.append((vector/norm if norm else vector).tolist());segment_count+=len(pieces)
 return results,token_count,segment_count
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"ready":true,"dimension":384,"long_input":"exact350token_chunks_normalized_aggregate","compute":"local_cpu"}')
 def do_POST(self):
  try:
   length=int(self.headers.get('Content-Length',0))
   if self.path!='/v1/embeddings'or length>2097152:raise ValueError('invalid request')
   data=json.loads(self.rfile.read(length));texts=data['input'];texts=[texts]if isinstance(texts,str)else texts
   if not isinstance(texts,list)or not 1<=len(texts)<=32 or any(not isinstance(t,str)or len(t)>65536 for t in texts):raise ValueError('input bound')
   vectors,tokens,segments=complete_embedding(texts);result={'object':'list','model':MODEL,'data':[{'object':'embedding','index':i,'embedding':v}for i,v in enumerate(vectors)],'usage':{'prompt_tokens':tokens,'total_tokens':tokens},'usage_kind':'local_cpu_tokenizer_count_not_subscription_usage','local_segments':segments}
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())
  except Exception:
   self.send_response(400);self.end_headers();self.wfile.write(b'{"error":"embedding request rejected; no truncation fallback"}')
if __name__=='__main__':HTTPServer(('127.0.0.1',8766),Handler).serve_forever()
