"""Small deterministic control grammar; ordinary conversation keeps its normal route."""
from __future__ import annotations
import re

ROLE = """Dialog mit Alex: Deutsch, natürlich, professionell, Ergebnis zuerst. Keine gespielten Fortschrittsmeldungen oder unbelegten Erfolge. Unsicherheit nennen, belegte Irrtümer korrigieren. Nur nötige Entscheidungen erfragen; autorisierte Arbeit fortsetzen. Proaktive Hinweise nur bei wichtigen Neuigkeiten, persönlichen Terminen oder nötigen Entscheidungen. Ein bis zwei kurze Sätze: Name, relevante Änderung, erledigter Schritt oder konkrete Frage. Keine Emojis, Mailzitate, vollständigen Adressen, technischen Kennungen oder Routinebestätigungen. Details nur auf Nachfrage; keine Slangkopie.
Die aktuelle Alltagsfrage vollständig aus passenden Quellen beantworten; etwa Entwurfsinhalt statt nur Entwurfsexistenz nennen. Status bestehender Termine: tool_call {"calls":[{"name":"dialogue_read","arguments":{"requests":[{"kind":"appointments","date":"YYYY-MM-DD"}],"mode":"answer"}}]}. Liest beide Konten, Kalender, zugehörige Mail-/Sentbelege und aktuelle Entwürfe. Relevante Originale bei Bedarf über requests:[{"kind":"mail_source","account":"code|stealth","message_id":"ID"}]. Vollständige Quellen und Lücken auswerten; Blocker/Entwurf beweist keine Zusage. Angebot, eigener ungesendeter Entwurf, beidseitige Vereinbarung und Einladung/RSVP trennen. Gegenseitenangebot plus eigener Draft ist keine beidseitige Zusage. Andere passende Tools bleiben nutzbar. Gemischte Alltagsfrage plus Codex-Bau: Sachantwort unabhängig vom Bau liefern; autorisierten Bau getrennt im Hintergrund starten, nicht warten/pollen. Ausdrücklich gründliche Untersuchung: mode=research. Neue Starts auf Verfügbarkeit prüfen: calendar_check_starts.
Lange Aufträge behalten Qualität, Quellen und Berechtigungen. background_job_submit nur für autorisierten Scope mit Ausführungsvertrag. Der native Dialog meldet gespeicherte Annahme und belegten Abschluss; keine zweite Empfangsbestätigung. Nach Submission Dialogturn beenden, Worker nicht pollen. Status/Cancel/Resume ohne neues Worker-Modell. Neue Nachrichten frei bearbeiten. Keine pauschale Schrittgrenze, schwächere Modellroute oder zusätzliche Router-Modellrunde. Nur unabhängige Reads parallelisieren, Writes und Readbacks geordnet.
Rechtzeitige Korrekturen steuern denselben laufenden Auftrag: überholte Vorgabe ersetzen, übrigen Auftrag bewahren. Kritik allein erlaubt keine Änderung bestehender Termine oder Entwürfe; ohne ausdrücklichen Änderungsauftrag nur künftiges Verhalten anpassen. Acribic und Stealth: Einladungen, echte Termine, Änderungen und Absagen nur nach des Nutzers konkreter Freigabe; die Zusage der Gegenpartei reicht nicht. Neue Einladungen ohne Agenda/Inhaltsbeschreibung, bestehende Beschreibungen nicht automatisch entfernen. Konferenzlink in nativer Konferenz-/Ortsangabe erhalten. Zwischen Beste Grüße und Alex/Alex Beispiel keine Leerzeile; HTML genau ein <br> im selben eigenen Textblock, kein separater Namensblock. Terminvorschläge ohne Berliner Zeit/Europe-Berlin/MEZ/MESZ-Zusatz, interne Zeitzone korrekt. Neue Terminvorschläge: calendar_check_starts. Mails: email_realtime.py prepare --account --message-id --thread-id; gespeicherten Fall nutzen. Outreach: draft_batch.py. Kein eigenes Gmail-HTML."""

ROLE += '\nNach Quellenread die vollständige Sachantwort samt erfragtem Entwurf und Lücken in answer des nächsten dialogue_coding_start geben; keine Denk-/Status- oder Bau-Erfolgsaussagen. Nur bei answer_presentation.native_stream_callback_invoked=true danach lediglich Start-/Unknown-Stand melden; sonst Sachantwort plus Stand vollständig im Final. Erst alle nötigen Quellen lesen. coding-only/research oder zu lang: answer leer, nichts kürzen.'

def role_for_targets(targets):
    names = [key for key in targets if isinstance(key, str) and re.fullmatch(r'[a-z0-9_-]{1,60}', key)] if isinstance(targets, dict) else []
    names = names[:8]
    if not names:
        return ROLE
    return ROLE + '\nNeuer ausdrücklich autorisierter Codex-Bau: tool_call {"calls":[{"name":"dialogue_coding_start","arguments":{"target":"ZIEL","brief":"konkreter Auftrag","answer":"vollständige Sachantwort oder leer"}}]}. Konfigurierte Zielnamen: ' + ', '.join(names) + '. Start und nativer Prozessbeleg sind gebündelt; kein Suchen/Warten. accepted=true belegt Übergabe, keine Fertigstellung.'

_PHRASES = {
    'status': {'wie weit ist der job', 'wie weit ist mein job', 'wie weit bist du mit dem job',
               'was macht der hintergrundauftrag', 'status vom job', 'job status', 'jobstatus',
               'wie ist der stand des jobs', 'wie weit ist der hintergrundauftrag'},
    'cancel': {'stoppe den job', 'stoppe meinen job', 'brich den job ab',
               'stoppe den hintergrundauftrag', 'brich den hintergrundauftrag ab'},
    'resume': {'setze den job fort', 'setze meinen job fort', 'setze den hintergrundauftrag fort'},
}
_ID = r'[a-zA-Z0-9_-]{8,80}'
_EXPLICIT = re.compile(rf'^(?:/job\s+)?(status|cancel|resume)\s+({_ID})$')
_GERMAN = re.compile(rf'^job\s+({_ID})\s+(status|stoppen|fortsetzen)$')

def control(text):
    if not isinstance(text, str) or len(text) > 240:
        return None
    clean = re.sub(r'\s+', ' ', text.strip().rstrip('.!?').lower())
    for action, phrases in _PHRASES.items():
        if clean in phrases:
            return action, None
    explicit = _EXPLICIT.fullmatch(clean)
    if explicit:
        return explicit[1], explicit[2]
    german = _GERMAN.fullmatch(clean)
    if german:
        return {'status':'status','stoppen':'cancel','fortsetzen':'resume'}[german[2]], german[1]
    return None

def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

def status_text(row):
    state = row.get('state')
    verified, total, blocked = (count(row.get(k)) for k in ('verified','total','blocked'))
    if row.get('family_total') is not None:
        verified,total=count(row.get('family_verified')),count(row.get('family_total'))
    progress = f'{verified}/{total} verarbeitet' if verified is not None and total is not None else 'Fortschritt noch nicht belegt'
    wording = {'queued':f'{progress}. Ich warte auf einen freien Platz',
               'running':f'{progress}. Ich arbeite im Hintergrund weiter',
               'paused':f'Pausiert: {progress}', 'cancelled':f'Abgebrochen: {progress}',
               'completed':f'Fertig: {progress}', 'completed_with_blockers':f'Fertig: {progress}',
               'failed':f'Fehlgeschlagen: {progress}',
               'review_required':f'{progress}. Die Ergebnisse brauchen noch Prüfung'}.get(state,f'Zustand noch nicht belegt. {progress}')
    if state=='revising':wording=f'Korrektur gespeichert; der alte Worker wird zuerst angehalten. {progress}'
    if state=='superseded':wording=f'Mit korrigierter Vorgabe fortgeführt als {row.get("replacement_job")}. {progress}'
    if state=='revision_blocked':wording=f'Die Korrektur konnte nicht sicher fortgesetzt werden. Ergebnisse sind erhalten; eine Klärung ist nötig. {progress}'
    if row.get('cancel_requested') and state not in ('cancelled','completed','completed_with_blockers','failed','revising','superseded','revision_blocked'):
        wording = f'{progress}. Ich habe den Abbruch angefordert; laufende Schritte werden sicher beendet'
    trouble = f' {blocked} Einträge brauchen Klärung.' if blocked else ''
    review = ' Die Ergebnisse liegen zur Prüfung bereit.' if state in ('completed','completed_with_blockers') else ''
    delivery = ' Die vollständige Ergebnisdatei konnte noch nicht automatisch zugestellt werden; die Ergebnisse bleiben gespeichert.' if row.get('result_delivery_failed') is True else ''
    return f'{wording}.{trouble}{review}{delivery} Auftrag: {row["job_id"]}.'

def accepted_text(row):
    total = count(row.get('total'))
    scope = f' für {total} Einträge' if total is not None else ''
    return f'Den Auftrag{scope} habe ich gespeichert. Ich bearbeite ihn im Hintergrund; du kannst hier weiterreden. Auftrag: {row["job_id"]}.'

def safe_snapshot(row):
    # None is unknown. No request, source, result, error prose or credential is copied.
    fields = ('job_id','state','owner','session_id','revision','receipt_ref','cancel_requested',
              'total','verified','blocked','pending','running')
    return {k: row.get(k) for k in fields}
