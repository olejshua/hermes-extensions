"""Explicit read/RCPT-only broker. No Gmail/CRM write route or credentials in AI sandbox."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime,timezone
from pathlib import Path
import hashlib,html,ipaddress,json,os,re,socket,sys,threading,time
from urllib.parse import urlsplit
from urllib.request import build_opener,HTTPRedirectHandler,Request

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'candidate'))
from acribic_pilot.central import Central,Stop,atomic

class Broker:
    def __init__(self,day,state):
        self.day=Path(day);self.state=Path(state);self.evidence=self.day/'research'
        self.evidence.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.stop=threading.Event();self.rows={};self.v=None

    def initialize(self):
        if self.v:return
        os.environ['ACRIBIC_CRM_BACKEND']='twenty'
        os.environ['ACRIBIC_VERSAND_STATE']=str(self.evidence/'state')
        os.environ['TZ']='Europe/Berlin';time.tzset()
        sys.path[:0]=['/home/hermes/acribic-smtp',str(HERE/'vendor'),'/home/hermes/.hermes/scripts',str(HERE/'candidate')]
        import smtp_pruefen
        prior=sys.argv;sys.argv=[prior[0],'--liste','it']
        try:import versand as v
        finally:sys.argv=prior
        import versand_senden_sicher as sender,email_watcher,ted_original_reader
        from acribic_pilot.gmail import Gmail
        from acribic_pilot.evidence import Evidence
        self.v=v;self.sender=sender;self.ted_reader=ted_original_reader
        v.SPERR=str(self.state/'protections/Nicht-kontaktieren.md')
        v.NACHTRAG=str(self.state/'protections/Nachtrag-seit-Export.csv')
        self.gmail=Gmail(lambda:email_watcher.service('code'),allow_write=False)
        self.reader=Evidence(self.evidence,ted_original_reader,v,smtp_pruefen)
        c=json.loads((self.day/'positive-control.json').read_text())
        sender.profile_and_control(self.gmail,c['id'],c['query'])

    def pool(self):
        self.initialize();v=self.v
        groups=v.TWENTY.refresh(complete=True)
        central=Central(self.state/'central')
        with central.lock():
            central.assert_clear();events=central.events()
        old_mail=set();old_domains=set();old_ids=set()
        for e in events:
            if e['kind']=='legacy_snapshot':
                old_mail.update(e['payload']['mailboxes']);old_domains.update(e['payload']['org_domains']);old_ids.update(e['payload']['source_ids'])
            else:
                old_mail.add(e['mailbox']);old_domains.add(e['org_domain']);old_ids.add(e['source_id'].replace('-',''))
        output={}
        all_rows=[r for rows in groups.values()for r in rows]
        blocked_aliases={v.firma_key(r.get('Firma')or'')for r in all_rows if r.get('_TwentyBlocked')or r.get('_TwentyKind')=='native'or v.crm_gesperrt(r)}
        blocked_domains={v.basis(v.dom(r.get(field)or''))for r in all_rows for field in ('E-Mail','Website','Domain')
            if r.get('_TwentyBlocked')or r.get('_TwentyKind')=='native'or v.crm_gesperrt(r)}-{''}
        from acribic_pilot.identity import ProtectedNames
        protected_names=ProtectedNames([r for r in all_rows if r.get('_TwentyBlocked')or r.get('_TwentyKind')=='native'or v.crm_gesperrt(r)],v.firma_key)
        for segment in ('it','medizin','beratung','ingenieur'):
            v.LISTE=segment;v.DB=v.LISTEN[segment];v._ANDERE=None
            candidates=v.candidate_pool(groups[segment]);rows=[]
            for r in candidates:
                email=r['E-Mail'].strip().casefold();domain=v.basis(v.dom(email))
                if email in old_mail or domain in old_domains or r['id'].replace('-','')in old_ids:continue
                # Early denial only, using the same exact native identity keys.
                # Full reviewed family and live send gates remain mandatory.
                if v.firma_key(r.get('Firma')or'')in blocked_aliases or domain in blocked_domains:continue
                if protected_names.collision([v.firma_key(r.get('Firma')or'')],r['id']):continue
                if re.search(r'\b(excluded-company-1|excluded-company-2|excluded-company-3|excluded-company-4|excluded-company-5|excluded-company-6|excluded-company-7|excluded-company-8|excluded-company-9)\b',r.get('Firma',''),re.I):continue
                value={**r,'liste':segment};rows.append(value);self.rows[r['id']]=value
            output[segment]=rows
        atomic(self.evidence/'candidate-pools.json',output)
        atomic(self.evidence/'live-snapshot.json',{'anchor':v.TWENTY.anchor,'records':v.TWENTY.records,'policy':v.TWENTY.policy})
        return {'path':str(self.evidence/'candidate-pools.json'),'counts':{k:len(x)for k,x in output.items()},'source_objects':{k:len(x)for k,x in v.TWENTY.records.items()},'seven_segments':list(groups),
            'existing_research_index':str(self.day/'work/research-index.json'),'reuse_originals':str(self.evidence)}

    def row(self,source_id):
        if not self.rows:
            if (self.evidence/'candidate-pools.json').exists():
                self.rows={r['id']:r for rs in json.loads((self.evidence/'candidate-pools.json').read_text()).values()for r in rs}
            else:self.pool()
        if source_id not in self.rows:raise Stop('only_current_allowed_pool_source_ids')
        return self.rows[source_id]

    def screen_one(self,source_id):
        row=self.row(source_id);v=self.v;email=row['E-Mail'].strip().casefold()
        result={'source_id':source_id,'company':row['Firma'],'email':email,'org_domain':v.basis(v.dom(email))}
        try:
            mail={'to':email,'firma':row['Firma']}
            self.sender.no_history(self.gmail,mail,v)
            probe=self.reader.probe(email)
            result.update(state='technical_candidate'if probe['status']=='OK'else 'blocked_smtp',smtp=probe)
        except Exception as error:result.update(state='blocked',reason=str(error)[:260])
        atomic(self.evidence/('screen-'+source_id+'.json'),result)
        return result

    def screen(self,ids):
        self.initialize()
        if not isinstance(ids,list)or not 1<=len(ids)<=30 or len(set(ids))!=len(ids):raise Stop('screen_1_to_30_distinct_source_ids')
        for key in ids:self.row(key)
        with ThreadPoolExecutor(max_workers=2)as workers:results=list(workers.map(self.screen_one,ids))
        return results

    def ted(self,source_id):
        self.initialize();row=self.row(source_id);t=self.ted_reader
        folder=self.evidence/'ted'/source_id;folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        found=t.search_winner(row['Firma']);atomic(folder/'search.json',found)
        result={'company':row['Firma'],'page_id':source_id,'awards':[],'excluded':[]}
        from acribic_pilot.legacy_ted import legacy_awards
        for notice in found['response']['notices']:
            pub=notice['publication-number']
            try:
                raw,http=t.download(pub);path=folder/(pub+'.xml');path.write_bytes(raw);path.chmod(0o600)
                try:facts=t.award_facts(raw,row['Firma'])
                except t.ContractError as error:
                    if str(error)!='ted_supported_award_notice_required':raise
                    facts=legacy_awards(raw,row['Firma'],pub)
                result['awards'].extend({**f,'publication':pub,'url':http['requested_url'],
                    'xml_path':str(path),'xml_sha256':hashlib.sha256(raw).hexdigest()}for f in facts)
                modern=[f for f in result['awards']if f.get('procedure_id')]
                procedures={f['procedure_id']for f in modern}
                legacy={f['publication']for f in result['awards']if not f.get('procedure_id')}
                if modern and len(procedures)+len(legacy)>=2:break
            except Exception as error:result['excluded'].append({'publication':pub,'reason':str(error)[:180]})
        atomic(folder/'facts.json',result)
        return result

    def website(self,url):
        def validate(value):
            u=urlsplit(value)
            if u.scheme!='https'or not u.hostname or u.username or u.password or u.port not in (None,443):raise Stop('public_HTTPS_source_required')
            if any(not ipaddress.ip_address(a[4][0]).is_global for a in socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM)):
                raise Stop('public_source_only')
        validate(url)
        class Redirects(HTTPRedirectHandler):
            def redirect_request(self,req,fp,code,msg,headers,newurl):
                validate(newurl);return super().redirect_request(req,fp,code,msg,headers,newurl)
        with build_opener(Redirects()).open(Request(url,headers={'User-Agent':'Hermes-Acribic-Research'}),timeout=25)as response:
            raw=response.read(3_000_001)
            if len(raw)>3_000_000:raise Stop('source_too_large')
            if response.status!=200:raise Stop('source_HTTP_not_OK')
            if not any(v in response.headers.get('Content-Type','')for v in ('text/html','text/plain','application/xhtml+xml')):raise Stop('text_source_required')
            source=raw.decode(response.headers.get_content_charset()or'utf-8',errors='replace')
            text=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>',' ',source,flags=re.S|re.I)
            text=html.unescape(re.sub('<[^>]+>',' ',text));text=re.sub(r'\s+',' ',text).strip()
            key=hashlib.sha256(url.encode()).hexdigest();path=self.evidence/'web'/key;path.parent.mkdir(mode=0o700,exist_ok=True)
            path.with_suffix('.html').write_bytes(raw)
            meta={'url':url,'final_url':response.url,'checked_at':datetime.now(timezone.utc).isoformat(),
                'sha256':hashlib.sha256(raw).hexdigest(),'text':text,'path':str(path.with_suffix('.html'))}
            atomic(path.with_suffix('.json'),meta)
            return {**meta,'text':text[:18000],'full_text_path':str(path.with_suffix('.json'))}

    def render(self,values):
        self.initialize();row=self.row(values['source_id']);buyer=values['buyer'];first=values.get('first_name')
        if not isinstance(buyer,str)or not buyer.strip():raise Stop('actual_buyer_required')
        subject=values.get('subject')or'Austausch zu öffentlichen Ausschreibungen'
        text_buyer=buyer if subject=='Austausch zu öffentlichen Ausschreibungen'else subject
        return {'firma':row['Firma'],'liste':row['liste'],'page_id':row['id'],'to':row['E-Mail'].strip().casefold(),
            'org_domain':self.v.basis(self.v.dom(row['E-Mail'])),'vorname':first,
            'subject':subject,'ted_buyer':buyer,'gekuerzt':len(buyer)>70 and subject!=buyer,'flags':[],
            'body':self.v.mailtext(first,row['Firma'],text_buyer)}

    def family(self,values):
        self.initialize();row=self.row(values['source_id']);v=self.v
        aliases=values['aliases'];domains=values['domains']
        if (not isinstance(aliases,list)or not 1<=len(aliases)<=40 or not isinstance(domains,list)or not 1<=len(domains)<=40
                or any(not isinstance(a,str)or not a.strip()or any(c in a for c in '\"\r\n')for a in aliases)
                or any(not isinstance(d,str)or not re.fullmatch(r'[a-z0-9.-]+',d)for d in domains)):
            raise Stop('explicit_safe_family_aliases_and_domains_required')
        v.LISTE=row['liste'];v.DB=v.LISTEN[v.LISTE]
        if not v.TWENTY.records:v.TWENTY.refresh(complete=True)
        snapshot=self.sender.assemble_crm_snapshot(v,v.TWENTY.groups())
        mail={'to':row['E-Mail'].strip().casefold(),'page_id':row['id'],'firma':row['Firma'],
            'org_domain':v.basis(v.dom(row['E-Mail'])),'family_id':values['family_id']}
        manifest={'family_id':values['family_id'],'family_aliases':aliases,'family_domains':domains}
        from copy import copy
        checker=copy(self.reader);checker.manifest=lambda:{row['id']:manifest}
        self.sender.validate_live(v,mail,snapshot);checker.family(mail,snapshot)
        with Central(self.state/'central').lock()as central:
            central.assert_clear()
            for event in central.events():
                if event['kind']=='legacy_snapshot':
                    if set(domains)&set(event['payload']['org_domains']):raise Stop('family_legacy_reservation')
                elif event.get('org_domain')in domains:raise Stop('family_prior_central_attempt')
        self.sender.no_history(self.gmail,mail,v)
        queries=[f'in:anywhere (to:{d} OR from:{d})'for d in domains if d!=mail['org_domain']]
        queries+=['in:anywhere "'+a+'"'for a in aliases if a!=mail['firma']]
        for response in self.sender.fetch_queries(self.gmail,queries):
            if response.get('messages')or response.get('nextPageToken')or response.get('resultSizeEstimate',0):raise Stop('family_Gmail_history')
        answer={'passed':True,'source_id':row['id'],'company':row['Firma'],'family_domains':domains,'family_aliases':aliases,
            'checked_at':datetime.now(timezone.utc).isoformat(),'preparation_only':True}
        atomic(self.evidence/('family-'+row['id']+'.json'),answer);return answer

    def handle(self,value):
        if not isinstance(value,dict)or set(value)!={'command','arguments'}:raise Stop('explicit_read_request_required')
        c,a=value['command'],value['arguments']
        if c=='pool'and a=={}:return self.pool()
        if c=='screen'and set(a)=={'ids'}:return self.screen(a['ids'])
        if c=='ted'and set(a)=={'source_id'}:return self.ted(a['source_id'])
        if c=='website'and set(a)=={'url'}:return self.website(a['url'])
        if c=='render'and set(a)<= {'source_id','buyer','first_name','subject'}:return self.render(a)
        if c=='family'and set(a)=={'source_id','family_id','aliases','domains'}:return self.family(a)
        raise Stop('read_command_not_allowed')

    def serve(self):
        # Only the parent process owns credentials/network. AI subprocesses
        # can read broker evidence and submit these five bounded read commands.
        while not self.stop.is_set():
            for base in [self.day/'work/.rpc',self.day/'review/.rpc']:
                for path in sorted(base.glob('*.request.json')):
                    out=path.with_name(path.name.replace('.request.json','.response.json'))
                    if out.exists():continue
                    try:
                        if path.is_symlink()or path.stat().st_size>16000:raise Stop('read_request_size_or_link')
                        with redirect_stdout(sys.stderr):result=self.handle(json.loads(path.read_text()))
                        value={'ok':True,'result':result}
                    except Exception as error:value={'ok':False,'error':type(error).__name__+': '+str(error)[:220]}
                    atomic(out,value)
            self.stop.wait(.3)
