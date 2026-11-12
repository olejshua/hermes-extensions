"""The explicit 22 October first-contact approval supersedes older Sent text."""
import re
from pathlib import Path
from background_jobs.contracts import ContractError, canonical, digest

SOURCE_ID='acribic-first-contact-approved-20261022'
SOURCE_PINS={
 'Acribic/AGENTS.md':'0586164a53d2922f57bf2673569664328f2e4d438fa67a1bd359353d98d88def',
 'Skills/acribic-versand/SKILL.md':'96a6143ef3ec41c61bde28a615f0f8381a22f4787d73fad3f8ca113394f388b5',
 'Skills/acribic-versand/references/ablauf.md':'19a52b70b100c4a7633b5a0cc347761dcf04be407cdb9b7acb1912ad18a3a10e',
 'Skills/acribic-versand/references/vorlage-und-ted.md':'3f1e3717d34386de031834e78c954064adf4bfba1ede774a1c87d5c569f40a6b',
 'Skills/acribic-versand/references/fehler-und-wiederaufnahme.md':'89b5c172eb22fa19c44ca7cf7b950821ac12efed97af9f249c70d63c68eca199',
 'Skills/acribic-versand/references/systemadapter.md':'86066ec8a452b264a614d771d119a2a46779d8248fbc5dc3e90a69ab03fe016d',
 'Acribic/Leads/tools/versand.py':'68a0bcb311a2e56998601d2fe12f02ba9fd29c0e8cfb60f334b2a18e50828a43'}
SELECTION=('Prioritize software, IT and Health-IT firms with their own tender submission and evidenced tender pressure. '
 'Use actual company evidence such as bid/tender roles, relevant jobs or explicit regular tender business. '
 'Engineering/planning firms remain allowed, with lower priority unless their own bidder need is evidenced. '
 'TED awards alone prove neither need nor a majority of public revenue. Do not invent a public revenue share. '
 'Use only existing permitted CRM contacts; prefer a proven tender/bid owner.')


def render(first_name,firm,buyer):
    for value in (firm,buyer):
        if not isinstance(value,str)or not value.strip()or any(c in value for c in '\r\n{}'):raise ContractError('approved_template_variables_required')
    if first_name is not None and (not isinstance(first_name,str)or not re.fullmatch(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß' -]{1,60}",first_name)):
        raise ContractError('approved_personal_name_required')
    du=bool(first_name);greeting=f'Hallo {first_name},'if du else 'Guten Tag,'
    return (f'{greeting}\n\n'
        'wir sind ein entstehendes Software-Unternehmen an der CODE University in Berlin. Wir entwickeln Software für Unternehmen, die Angebote auf öffentliche Ausschreibungen einreichen.\n\n'
        f"Auf {firm} sind wir über die EU-Vergabedatenbank TED gestoßen, in der {'ihr'if du else 'Sie'} regelmäßig als Zuschlagsempfänger {'auftaucht'if du else 'auftauchen'} (u. a. {buyer}).\n\n"
        f"Für unsere Entwicklung möchten wir verstehen, wie {'ihr'if du else 'Sie'} Ausschreibungsunterlagen {'prüft'if du else 'prüfen'} und Angebote im Team {'vorbereitet'if du else 'vorbereiten'}: Welche Schritte kosten besonders viel Zeit, und wo helfen {'eure'if du else 'Ihre'} bisherigen Werkzeuge noch nicht ausreichend?\n\n"
        f"{'Wärst du'if du else 'Wären Sie'} oder die dafür zuständige Person in {'eurem'if du else 'Ihrem'} Team für einen 10-15-minütigen Austausch offen?\n\n"
        f"Falls das bei {'euch'if du else 'Ihnen'} nicht passt, genügt eine kurze Rückmeldung - dann melden wir uns nicht wieder.\n\n"
        'Vielen Dank im Voraus!\n\nMit besten Grüßen\nOle Beispiel')


def document():
    # Template marker values are literals for the source document only.
    neutral=render(None,'FIRMAMARKER','BUYERMARKER').replace('FIRMAMARKER','{FIRMA}').replace('BUYERMARKER','{AUFTRAGGEBER}')
    personal=render('NAMEMARKER','FIRMAMARKER','BUYERMARKER').replace('NAMEMARKER','{VORNAME}').replace('FIRMAMARKER','{FIRMA}').replace('BUYERMARKER','{AUFTRAGGEBER}')
    value={'contract':'acribic-explicit-approved-template-v1','approved_on':'2026-10-22','source_pins':SOURCE_PINS,
        'precedence':'Explicit current user approval overrides all older Sent templates; first contact only.',
        'subject':'{AUFTRAGGEBER}','neutral':neutral,'personal':personal,'selection':SELECTION}
    text=canonical(value)
    return {'source_id':SOURCE_ID,'kind':'approved_template','content':text,'sha256':digest(text.encode())}


def variables(plan):
    if plan.get('proposed_slots')or plan.get('html_body')is not None:raise ContractError('approved_first_contact_fixed_format_required')
    for name in (None,'NAMEMARKER'):
        pattern=re.escape(render(name,'FIRMAMARKER','BUYERMARKER'))
        pattern=pattern.replace('FIRMAMARKER','(?P<firm>[^\\n{}]+)').replace('BUYERMARKER','(?P<buyer>[^\\n{}]+)')
        if name:pattern=pattern.replace('NAMEMARKER','(?P<first_name>[^\\n{},]+)')
        match=re.fullmatch(pattern,plan.get('body',''))
        if match:
            values=match.groupdict();values.setdefault('first_name',None)
            if plan.get('subject')!=values['buyer']or len(values['buyer'])>70:raise ContractError('approved_template_subject_binding_required')
            return values
    raise ContractError('explicit_approved_template_changed')


def checked(plan,source):
    if source!=document()or plan.get('template_source_id')!=SOURCE_ID or plan.get('template_sha256')!=source['sha256']:
        raise ContractError('explicit_approved_template_source_required')
    return variables(plan)


class TemplateSource:
    """Verify the actual reviewed originals, not just this module's constants."""
    def __init__(self,vault_root,state_root):self.vault,self.root=Path(vault_root),Path(state_root)
    def capture(self,binding,active):
        from .protections import _read
        from .sources import persist
        from datetime import datetime,timezone
        documents=[];originals={}
        for name,expected in SOURCE_PINS.items():
            active();raw,stat=_read(self.vault/name);active()
            if digest(raw)!=expected:raise ContractError('approved_template_authority_changed')
            original={'path':name,'sha256':expected,'stat':stat,'text':raw.decode('utf-8')}
            originals[name]=original
            documents.append(persist(self.root,'template_authority','template-original:'+name,
                                     original,binding,'authoritative-approved-template.read'))
        # Re-read after the set: approval cannot silently mix source versions.
        for name,original in originals.items():
            raw,stat=_read(self.vault/name)
            if digest(raw)!=original['sha256']or stat!=original['stat']:
                raise ContractError('approved_template_authority_changed')
        packet={'contract':'acribic-current-template-authority-v1','captured_at':datetime.now(timezone.utc).isoformat(),
                'source_pins':dict(SOURCE_PINS),'document':document(),'originals':originals}
        proof=persist(self.root,'template_authority','approved-template-authority',packet,binding,
                      'authoritative-approved-template.complete')
        return documents+[proof],document()
