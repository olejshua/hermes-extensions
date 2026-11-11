"""One source-bound Google invitation phase and a separate owned-hold release.

Invoke through the existing GTM Coordinator so its durable marker precedes each
write. This module never sends Gmail, mutates CRM, guesses an agreement, or
replays an uncertain calendar insert. Google services are injected, never authed here.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
import re
from gtm.coordinator import Readback
from gtm.calendar.confirmation import (CODE,PIET,ResolvedAcceptance,plan_acribic_invitation,
                                      resolve_single_proposal_acceptance,authored_text)
from gtm.calendar.routing import ACRIBIC, AVAILABILITY, MANAGED, claim_calendar

BERLIN=ZoneInfo('Europe/Berlin')
def _dt(value):
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Timezone missing')
    return stamp

def _get(calendar,event_id,calendar_id=CODE):
    try:return calendar.events().get(calendarId=calendar_id,eventId=event_id).execute()
    except Exception as error:
        if getattr(getattr(error,'resp',None),'status',None)==404:return None
        raise

def _pages(calendar,**kwargs):
    items=[];seen=set();token=None
    for _ in range(20):
        result=calendar.events().list(**kwargs,**({'pageToken':token} if token else {})).execute()
        if not isinstance(result.get('items'),list):raise ValueError('Incomplete event overlap evidence')
        items.extend(result['items']);token=result.get('nextPageToken')
        if not token:return items
        if not isinstance(token,str) or token in seen:raise ValueError('Invalid event page continuation')
        seen.add(token)
    raise ValueError('Event page cap; no availability claim')

def _interval(event):
    def endpoint(value):
        if 'dateTime' in value:return _dt(value['dateTime'])
        if 'date' in value:return datetime.fromisoformat(value['date']).replace(tzinfo=BERLIN)
        raise ValueError('Unknown event interval')
    a,b=endpoint(event['start']),endpoint(event['end'])
    if a>=b:raise ValueError('Invalid event interval')
    return a,b

class InvitationAdapter:
    def __init__(self,calendar,hold_policy,identity,acceptance:ResolvedAcceptance,hold_id,*,source_reader,conference_mode='auto',join_url=None,location=None,clock=None,acceptance_resolver=None):
        if not isinstance(acceptance,ResolvedAcceptance):raise ValueError('Resolver-bound full-source acceptance required')
        self.calendar,self.policy,self.identity,self.acceptance=calendar,hold_policy,identity.checked(),acceptance
        self.binding=acceptance.binding;self.hold_id=hold_id
        self.calendar_id=ACRIBIC
        self.hold_calendar_id=claim_calendar(hold_policy._read().get(hold_id,{}))
        self.source_reader=source_reader;self.clock=clock or (lambda:datetime.now(timezone.utc))
        if acceptance_resolver is not None:
            from gtm.calendar.reply_acceptance import SemanticAcceptanceResolver
            if not isinstance(acceptance_resolver,SemanticAcceptanceResolver):
                raise ValueError('Only reviewed typed source-bound resolver supported')
        self.acceptance_resolver=acceptance_resolver or resolve_single_proposal_acceptance
        resolved=self.acceptance_resolver(self.binding,acceptance.proposal,acceptance.incoming)
        if resolved!=acceptance:raise ValueError('Acceptance changed after resolution')
        if (identity.category!='acribic' or identity.account!=CODE or identity.kind!='reply' or
            identity.source_id!=acceptance.incoming_message_id or
            identity.source_hash!=hashlib.sha256(acceptance.incoming.source_text().encode()).hexdigest()):
            raise ValueError('Invitation identity differs from original incoming message')
        self.owner=json.dumps(['code',self.binding.thread_id,[self.binding.recipient.lower()]],separators=(',',':'))
        expected_hold=hashlib.sha256((self.owner+'|'+self.binding.start.astimezone(timezone.utc).isoformat()).encode()).hexdigest()[:40]
        legacy_hold=hashlib.sha256(('code|'+self.binding.thread_id+'|'+self.binding.start.astimezone(timezone.utc).isoformat()).encode()).hexdigest()[:40]
        if hold_id not in (expected_hold,legacy_hold):raise ValueError('Hold ID does not match exact source-bound owner/start')
        self.legacy_hold=hold_id==legacy_hold
        key=json.dumps([CODE,self.binding.thread_id,self.binding.proposal_message_id,self.binding.recipient.lower(),
                        self.binding.start.astimezone(timezone.utc).isoformat(),self.binding.duration_minutes],separators=(',',':'))
        self.event_id=hashlib.sha256(key.encode()).hexdigest()[:40] # lowercase base32hex-compatible
        plan=plan_acribic_invitation(self.binding,acceptance,availability='free',own_hold_readback_verified=True)
        self.body=plan['body'];self.body['id']=self.event_id
        self.body['start']['timeZone']='Europe/Berlin';self.body['end']['timeZone']='Europe/Berlin'
        # Both complete originals establish the agreed medium. A reply asking for
        # Teams must never silently receive a newly generated Google Meet.
        authored=authored_text(acceptance.proposal.body)+'\n'+authored_text(acceptance.incoming.body)
        if conference_mode=='auto':
            if join_url:conference_mode='existing_url'
            elif location:conference_mode='physical'
            else:
                urls=re.findall(r'https://[^\s<>"\']+',authored)
                meeting_urls=[u.rstrip('.,;)') for u in urls if urlsplit(u.rstrip('.,;)')).hostname in
                              ('meet.google.com','teams.microsoft.com','teams.live.com') or
                              (urlsplit(u.rstrip('.,;)')).hostname or '').endswith('.zoom.us')]
                if len(meeting_urls)>1:raise ValueError('Multiple source meeting links require review')
                if meeting_urls:join_url=meeting_urls[0];conference_mode='existing_url'
                else:conference_mode='google_meet'
        self.conference_mode=conference_mode;self.join_url=join_url;self.location=location
        if conference_mode=='existing_url':
            parsed=urlsplit(join_url or '')
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or join_url not in authored:
                raise ValueError('Existing join URL must be HTTPS and present in the original proposal')
            self.body['description']='Besprechungslink: '+join_url
        elif conference_mode=='google_meet':
            if join_url or location or any(word in authored.casefold() for word in ('teams','zoom','webex','vor ort')):
                raise ValueError('Do not replace an agreed Teams/existing medium with Google Meet')
            self.body['conferenceData']={'createRequest':{'requestId':self.event_id,
                'conferenceSolutionKey':{'type':'hangoutsMeet'}}}
        elif conference_mode=='physical':
            if not location or location not in authored or join_url:
                raise ValueError('Physical location must be explicit in the original proposal')
            self.body['location']=location
        else:raise ValueError('Explicit reviewed conference mode required; no naked default invitation')
        self.body['extendedProperties']={'private':{'hermesGTMInvitation':'1','gtmSourceKey':identity.key,
           'gtmSourceHash':identity.source_hash,'sourceThread':self.binding.thread_id,
           'sourceProposal':self.binding.proposal_message_id,'acceptedMessage':acceptance.incoming_message_id,
           'holdOwner':self.owner,'holdId':hold_id,'ole_gtm_project':'acribic','sourceAccount':CODE}}
    def _identity(self,identity):
        if identity!=self.identity:raise ValueError('Wrong source/version/account identity')
    def _matches(self,event):
        try:
            actual=[a['email'].casefold() for a in event['attendees']]
            expected=[CODE,PIET,self.binding.recipient.casefold()]
            return (event.get('id')==self.event_id and event.get('status')!='cancelled' and
                event.get('summary')==self.body['summary'] and event.get('organizer',{}).get('email','').casefold()==self.calendar_id and _interval(event)==(self.binding.start,self.binding.start+timedelta(minutes=self.binding.duration_minutes)) and
                len(actual)==3 and set(actual)==set(expected) and
                not any(a.get('responseStatus')=='declined' for a in event['attendees']) and
                event.get('extendedProperties',{}).get('private')==self.body['extendedProperties']['private'])
        except (KeyError,TypeError,ValueError):return False
    def _conference_ready(self,event):
        if self.conference_mode=='existing_url':return event.get('description')==self.body['description']
        if self.conference_mode=='physical':return event.get('location')==self.location
        data=event.get('conferenceData',{});request=data.get('createRequest',{})
        if request.get('requestId')!=self.event_id:return False
        code=request.get('status',{}).get('statusCode')
        if code=='pending':return None
        if code!='success':return False
        videos=[e.get('uri') for e in data.get('entryPoints',[]) if e.get('entryPointType')=='video']
        return len(videos)==1 and urlsplit(videos[0] or '').scheme=='https' and urlsplit(videos[0] or '').hostname=='meet.google.com'
    def readback(self,identity):
        self._identity(identity)
        try:event=self._event()
        except Exception:return Readback('unknown',identity)
        if event is None:return Readback('absent',identity)
        if not self._matches(event):return Readback('conflict',identity)
        ready=self._conference_ready(event)
        if ready is None:return Readback('unknown',identity)
        return Readback('verified' if ready else 'conflict',identity,self.event_id if ready else None)
    def conference_receipt(self,identity):
        """Private caller receipt; never claims complete while Google is pending."""
        self._identity(identity)
        try:event=self._event()
        except Exception:return {'state':'unknown','event_id':self.event_id}
        if event is None:return {'state':'absent','event_id':self.event_id}
        if not self._matches(event):return {'state':'conflict','event_id':self.event_id}
        ready=self._conference_ready(event)
        status='pending' if ready is None else 'verified' if ready else 'failed_or_mismatched'
        uri=self.join_url
        if self.conference_mode=='google_meet' and ready:
            uri=next(e['uri'] for e in event['conferenceData']['entryPoints'] if e.get('entryPointType')=='video')
        return {'state':status,'event_id':self.event_id,'conference_mode':self.conference_mode,
                'join_url':uri if ready else None,'location':self.location if ready else None,
                'calendar_id':self.calendar_id}
    def _event(self):
        # Old invitations stay on CODE. A matching deterministic ID on either
        # managed calendar prevents a second invitation on the new target.
        found=[(cid,event) for cid in MANAGED if (event:=_get(self.calendar,self.event_id,cid)) is not None]
        if len(found)>1:raise ValueError('Invitation ID exists on multiple calendars')
        if not found:return None
        self.calendar_id,event=found[0]
        return event
    @staticmethod
    def owned_hold_id(calendar,binding):
        """Read exact deterministic managed ID, then legacy only after real 404.

        Network/auth failures propagate. No broad title/address discovery and no
        journal adoption occur before the invitation's positive readback.
        """
        owner=json.dumps(['code',binding.thread_id,[binding.recipient.lower()]],separators=(',',':'))
        stamp=binding.start.astimezone(timezone.utc).isoformat()
        managed=hashlib.sha256((owner+'|'+stamp).encode()).hexdigest()[:40]
        found=[cid for cid in MANAGED if _get(calendar,managed,cid) is not None]
        if len(found)>1:raise ValueError('Owned hold ID exists on multiple calendars')
        if found:return managed
        return hashlib.sha256(('code|'+binding.thread_id+'|'+stamp).encode()).hexdigest()[:40]
    def verified_hold(self):
        hold=_get(self.calendar,self.hold_id,self.hold_calendar_id)
        if not hold:raise ValueError('Owned hold missing')
        props=hold.get('extendedProperties',{}).get('private',{})
        if self.legacy_hold:
            proposal=self.acceptance.proposal
            if (props.get('hermesOwner') or props.get('sourceAccount')!='code' or
                props.get('sourceThread')!=self.binding.thread_id or
                props.get('sourceMessage')!=proposal.message_id or 'SENT' not in proposal.labels):
                raise ValueError('Legacy hold requires exact original SENT proposal')
            claim=self.policy._read().get(self.hold_id)
            if claim and (claim.get('kind')!='legacy_slot_hold' or claim.get('owner')!=self.owner or
                claim.get('event_id')!=self.hold_id or claim.get('state')!='held_draft_verified' or
                claim.get('legacy_source_message')!=proposal.message_id or
                claim.get('legacy_source_hash')!=hashlib.sha256(proposal.source_text().encode()).hexdigest()):
                raise ValueError('Legacy hold journal conflicts with original proposal')
        elif props.get('hermesOwner')!=self.owner:raise ValueError('Managed hold owner mismatch')
        if (props.get('hermesSlotHold')!='1' or
            hold.get('visibility')!='private' or hold.get('transparency')!='opaque' or hold.get('attendees') or
            hold.get('status')=='cancelled' or _interval(hold)!=(self.binding.start,self.binding.start+timedelta(minutes=30))):
            raise ValueError('Exact private participant-free owned hold required')
        return hold
    def preflight(self,identity):
        self._identity(identity)
        if self.readback(identity).state!='absent':raise ValueError('Invitation create requires confirmed 404')
        proposal,incoming=self.source_reader()
        if (proposal.source_text()!=self.acceptance.proposal.source_text() or
            incoming.source_text()!=self.acceptance.incoming.source_text()):raise ValueError('Live source changed or superseded')
        self.acceptance_resolver(self.binding,proposal,incoming)
        if self.binding.start<=self.clock():raise ValueError('Historical/past agreement is not an invitation trigger')
        ids=self.policy._calendars(self.calendar)
        if tuple(ids)!=AVAILABILITY:raise ValueError('Exact CODE, Fuxam and Acribic required')
        if self.conference_mode=='google_meet':
            capability=self.calendar.calendars().get(calendarId=self.calendar_id).execute()
            if 'hangoutsMeet' not in capability.get('conferenceProperties',{}).get('allowedConferenceSolutionTypes',[]):
                raise ValueError('CODE calendar does not prove Meet capability; no insert')
        self.verified_hold()
        if not self.legacy_hold and self.policy.own_hold_available(self.calendar,'code',self.binding.thread_id,[self.binding.recipient],self.binding.start) is not True:
            raise ValueError('Owned journal claim not verified')
        lower=self.binding.start-timedelta(minutes=15);upper=self.binding.start+timedelta(minutes=self.binding.duration_minutes+15)
        checked=[cid for cid in ids if cid!=self.hold_calendar_id]
        response=self.calendar.freebusy().query(body={'timeMin':lower.isoformat(),'timeMax':upper.isoformat(),
            'timeZone':'Europe/Berlin','items':[{'id':cid} for cid in checked]}).execute().get('calendars',{})
        for cid in checked:
            result=response.get(cid,{})
            if result.get('errors') or not isinstance(result.get('busy'),list):raise ValueError('Required calendar availability incomplete')
            for busy in result['busy']:
                a,b=_dt(busy['start']),_dt(busy['end'])
                if a>=b or (lower<b and upper>a):raise ValueError('Required calendar conflict or invalid interval')
        own_seen=False
        for event in _pages(self.calendar,calendarId=self.hold_calendar_id,timeMin=lower.isoformat(),timeMax=upper.isoformat(),singleEvents=True,maxResults=2500):
            if event.get('id')==self.hold_id:
                own_seen=True
                continue # only after exact own readback
            if event.get('status')=='cancelled' or event.get('transparency')=='transparent':continue
            a,b=_interval(event)
            if lower<b and upper>a:raise ValueError('Another CODE event/hold overlaps agreed meeting')
        if not own_seen:raise ValueError('Primary overlap listing did not include the verified own hold')
        for key,claim in self.policy._read().items():
            if key==self.hold_id or self.policy.verified_release(claim):continue
            a=_dt(claim['start']);b=a+timedelta(minutes=30)
            if lower<b and upper>a:raise ValueError('Another durable pending/foreign claim overlaps')
        return True
    def execute_once(self,identity):
        with self.policy._locked():
            self.preflight(identity)
            self.calendar.events().insert(calendarId=self.calendar_id,body=self.body,sendUpdates='all',
                **({'conferenceDataVersion':1} if self.conference_mode=='google_meet' else {})).execute()
        # Coordinator marker is already durable. Insert exactly once; on timeout
        # Coordinator reads deterministic ID. No meeting-link provider invented.

class HoldReleaseAdapter:
    """Release all exact same-person CODE proposals, with per-event journals.

    `resume_pending` is safe only for this compound cleanup: attempted deletes
    are permanently readback-only, even if the event is still visible.
    """
    def __init__(self,invitation,*,legacy_window=None,hold_source_reader=None):
        self.invitation=invitation;self.lease_guard=lambda:None
        self.legacy_window=legacy_window;self.hold_source_reader=hold_source_reader;self.review_count=0
        if legacy_window is not None:
            a,b=legacy_window
            if a.tzinfo is None or b.tzinfo is None or a>=b or b-a>timedelta(days=366):
                raise ValueError('Explicit bounded timezone-aware legacy booking window required')
    def _discover_legacy(self):
        i=self.invitation;self.review_count=0;self.blocked_legacy=set()
        if self.legacy_window is None:self.review_count=1;return
        a,b=self.legacy_window
        events=_pages(i.calendar,calendarId=CODE,timeMin=a.isoformat(),timeMax=b.isoformat(),singleEvents=True,
                      maxResults=250,privateExtendedProperty='hermesSlotHold=1')
        if len(events)>100:
            self.review_count=len(events);raise ValueError('Legacy hold discovery exceeds source-read bound')
        for event in events:
            props=event.get('extendedProperties',{}).get('private',{})
            if props.get('hermesSlotHold')!='1':raise ValueError('Unfiltered legacy listing')
            if props.get('hermesOwner'):continue # managed journal proves these separately
            if props.get('sourceAccount')!='code':
                if props.get('sourceAccount')!='stealth':self.review_count+=1
                continue # other or unproven account must remain
            mid=props.get('sourceMessage');thread=props.get('sourceThread');key=event.get('id')
            self.blocked_legacy.add(key)
            if not isinstance(mid,str) or not mid or not isinstance(thread,str) or not thread:
                self.review_count+=1;continue
            try:
                source=(self.hold_source_reader(thread,mid) if self.hold_source_reader else
                        i.source_reader()[0] if mid==i.binding.proposal_message_id and thread==i.binding.thread_id else None)
                if source is None:self.review_count+=1;continue
                if (not source.complete or source.account_email!=CODE or source.message_id!=mid or source.thread_id!=thread or
                    source.sender.casefold()!=CODE or not source.rfc_message_id or 'SENT' not in source.labels or 'DRAFT' in source.labels):raise ValueError('Legacy SENT identity not verified')
                external={e.casefold() for e in source.recipients}-{CODE,PIET}
                if external!={i.binding.recipient.lower()}:
                    if i.binding.recipient.lower() in external:self.review_count+=1
                    continue # another/ambiguous recipient, preserve
                start,end=_interval(event)
                expected=hashlib.sha256(('code|'+thread+'|'+start.astimezone(timezone.utc).isoformat()).encode()).hexdigest()[:40]
                if key!=expected:raise ValueError('Legacy UTC slot ID mismatch')
                own=authored_text(source.body)
                date_pattern=rf'(?<!\d)0?{start.astimezone(BERLIN).day}\.\s*0?{start.astimezone(BERLIN).month}\.(?:\s*{start.astimezone(BERLIN).year}\b|(?!\s*\d))'
                clock_pattern=rf'(?<!\d)0?{start.astimezone(BERLIN).hour}:{start.astimezone(BERLIN).minute:02d}(?:\s*Uhr)?\b'
                if not re.search(date_pattern,own) or not re.search(clock_pattern,own):raise ValueError('Exact legacy slot absent from original authored proposal')
                claim={'event_id':key,'start':start.astimezone(timezone.utc).isoformat(),
                       'owner':json.dumps(['code',thread,[i.binding.recipient.lower()]],separators=(',',':')),
                       'kind':'legacy_slot_hold','legacy_source_message':mid,
                       'legacy_source_hash':hashlib.sha256(source.source_text().encode()).hexdigest(),'state':'held_draft_verified'}
                self._verify_hold(event,claim)
                claims=i.policy._read();old=claims.get(key)
                if old and any(old.get(k)!=v for k,v in claim.items() if k!='state'):raise ValueError('Legacy source changed under claim ID')
                if not old:claims[key]=claim;i.policy._save(claims)
                self.blocked_legacy.discard(key)
            except Exception:self.review_count+=1 # preserve unproven legacy event, expose count
    def _verify_hold(self,event,claim):
        if claim.get('kind')!='legacy_slot_hold':return self.invitation.policy._verify(event,claim)
        a,b=_interval(event);props=event.get('extendedProperties',{}).get('private',{})
        owner=json.loads(claim['owner'])
        if (event.get('id')!=claim['event_id'] or a!=_dt(claim['start']) or b-a!=timedelta(minutes=30) or
            event.get('visibility')!='private' or event.get('transparency')!='opaque' or event.get('status')=='cancelled' or
            event.get('attendees') or props.get('hermesSlotHold')!='1' or props.get('sourceAccount')!='code' or
            props.get('sourceThread')!=owner[1] or props.get('sourceMessage')!=claim['legacy_source_message']):
            raise ValueError('Legacy hold live ownership/interval not verified')
    def bind_native_fence(self,guard):self.lease_guard=guard
    def _selected(self):
        rows=[];recipient=self.invitation.binding.recipient.lower()
        for key,claim in self.invitation.policy._read().items():
            if claim.get('kind')=='legacy_slot_hold':
                if self.legacy_window is None or not self.legacy_window[0]<=_dt(claim['start'])<self.legacy_window[1]:continue
                if key in getattr(self,'blocked_legacy',set()):continue
            try:owner=json.loads(claim['owner'])
            except (ValueError,KeyError,TypeError):continue
            if (not isinstance(owner,list) or len(owner)!=3 or owner[0]!='code' or
                not isinstance(owner[1],str) or not owner[1] or owner[2]!=[recipient]):continue
            slot=_dt(claim['start']).astimezone(timezone.utc).isoformat()
            identity_key=('code|'+owner[1]+'|'+slot if claim.get('kind')=='legacy_slot_hold' else claim['owner']+'|'+slot)
            expected=hashlib.sha256(identity_key.encode()).hexdigest()[:40]
            if claim.get('event_id')!=key or key!=expected:raise ValueError('Claim identity malformed')
            rows.append((key,claim))
        if not any(key==self.invitation.hold_id for key,_ in rows):raise ValueError('Accepted hold journal absent')
        if len(rows)>100:raise ValueError('Bounded same-person cleanup exceeded; review')
        return sorted(rows)
    def _proof(self,key):
        i=self.invitation
        result={'attempted':True,'verified':False,'account':CODE,'recipient':i.binding.recipient.lower(),
                'hold_id':key,'invitation_event_id':i.event_id,'source_hash':i.identity.source_hash}
        cid=claim_calendar(i.policy._read().get(key,{}))
        if cid!=CODE:result['calendar_id']=cid
        return result
    def _inspect(self):
        i=self.invitation;pending=[];unknown=False
        for key,claim in self._selected():
            if i.policy.verified_release(claim):continue
            try:event=_get(i.calendar,key,claim_calendar(claim))
            except Exception as error:
                # Only this exact hold GET: 410 is deleted-resource readback,
                # never a list/sync-token response or permission/transport loss.
                if getattr(getattr(error,'resp',None),'status',None)!=410:raise
                event=None
            proof=claim.get('release')
            expected=self._proof(key)
            if proof and any(proof.get(k)!=v for k,v in expected.items() if k!='verified'):
                raise ValueError('Release belongs to another invitation/source')
            deleted = event is None or (isinstance(event,dict) and event.get('id')==key
                and event.get('status')=='cancelled'
                and not any(field in event for field in ('recurringEventId','recurrence','originalStartTime')))
            if deleted:
                # Missing alone is not release permission. A persisted attempted
                # delete plus actual invitation proof must precede publication.
                # Google GET also retains deleted single events as tombstones;
                # recurring exceptions and mismatched IDs are not absence.
                if not proof or proof.get('attempted') is not True:unknown=True;continue
                claims=i.policy._read();claims[key]['release']={**expected,'verified':True}
                claims[key]['state']='released_after_verified_invitation';i.policy._save(claims)
                continue
            self._verify_hold(event,claim) # private 30m, opaque, no attendees, exact owner
            if proof:unknown=True
            else:pending.append(key)
        return pending,unknown
    def readback(self,identity):
        self.invitation._identity(identity)
        with self.invitation.policy._locked():
            if self.invitation.readback(identity).state!='verified':return Readback('unknown',identity)
            try:
                self._discover_legacy()
                pending,unknown=self._inspect()
                unknown=unknown or self.review_count>0
            except (ValueError,KeyError,TypeError):return Readback('conflict',identity)
            except Exception:return Readback('unknown',identity)
            if pending:return Readback('absent',identity)
            return Readback('unknown' if unknown else 'verified',identity,
                            None if unknown else self.invitation.event_id+':holds-released')
    def preflight(self,identity):
        if self.readback(identity).state!='absent':raise ValueError('Verified invitation and exact unattempted proposal holds required')
    def _release_pending(self,identity):
        i=self.invitation;i._identity(identity)
        with i.policy._locked():
            if i.readback(identity).state!='verified':raise ValueError('Actual invitation not verified')
            self._discover_legacy()
            pending,_=self._inspect()
            if not pending:raise ValueError('No unattempted exact owned holds remain')
            for key in pending:
                self.lease_guard() # native owner is fenced again before every delete
                if i.readback(identity).state!='verified':raise ValueError('Invitation changed before hold delete')
                claims=i.policy._read();claim=claims[key]
                if claim.get('kind')=='legacy_slot_hold':
                    self._discover_legacy()
                    if key in self.blocked_legacy:continue
                if claim.get('release'):continue
                cid=claim_calendar(claim)
                event=_get(i.calendar,key,cid)
                if event is None:continue # missing without attempt is review, never published
                self._verify_hold(event,claim)
                claims[key]['release']=self._proof(key);i.policy._save(claims) # BEFORE each delete
                i.calendar.events().delete(calendarId=cid,eventId=key,sendUpdates='none').execute()
                # Exact readback and actual invitation proof precede released state.
                if self.readback(identity).state=='conflict':raise ValueError('Cleanup readback conflict')
    def execute_once(self,identity):
        self.preflight(identity);self._release_pending(identity)
    def resume_pending(self,identity):
        self._release_pending(identity)

class GmailAcceptanceReader:
    """Fresh complete Gmail thread; the accepted incoming must still be latest.

    Reads injected existing Gmail service under verified CODE profile. No compact
    realtime summaries, no sender-order guesses, no model and no credential loading.
    """
    def __init__(self,w,gmail,*,thread_id,proposal_id,incoming_id):
        self.w,self.gmail,self.thread,self.proposal,self.incoming=w,gmail,thread_id,proposal_id,incoming_id
    def __call__(self):
        from email.utils import parseaddr,getaddresses
        from gtm.calendar.confirmation import MailEvidence
        if self.gmail.users().getProfile(userId='me').execute().get('emailAddress')!=CODE:
            raise ValueError('Authenticated CODE Gmail profile required')
        raw=self.gmail.users().threads().get(userId='me',id=self.thread,format='full').execute()
        messages=raw.get('messages')
        if raw.get('id')!=self.thread or not isinstance(messages,list) or not messages or len(messages)>200:
            raise ValueError('Complete bounded thread required; no truncation')
        relevant=[m for m in messages if 'DRAFT' not in m.get('labelIds',[])]
        if any(not str(m.get('internalDate','')).isdigit() for m in relevant):raise ValueError('Thread chronology incomplete')
        ordered=sorted(relevant,key=lambda m:int(m['internalDate']))
        if not ordered or ordered[-1].get('id')!=self.incoming:raise ValueError('Acceptance superseded; source needs review')
        if len(ordered)>1 and ordered[-1]['internalDate']==ordered[-2]['internalDate']:
            raise ValueError('Thread chronology ambiguous')
        selected={}
        for msg in messages:
            if msg.get('id') not in (self.proposal,self.incoming):continue
            h=self.w.headers(msg);body=self.w.body(msg.get('payload',{}))
            if msg.get('threadId')!=self.thread or not isinstance(body,str) or not body.strip() or len(body)>200000:
                raise ValueError('Full original source required')
            recipients=tuple(a.casefold() for _,a in getaddresses([h.get('to',''),h.get('cc','')]) if a)
            selected[msg['id']]=MailEvidence(CODE,msg['id'],self.thread,parseaddr(h.get('from',''))[1],
                recipients,body,tuple(msg.get('labelIds',[])),h.get('message-id',''),h.get('in-reply-to',''),True)
        if set(selected)!={self.proposal,self.incoming}:raise ValueError('Exact original proposal/acceptance missing')
        return selected[self.proposal],selected[self.incoming]


class GmailHoldSourceReader:
    """Current exact CODE profile and complete original SENT source for legacy holds."""
    def __init__(self,w,gmail):self.w,self.gmail=w,gmail
    def __call__(self,thread_id,message_id):
        from email.utils import parseaddr,getaddresses
        from gtm.calendar.confirmation import MailEvidence
        if self.gmail.users().getProfile(userId='me').execute().get('emailAddress')!=CODE:raise ValueError('Legacy source account mismatch')
        msg=self.gmail.users().messages().get(userId='me',id=message_id,format='full').execute()
        h=self.w.headers(msg);body=self.w.body(msg.get('payload',{}));labels=tuple(msg.get('labelIds',[]))
        if (msg.get('id')!=message_id or msg.get('threadId')!=thread_id or 'SENT' not in labels or 'DRAFT' in labels or
            parseaddr(h.get('from',''))[1].casefold()!=CODE or not h.get('message-id') or
            not isinstance(body,str) or not body.strip() or len(body)>200000):raise ValueError('Exact complete CODE SENT legacy source required')
        recipients=tuple(a.casefold() for _,a in getaddresses([h.get('to',''),h.get('cc','')]) if a)
        return MailEvidence(CODE,message_id,thread_id,CODE,recipients,body,labels,h['message-id'],h.get('in-reply-to',''),True)
