"""Official originals and an explicit reviewed family/fit manifest."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import subprocess
import re
from .central import Stop
from .legacy_ted import legacy_awards
from .identity import ProtectedNames


class Evidence:
    def __init__(self,root,reader,v,smtp):
        self.root=Path(root);self.reader=reader;self.v=v;self.smtp=smtp

    def manifest(self):
        value=json.loads((self.root/'firm-evidence.json').read_text())
        if value.get('schema')!='acribic-firm-review/v1':raise Stop('firm_evidence_schema')
        return value['rows']

    def validate_all(self,mails):
        aliases=set();domains=set()
        for mail in mails:
            row=self.manifest()[mail['page_id']]
            current_aliases={self.v.firma_key(v)for v in row['family_aliases']}
            current_domains=set(row['family_domains'])
            if aliases&current_aliases or domains&current_domains:raise Stop('overlapping_reviewed_families_in_parent_ten')
            aliases.update(current_aliases);domains.update(current_domains)

    def family(self,mail,snapshot):
        row=self.manifest()[mail['page_id']]
        aliases={self.v.firma_key(a)for a in row['family_aliases']}
        domains=set(row['family_domains'])
        if not aliases or not domains or row['family_id']!=mail['family_id']:raise Stop('family_review_missing')
        protected=[candidate for candidate in snapshot[1]
            if(candidate.get('Stage')!='Nicht kontaktiert'or self.v.crm_gesperrt(candidate)
               or candidate.get('_TwentyBlocked')or candidate.get('_TwentyKind')=='native')]
        collision=ProtectedNames(protected,self.v.firma_key).collision(aliases,mail['page_id'])
        if collision=='exact_name_variant':raise Stop('family_member_contacted_reserved_or_unreviewed')
        if collision:raise Stop('unresolved_similar_protected_company')
        for candidate in snapshot[1]:
            name=self.v.firma_key(candidate.get('Firma')or'')
            email=candidate.get('E-Mail')or'';website=candidate.get('Website')or''
            hit=name in aliases or self.v.basis(self.v.dom(email))in domains or self.v.basis(self.v.dom(website))in domains
            if hit and str(candidate.get('id','')).replace('-','')!=mail['page_id'].replace('-',''):
                if (candidate.get('Stage')!='Nicht kontaktiert'or self.v.crm_gesperrt(candidate)
                        or candidate.get('_TwentyBlocked')or candidate.get('_TwentyKind')=='native'):
                    raise Stop('family_member_contacted_reserved_or_unreviewed')
        # Known aliases/domains receive independent all-mail Gmail checks in
        # launcher family_history. Similar words never infer a parent company.
        return row

    def ted(self,mail):
        row=self.manifest()[mail['page_id']]
        if not row.get('fit_review')or not row.get('pressure_sources')or not row.get('family_sources'):
            raise Stop('manual_firm_review_required')
        primary=row['primary'];raw,http=self.reader.download(primary['publication'])
        if hashlib.sha256(raw).hexdigest()!=primary['xml_sha256']:raise Stop('official_primary_original_changed')
        facts=self.reader.award_facts(raw,mail['firma'])
        matching=[f for f in facts if f['lot_id']==primary['lot_id']and f['buyer']==mail['ted_buyer']]
        if len(matching)!=1 or primary['publication']!=mail['ted_publication']:raise Stop('selected_winner_buyer_chain_changed')
        notices={primary['publication']}
        repeated=False
        for repeat in row['repeat_awards']:
            path=Path(repeat['xml_path']);data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=repeat['xml_sha256']:raise Stop('repeat_original_changed')
            try:
                additional=self.reader.award_facts(data,mail['firma'])
                if any(f['procedure_id']!=matching[0]['procedure_id']for f in additional):repeated=True
            except self.reader.ContractError as error:
                if str(error)!='ted_supported_award_notice_required':raise
                legacy=legacy_awards(data,mail['firma'],repeat['publication'])
                if legacy and repeat['publication']!=primary['publication']:repeated=True
            notices.add(repeat['publication'])
        if len(notices)<2 or not repeated:raise Stop('repeated_TED_wins_unproven')
        return matching[0]

    def probe(self,address):
        from zoneinfo import ZoneInfo
        ip=subprocess.check_output(['curl','-4fsS','--max-time','15','https://api.ipify.org'],text=True).strip()
        if ip!='45.89.127.191':raise Stop('unreviewed_smtp_exit')
        domain=address.split('@')[1];dns=self.mx(domain);mx=dns['mx']
        random=self.smtp.zufall(domain);real=self.smtp.rcpt(mx,address);control=self.smtp.rcpt(mx,random)
        now=datetime.now(timezone.utc)
        return {'status':self.smtp.status(real,control),'real':real,'random':control,'random_address':random,
                'mx':mx,'dns_evidence':dns['lookups'],'ip':ip,'checked_at':now.isoformat(),'berlin_date':now.astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()}

    def mx(self,domain):
        """Same installed dig lookup, with diagnostics and one technical TCP fallback.

        No old MX cache, address substitution, or SMTP retry. An actual empty
        answer, Null MX or NXDOMAIN remains a hard block.
        """
        if not re.fullmatch(r'[a-z0-9.-]+',domain,re.I):raise Stop('invalid_MX_domain')
        lookups=[]
        for tcp in (False,True):
            args=['/usr/bin/dig','+time=3','+tries=1']+(['+tcp']if tcp else[])+['MX',domain]
            row={'domain':domain,'transport':'tcp'if tcp else'udp','command':args,
                 'at':datetime.now(timezone.utc).isoformat()}
            try:
                result=subprocess.run(args,capture_output=True,text=True,timeout=5)
                row.update(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)
            except subprocess.TimeoutExpired as error:
                row.update(returncode=None,error='TimeoutExpired',stdout=str(error.stdout or''),stderr=str(error.stderr or''))
            except OSError as error:
                raise Stop('MX_exec_failed:'+domain+':'+type(error).__name__)from error
            lookups.append(row)
            with (self.root/'dns-lookups.jsonl').open('a')as log:log.write(json.dumps(row,ensure_ascii=False)+'\n')
            status=re.search(r'status:\s*([A-Z]+)',row.get('stdout',''))
            status=status.group(1)if status else None
            if row['returncode']==0 and status=='NXDOMAIN':raise Stop('smtp_no_MX:'+domain+':NXDOMAIN')
            if row['returncode']==0 and status=='NOERROR':
                records=[]
                for line in row['stdout'].splitlines():
                    parts=line.split()
                    if not line.startswith(';')and len(parts)==6 and parts[3]=='MX'and parts[4].isdigit():
                        records.append((int(parts[4]),parts[5].rstrip('.')))
                if any(not host for _,host in records):raise Stop('smtp_null_MX:'+domain)
                if not records:raise Stop('smtp_no_MX:'+domain+':NOERROR')
                host=sorted(records)[0][1]
                if not re.fullmatch(r'[a-z0-9.-]+',host,re.I):raise Stop('invalid_MX_answer')
                return {'mx':host,'lookups':lookups}
            # Only a technical lookup failure reaches the one fresh TCP read.
        raise Stop('MX_technical_lookup_failed:'+domain)
