"""Fixed public-page collection through Hermes' installed native browser tools.

No model-written sources, injected JavaScript, accounts, cloud browser or PM setup.
DOM snapshots remain complete receipts; parsing never claims a provider API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
from urllib.parse import urlencode, urlparse, parse_qs
from uuid import uuid4
from zoneinfo import ZoneInfo

from .contracts import Station, JourneyResult, ProviderUnavailable, TravelError, instant, now
from .capture import SCHEMA, full_hash
from .transport import parse_journey
from .web import WebObservation, WebPage, official_url, validate_observation

ENTRIES = {'vbb': 'https://www.bvg.de/de/verbindungen/fahrplanauskunft', 'db': 'https://www.bahn.de/'}
PRODUCTS = {'suburban':'sbahn', 'subway':'subway', 'bus':'bus', 'tram':'tram', 'ferry':'ferry',
            'express':'longDistanceTraffic', 'regional':'regionalTraffic'}
DOM_LIMIT = 1_800_000

# These expressions are source code, not tool/model arguments. Public DOM only.
OPTIONS_JS = r'''({url:location.href,options:Array.from(document.querySelectorAll('[role="option"]')).map(e=>({
  raw_id:e.getAttribute('data-key')||e.getAttribute('data-value'),text:e.textContent,html:e.outerHTML
})).filter(e=>e.raw_id&&e.raw_id.startsWith('A=1@'))})'''

BVG_JS = r'''(()=>{
 const text=e=>e?e.textContent.trim():null;
 const clean=e=>{if(!e)return null;const c=e.cloneNode(true);c.querySelectorAll('.sr-only').forEach(n=>n.remove());return c.textContent.trim()};
 const times=e=>e?{text:e.textContent.trim(),html:e.outerHTML,times:Array.from(e.querySelectorAll('time')).map(t=>({value:t.getAttribute('datetime'),text:t.textContent,classes:t.className}))}:null;
 const nodes=Array.from(document.querySelectorAll('[class*="ConnectionSearchResultItem_ConnectionSearchResultItem"]'));
 const rows=nodes.map(n=>{
  let d=n.previousElementSibling;while(d&&!d.className.includes('newDay'))d=d.previousElementSibling;
  return {id:n.getAttribute('data-id'),day:text(d),html:n.outerHTML,text:n.textContent.trim(),
   legs:Array.from(n.querySelectorAll('[data-legtype]')).map(l=>({
    type:l.getAttribute('data-legtype'),html:l.outerHTML,text:l.textContent.trim(),
    origin:clean(l.querySelector('[class*="ConnectionLeg_depStop"]')),
    destination:clean(l.querySelector('[class*="ConnectionLeg_arrStop"]')),
    departure:times(l.querySelector('[class*="ConnectionLeg_depTime"]')),
    arrival:times(l.querySelector('[class*="ConnectionLeg_arrTime"]')),
    departure_platform:clean(l.querySelector('[class*="ConnectionLeg_depTrack"]')),
    arrival_platform:clean(l.querySelector('[class*="ConnectionLeg_arrTrack"]')),
    vehicle:l.querySelector('[class*="infoRide"] [title]')?.getAttribute('title')||null,
    messages:Array.from(l.querySelectorAll('[class*="Message"],[role="alert"]')).map(text).filter(Boolean)
   }))}
 });
 return {url:location.href,title:document.title,text:document.querySelector('main')?.innerText,
  summary:Array.from(document.querySelectorAll('output dd')).map(text),rows,
  controls:Array.from(document.querySelectorAll('main button')).filter(e=>/^(Spätere|Frühere) Abfahrten$/.test(e.textContent.trim())).map(e=>({label:e.textContent.trim(),html:e.outerHTML,disabled:e.disabled}))};
})()'''

DB_JS = r'''(()=>{
 const tx=e=>e?e.textContent.trim():null;
 const tm=e=>e?{html:e.outerHTML,planned:tx(e.querySelector('time[datetime]')),actual:tx(e.querySelector('time[aria-description="Echtzeitinformation"]'))}:null;
 return {url:location.href,title:document.title,text:document.querySelector('main')?.innerText,
  date:tx(document.getElementById('SearchZeitauswahlButton')),
  rows:Array.from(document.querySelectorAll('li.verbindung-list__result-item')).map((n,i)=>({
   id:String(i),html:n.outerHTML,text:n.textContent.trim(),
   origin:tx(n.querySelector('.test-reise-beschreibung-start-value')),
   destination:tx(n.querySelector('.test-reise-beschreibung-ziel-value')),
   departure:tm(n.querySelector('.reiseplan__uebersicht-uhrzeit-von')),
   arrival:tm(n.querySelector('.reiseplan__uebersicht-uhrzeit-nach')),
   vehicles:Array.from(n.querySelectorAll('.test-verbindungsabschnittVisualisierung-verkehrsmittelText')).map(tx),
   changes:tx(n.querySelector('[class*="dauer-umstieg__umstiege"]')),
   messages:Array.from(n.querySelectorAll('.reiseplan__uebersicht-hinweise, .AuslastungsAnzeige, .test-reservierungsplicht')).map(tx).filter(Boolean),
   unresolved_messages:n.textContent.includes('Es liegen Meldungen vor.')
  })),
  controls:Array.from(document.querySelectorAll('main button')).filter(e=>/^(Spätere|Frühere) Verbindungen$/.test(e.textContent.trim())).map(e=>({label:e.textContent.trim(),html:e.outerHTML,disabled:e.disabled}))};
})()'''


class OfficialReadError(TravelError):
    def __init__(self, reason, *, candidates=()):
        self.reason = reason
        self.candidates = tuple(candidates)
        super().__init__(reason)


def _json_safe(value):
    data=json.dumps(value,ensure_ascii=False,allow_nan=False)
    if len(data.encode())>DOM_LIMIT:
        raise OfficialReadError('official_complete_source_budget_exceeded')
    return data


def _local_date(value):
    # A date must be observed in the provider page, not inferred from request.
    if not isinstance(value,str):raise OfficialReadError('official_result_date_missing')
    found=re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})',value)
    if found:
        day,month,year=map(int,found.groups());return datetime(year,month,day).date()
    months={'Jan':1,'Feb':2,'Mär':3,'Apr':4,'Mai':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Okt':10,'Nov':11,'Dez':12}
    found=re.search(r'(\d{1,2})\.\s*([A-Za-zÄäöü]+)\.?\s*(\d{4})',value)
    if found and found[2][:3] in months:
        return datetime(int(found[3]),months[found[2][:3]],int(found[1])).date()
    raise OfficialReadError('official_result_date_unrecognized')


def _clock(day,value,*,after=None):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?',value.strip()):
        raise OfficialReadError('official_result_time_unrecognized')
    value=value.strip()
    t=datetime.strptime(value,'%H:%M:%S' if len(value)>5 else '%H:%M').time()
    local=datetime.combine(day,t,tzinfo=ZoneInfo('Europe/Berlin'))
    if after is not None and local<after:
        local+=timedelta(days=1)
    # Resolve the displayed overnight date before checking its possible offsets.
    # A plain 02:xx on the DST rollback day has two meanings; never guess fold.
    if local.replace(fold=0).utcoffset()!=local.replace(fold=1).utcoffset():
        raise OfficialReadError('official_result_dst_offset_ambiguous')
    return local


def _vehicle(title):
    if not isinstance(title,str) or not title.strip():raise OfficialReadError('official_vehicle_missing')
    for prefix,product in [('S-Bahn ','suburban'),('U-Bahn ','subway'),('Bus ','bus'),('Tram ','tram'),
                           ('Fähre ','ferry'),('Regionalbahn ','regional'),('Regional-Express ','regional')]:
        if title.startswith(prefix):return {'name':title[len(prefix):],'product':product}
    if re.match(r'^(ICE|IC|EC|RJ|RJX|FLX|TGV|EN|NJ)\s',title):return {'name':title,'product':'express'}
    if re.match(r'^(RE|RB|FEX|IRE)\s?\d',title):return {'name':title,'product':'regional'}
    raise OfficialReadError('official_vehicle_unrecognized')


def _warning(values):
    return [{'type':'provider_warning','text':v} for v in dict.fromkeys(values) if isinstance(v,str) and v.strip()]


def bvg_journey(row,query):
    day=_local_date(row['day']);legs=[];previous=None
    for item in row['legs']:
        if item['type'] not in {'ride','walk','transfer'}:raise OfficialReadError('official_leg_type_unrecognized')
        walking=item['type']!='ride'
        if not item.get('departure') or not item.get('arrival'):
            raise OfficialReadError('official_complete_leg_times_missing')
        def clock(packet,after):
            times=packet.get('times',[])
            if len(times)!=1:raise OfficialReadError('official_bvg_realtime_markup_unrecognized')
            # Multiple/annotated clocks must not erase a known delay.
            t=times[0]
            if packet['text'].strip()!=t['text'].strip():
                raise OfficialReadError('official_bvg_realtime_markup_unrecognized')
            return _clock(day,t.get('value') or t['text'],after=after)
        dep=clock(item['departure'],previous);arr=clock(item['arrival'],dep);previous=arr
        if not item.get('origin') or not item.get('destination'):raise OfficialReadError('official_leg_stop_missing')
        text=item.get('text','')
        legs.append({'origin':{'name':item['origin']},'destination':{'name':item['destination']},
            'plannedDeparture':dep.isoformat(),'plannedArrival':arr.isoformat(),
            'line':None if walking else _vehicle(item.get('vehicle')),'walking':walking,
            'cancelled':True if re.search(r'(fällt aus|entfällt|Ausfall)',text,re.I) else None,
            'departurePlatform':item.get('departure_platform'),'arrivalPlatform':item.get('arrival_platform'),
            'remarks':_warning(item.get('messages',[]))})
    if not legs:raise OfficialReadError('official_complete_legs_missing')
    for key,station,leg in [('origin',query.origin,legs[0]),('destination',query.destination,legs[-1])]:
        if leg[key]['name']!=station.name:raise OfficialReadError('official_route_endpoint_mismatch')
        leg[key]['id']=station.id
    return {'legs':legs}


def db_journey(row,query,source_day):
    if row.get('origin')!=query.origin.name or row.get('destination')!=query.destination.name:
        raise OfficialReadError('official_route_endpoint_mismatch')
    if len(row.get('vehicles',[]))!=1 or row.get('changes'):
        raise OfficialReadError('official_db_transfer_details_not_supported')
    if row.get('unresolved_messages'):
        raise OfficialReadError('official_db_warning_details_unread')
    day=_local_date(source_day)
    dep=row['departure'];arr=row['arrival']
    planned_dep=_clock(day,dep['planned']);planned_arr=_clock(day,arr['planned'],after=planned_dep)
    actual_dep=_clock(planned_dep.date(),dep['actual']) if dep.get('actual') else None
    actual_arr=_clock(planned_arr.date(),arr['actual']) if arr.get('actual') else None
    # A >12 h clock delta is ambiguous across midnight, not an invented +24 h delay.
    if any(a is not None and abs((a-p).total_seconds())>43200 for a,p in [(actual_dep,planned_dep),(actual_arr,planned_arr)]):
        raise OfficialReadError('official_db_realtime_date_ambiguous')
    leg={'origin':{'id':query.origin.id,'name':query.origin.name},
         'destination':{'id':query.destination.id,'name':query.destination.name},
         'plannedDeparture':planned_dep.isoformat(),'plannedArrival':planned_arr.isoformat(),
         'departure':actual_dep.isoformat() if actual_dep else None,'arrival':actual_arr.isoformat() if actual_arr else None,
         'departureDelay':int((actual_dep-planned_dep).total_seconds()) if actual_dep else None,
         'arrivalDelay':int((actual_arr-planned_arr).total_seconds()) if actual_arr else None,
         'line':_vehicle(row['vehicles'][0]),'walking':False,
         'cancelled':True if re.search(r'(Verbindung fällt aus|Zug fällt aus|Halt entfällt)',row['text'],re.I) else None,
         'remarks':_warning(row.get('messages',[]))}
    return {'legs':[leg]}


class NativeBrowser:
    """An owned native task session; it never changes the user's browser policy."""
    def __init__(self,provider,*,on_source=None):
        self.provider=provider;self.task='ole-travel-'+uuid4().hex
        self.on_source=on_source;self.actions=[];self.opened=False
        self.started=time.monotonic()

    @staticmethod
    def readiness():
        try:
            import pm
            from tools import browser_tool as bt
            from tools import browser_tool_cloud as cloud
            from tools import browser_tool_cdp as cdp
            from tools import browser_tool_install as install
            if cloud._is_headed_mode() or cloud._use_real_profile() or cdp._get_cdp_override_raw():
                return False,'travel_requires_anonymous_local_headless_browser'
            if bt._is_camofox_mode() or bt._is_browser_use_cli_mode() or not cloud._is_local_mode():
                return False,'travel_requires_existing_local_chromium_backend'
            # Presence only. Never call ensure()/requirements discovery or install.
            if not pm.installed_package('agent-browser') or not install._chromium_installed():
                return False,'existing_native_browser_runtime_missing'
            return True,None
        except (ImportError,ValueError,OSError,RuntimeError):
            return False,'existing_native_browser_runtime_unavailable'

    def check(self):
        from tools.interrupt import is_interrupted
        if is_interrupted():raise OfficialReadError('travel_cancelled')
        if time.monotonic()-self.started>180:
            raise OfficialReadError('official_read_deadline_exceeded')

    def packet(self,value):
        self.check();data=json.loads(value)
        if not isinstance(data,dict) or not data.get('success'):
            error=data.get('error','') if isinstance(data,dict) else ''
            if isinstance(error,str) and error.startswith('Blocked: browser_console'):
                raise OfficialReadError('native_dom_read_blocked_by_browser_policy')
            if isinstance(error,str) and 'SyntaxError' in error:
                raise OfficialReadError('official_collector_expression_error')
            raise OfficialReadError('native_browser_action_unavailable')
        return data

    def navigate(self,url):
        ok,reason=self.readiness()
        if not ok:raise OfficialReadError(reason)
        official_url(self.provider,url)
        from tools.browser_tool import browser_navigate
        self.check();self.opened=True
        data=self.packet(browser_navigate(url,task_id=self.task))
        self.actions.append({'action':'navigate','url':url,'completed':True,'observed_at':now()})
        if self.on_source:
            self.on_source('official_native_navigation',{'source_url':url,'observed_at':now(),'native_response':data})
        # Server-rendered rows can precede the public form's React listeners.
        # Allow hydration before treating an available button as interactive.
        time.sleep(1)
        self.check()
        return data

    def evaluate(self,expression):
        from tools.browser_tool import browser_console
        self.check()
        data=self.packet(browser_console(expression=expression,task_id=self.task))['result']
        _json_safe(data)
        return data

    def snapshot(self):
        from tools.browser_tool import browser_snapshot
        return self.packet(browser_snapshot(task_id=self.task))['snapshot']

    def type_origin(self,value):
        from tools.browser_tool import browser_type
        # Fresh actual accessibility ref. IDs are never model/tool arguments.
        for attempt in range(6):
            snap=self.snapshot()
            if re.search(r'(Fehler-Code:\s*751|Verhalten Ihres Browsers[^\n]*Bots)',snap,re.I):
                if self.on_source:self.on_source('official_access_failure_snapshot',{'snapshot':snap,'observed_at':now()})
                raise OfficialReadError('official_provider_bot_protection')
            refs=re.findall(r'^\s*- combobox "Start"[^\n]*ref=(e\d+)',snap,re.M)
            if len(refs)==1:break
            self.check();time.sleep(.5)
        else:raise OfficialReadError('official_station_form_unavailable')
        self.packet(browser_type(refs[0],value,task_id=self.task))
        self.actions.append({'action':'type_public_station_query','completed':True,'observed_at':now()})

    def button_ref(self,label):
        refs=re.findall(r'^\s*- button "'+re.escape(label)+r'"[^\n]*ref=(e\d+)',self.snapshot(),re.M)
        if len(refs)!=1:raise OfficialReadError('official_continuation_unavailable')
        return refs[0]

    def click_label(self,label,ref):
        from tools.browser_tool import browser_click
        self.check()
        self.packet(browser_click(ref,task_id=self.task))
        self.actions.append({'action':'click_public_continuation','label':label,'target_ref':ref,'completed':True,'observed_at':now()})

    def read(self,script):
        data=self.evaluate(script)
        url=data.get('url','');official_url(self.provider,url)
        page_text=data.get('text') or ''
        if re.search(r'(Verhalten Ihres Browsers.*Bots|behaviour resembles.*bot|Fehler-Code:\s*751|captcha)',page_text,re.I|re.S):
            if self.on_source:self.on_source('official_access_failure_dom',data)
            raise OfficialReadError('official_provider_bot_protection')
        if re.search(r'(Bitte melden Sie sich an|Login erforderlich|Zugriff verweigert)',page_text,re.I):
            raise OfficialReadError('official_provider_login_or_access_required')
        return data

    def close(self):
        if self.opened:
            from tools.browser_tool_lifecycle import cleanup_browser
            cleanup_browser(self.task)
            self.opened=False


class OfficialReader:
    def __init__(self,*,on_source=None):self.on_source=on_source

    def _receipt(self,kind,data):
        if self.on_source:self.on_source(kind,_json_safe(data),now())

    def _options(self,browser,query):
        before=browser.evaluate(OPTIONS_JS)
        before_hash=hashlib.sha256(_json_safe(before.get('options',[])).encode()).hexdigest()
        browser.type_origin(query)
        for attempt in range(4):
            browser.check();time.sleep(.4)
            data=browser.evaluate(OPTIONS_JS)
            official_url(browser.provider,data['url'])
            current_hash=hashlib.sha256(_json_safe(data.get('options',[])).encode()).hexdigest()
            if data.get('options') and current_hash!=before_hash:break
        else:
            raise OfficialReadError('official_station_suggestions_not_refreshed')
        self._receipt('official_station_dom',data)
        options=[]
        for item in data.get('options',[]):
            raw=item['raw_id'];match=re.search(r'@L=(\d{1,16})@',raw);name=re.search(r'@O=([^@]+)@',raw)
            if match and name:
                options.append({'id':match[1],'name':name[1],'raw_id':raw})
        return options

    def stations(self,provider,query):
        browser=NativeBrowser(provider,on_source=self._receipt)
        try:
            browser.navigate(ENTRIES[provider])
            values=self._options(browser,query)
            return tuple(Station(v['id'],v['name']).checked() for v in values)
        finally:browser.close()

    def __call__(self,request):
        q=request.query;browser=NativeBrowser(q.provider,on_source=self._receipt)
        try:
            browser.navigate(ENTRIES[q.provider])
            selected=[]
            for station in (q.origin,q.destination):
                options=self._options(browser,station.name)
                matching=[v for v in options if v['id']==station.id and v['name']==station.name]
                if len(matching)!=1:
                    raise OfficialReadError('official_station_identity_unconfirmed',
                        candidates=[{'id':v['id'],'name':v['name']} for v in options])
                selected.append(matching[0])
            at=instant(q.window_end if q.window_end and q.time_mode=='arrival' else q.at).astimezone(ZoneInfo('Europe/Berlin'))
            if q.provider=='vbb':
                params={**{v:'on' for v in PRODUCTS.values()},'origin':q.origin.name,'destination':q.destination.name,
                    'originName':q.origin.name,'destinationName':q.destination.name,
                    'originId':selected[0]['raw_id'],'destinationId':selected[1]['raw_id'],
                    'timeSel':'arrive' if q.time_mode=='arrival' else 'depart','date':at.strftime('%Y-%m-%d'),'time':at.strftime('%H:%M')}
                if q.products:
                    params={k:v for k,v in params.items() if k not in PRODUCTS.values()}
                    params.update({PRODUCTS[p]:'on' for p in q.products})
                url=ENTRIES['vbb']+'?'+urlencode(params)
            else:
                if q.products:raise OfficialReadError('official_db_vehicle_filter_not_yet_verified')
                params={'sts':'true','so':q.origin.name,'zo':q.destination.name,
                    'soid':selected[0]['raw_id'],'zoid':selected[1]['raw_id'],'sot':'ST','zot':'ST',
                    'soei':q.origin.id,'zoei':q.destination.id,'hd':at.replace(tzinfo=None).isoformat(),
                    'hza':'A' if q.time_mode=='arrival' else 'D','hz':'[]','ar':'false','s':'false','d':'false',
                    'vm':'00,01,02,03,04,05,06,07,08,09','fm':'false','bp':'false','dlt':'false','nfv':'false','dltv':'false'}
                url='https://www.bahn.de/buchung/fahrplan/suche#'+urlencode(params)
            browser.navigate(url)
            pages=[];incoming=None;previous_hash=None;reason=None;seen=set()
            for index in range(q.max_pages):
                script=BVG_JS if q.provider=='vbb' else DB_JS
                for attempt in range(10):
                    browser.check();time.sleep(.4)
                    source=browser.read(script)
                    if source.get('rows'):
                        current_signature=hashlib.sha256(_json_safe(source['rows']).encode()).hexdigest()
                        if current_signature not in seen:break
                    elif re.search(r'(keine Ergebnisse|Keine Verbindungen gefunden)',source.get('text') or '',re.I):break
                else:
                    if pages:
                        reason='Official provider did not advance after its continuation action'
                        self._receipt('official_continuation_stall',{'source':source,'native_actions':browser.actions})
                        break
                    raise OfficialReadError('official_result_not_loaded_or_layout_changed')
                signature=hashlib.sha256(_json_safe(source['rows']).encode()).hexdigest()
                if signature in seen:
                    reason='Official provider repeated a result page';break
                seen.add(signature)
                stamp=now();records=[];rows=[]
                for i,row in enumerate(source['rows']):
                    try:
                        raw=bvg_journey(row,q) if q.provider=='vbb' else db_journey(row,q,source.get('date'))
                        parse_journey(raw)
                    except (OfficialReadError,TravelError,KeyError,TypeError,ValueError) as exc:
                        records.append({'id':f'page{index}-unparsed{i}','kind':'unparsed_journey',
                            'data':{'reason':getattr(exc,'reason','official_row_schema_unrecognized')},'original_dom':row})
                        reason='Some official journey details could not be verified'
                        continue
                    rows.append(raw)
                    records.append({'id':f'page{index}-journey{i}','kind':'journey','data':raw,'original_dom':row})
                direction='earlier' if q.time_mode=='arrival' else 'later'
                label=('Frühere Abfahrten' if direction=='earlier' else 'Spätere Abfahrten') if q.provider=='vbb' else ('Frühere Verbindungen' if direction=='earlier' else 'Spätere Verbindungen')
                matches=[c for c in source.get('controls',[]) if c['label']==label and not c['disabled']]
                control=None
                if len(matches)==1:
                    control={'control_id':f'page{index}-continuation','target_ref':browser.button_ref(label),
                        'label':label,'direction':direction}
                    records.append({'id':control['control_id'],'kind':'control','data':control,'original_dom':matches[0]})
                frame={'schema':SCHEMA,'provider':q.provider,'source_url':source['url'],'observed_at':stamp,
                    'request_hash':request.request_hash,'records_complete':True,'records':records,
                    'source_time_label':None,'navigation':{'index':index,'incoming':incoming,'outgoing':control},
                    'collector':'ole-travel-native-dom-v1','original_page':source,'native_actions':list(browser.actions)}
                body=_json_safe(frame);page=WebPage(source['url'],stamp,body,tuple(rows))
                pages.append(page)
                self._receipt('official_browser_capture',frame)
                if q.window_end is None:break
                parsed=[parse_journey(raw) for raw in rows]
                boundary=instant(q.at if direction=='earlier' else q.window_end)
                clocks=[instant(j.arrival if direction=='earlier' else j.departure) for j in parsed]
                if clocks and (min(clocks)<=boundary if direction=='earlier' else max(clocks)>=boundary):break
                if control is None:
                    reason='Official provider has no verified continuation before window boundary';break
                previous_hash=full_hash(body)
                browser.click_label(label,control['target_ref'])
                incoming={'previous_frame_sha256':previous_hash,'action':control,'completed':True}
            else:reason='Page budget reached before official window boundary'
            if not pages:raise OfficialReadError('official_no_verified_result_page')
            if all(not p.raw_journeys for p in pages) and reason:
                raise OfficialReadError('official_result_rows_could_not_be_verified')
            return WebObservation(request.request_hash,q.origin.id,q.destination.id,tuple(pages),q.at,q.time_mode,
                q.window_end,q.max_transfers,reason)
        finally:browser.close()
