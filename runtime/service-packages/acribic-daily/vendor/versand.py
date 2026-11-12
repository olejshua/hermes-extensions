#!/usr/bin/env python3
"""Outreach-Versand für Acribic – Werkzeug zum Skill `acribic-versand`.

Ablauf (Zustand liegt in ~/.cache/acribic/versand/):
  python3 versand.py index                      CRM live aus Twenty → lokaler CSV-Export + Landminen-Prüfung
  python3 versand.py kandidaten <N>             Kandidatenpool (eine Adresse pro Domain) → batch.json + Gmail-Abfragen
  python3 versand.py ausschliessen d1 d2 …      Domains mit Gmail-Treffern aus dem Batch nehmen
  python3 versand.py smtp [--probe] [--remote]  Uni direkt oder von zuhause ueber den geprueften Hermes-VPS
  python3 versand.py mails <N> [--probe]        Mails nach Vorlage für die ersten N bestätigten Adressen → mails.json
  python3 versand.py abschluss [--ohne a,b] [--probe]   nach dem Versand: CRM, Nachtrag, Sperrliste-Historie
  python3 versand.py bounce adr1 adr2 …         Bounces: Stage „Adresse pruefen"
  python3 versand.py heute                      Zahl der heute versendeten Outreach-Mails

--probe = nichts ins CRM oder in die Vault-Dateien schreiben (Trockenlauf).
--liste it|ingenieur|medizin|beratung = welches CRM-Segment (Standard it). Alle Schritte eines Batches mit demselben Segment.
Der Doppelschutz gilt immer über alle Listen, die Sperrliste und den Nachtrag.
"""
import collections as C, concurrent.futures as cf, csv, datetime, json, os, re, subprocess, sys, time, urllib.error, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LEADS = os.path.dirname(HERE)
SPERR, NACHTRAG = (os.path.join(LEADS, f) for f in ("Nicht-kontaktieren.md", "Nachtrag-seit-Export.csv"))
CONFIG_BACKEND = os.path.expanduser('~/.config/acribic/crm-backend')
BACKEND = os.environ.get('ACRIBIC_CRM_BACKEND') or (open(CONFIG_BACKEND).read().strip() if os.path.exists(CONFIG_BACKEND) else 'notion')
if BACKEND not in ('notion', 'twenty'): raise SystemExit('Unbekannter CRM-Backend')
STATE = os.path.expanduser(os.environ.get('ACRIBIC_VERSAND_STATE', '~/.cache/acribic/versand' if BACKEND == 'notion' else '~/.cache/acribic/versand-twenty'))
os.makedirs(STATE, exist_ok=True)
BATCH, MAILS = os.path.join(STATE, "batch.json"), os.path.join(STATE, "mails.json")
SENDER_LOCK = os.path.expanduser('~/.cache/acribic/versand/abschluss-sicher.lock')
ATTEMPT_REGISTRY = os.path.expanduser('~/.cache/acribic/versand/send-attempts.jsonl')
# Getrennte Listen je Branche (Alex, 22.09.: „nicht architekten, it etc in eine liste“). Auswahl mit --liste <name>, Standard it.
LISTEN = {"it": "00000000-0000-4000-8000-000000000001",
          "ingenieur": "e0000000000000000000000000000004",
          "medizin": "e0000000000000000000000000000005",
          "beratung": "e0000000000000000000000000000006"}
# Nur als Dubletten-Schutz lesen. Diese Listen sind keine Versand-Auswahl.
SCHUTZ_LISTEN = {"sicherheit": "00000000-0000-4000-8000-000000000002",
                 "reinigung": "00000000-0000-4000-8000-000000000003",
                 "icp": "e0000000000000000000000000000007"}
LISTE = sys.argv[sys.argv.index("--liste") + 1] if "--liste" in sys.argv[:-1] else "it"
if LISTE not in LISTEN: raise SystemExit(f"Unbekannte Liste {LISTE!r} – möglich: {', '.join(LISTEN)}")
DB = LISTEN[LISTE]
if "--liste" in sys.argv[:-1]:  # aus argv entfernen, damit Befehle den Listennamen nicht als Argument lesen
    _i = sys.argv.index("--liste"); del sys.argv[_i:_i + 2]
INDEX = os.path.join(LEADS, "CRM-Index.csv" if LISTE == "it" else f"CRM-Index-{LISTE}.csv")
TOK = open(os.path.expanduser("~/.config/acribic/notion-token")).read().strip() if BACKEND == 'notion' else None
H = {"Authorization": f"Bearer {TOK}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
TWENTY = None
if BACKEND == 'twenty':
    from twenty_backend import TwentyBackend
    TWENTY = TwentyBackend(STATE)
UNI_NETZ = ("83.135.",)                         # Bisher verlässlich geprüfter Uni-Ausgang, nicht alle Versatel-Anschlüsse
OFFEN = {"Nicht kontaktiert", "Adresse pruefen"}  # jede andere Stage gilt als kontaktiert oder gesperrt
FREEMAIL = {"gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "t-online.de", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.de", "freenet.de", "aol.com", "online.de", "icloud.com"}
TECH = re.compile(r"^(karriere|career|careers|jobs?|bewerbung\w*|recruiting|hr|personal|postmaster|hostmaster|webmaster|abuse|noreply|no-reply|donotreply|mailer-daemon|support|helpdesk|hotline|servicedesk|datenschutz|privacy|dsgvo|rechnung\w*|invoice\w*|buchhaltung|accounting|billing|newsletter|presse|press|marketing|social|events?)([.\-_]|$)", re.I)
PLATZHALTER = re.compile(r"tedremoved|emailaddress\.given|^x+@|dummy|beispiel@|example\.", re.I)
ROLLE = set("info kontakt contact vertrieb sales office ausschreibung ausschreibungen tender tenders angebot angebote service team mail post postfach zentrale hello hallo public einkauf vergabe vergaben vgv projekte buero empfang verwaltung sekretariat anfrage anfragen bid bids kunden customer order auftrag de germany deutschland gf geschaeftsfuehrung management inside innendienst it edv".split())
FAMILIE = re.compile(r"\b(nexus|allgeier|dedalus|mesalvo|conet|rohde\s*&\s*schwarz|allegion|interflex|primeline|asknet|cerner|oracle)\b", re.I)
NOTIZ_STOP = re.compile(r"catch-all|policy|(?<!zufallsadresse )abgelehnt|bounce|existiert nicht|gesperrt", re.I)  # Der positive Zufallsadress-Test lautet „Zufallsadresse abgelehnt“.
NICHT_ANSCHREIBEN = re.compile(r"keine\s+(?:weitere\s+)?(?:e-?mail|nachricht)\s+(?:senden|schicken)|nicht\s+(?:erneut\s+|mehr\s+)?(?:anschreiben|kontaktieren)|opt[ -]?out", re.I)
POLICY = re.compile(r"5\.7\.\d|block|reputation|listed|spamhaus|pbl|client host|protocol error|not permitted|relay", re.I)
VORNAMEN = {"joerg": "Jörg", "juergen": "Jürgen", "bjoern": "Björn", "soeren": "Sören", "joern": "Jörn", "guenter": "Günter", "guenther": "Günther",
            "baerbel": "Bärbel", "kaethe": "Käthe", "goekhan": "Gökhan", "oezlem": "Özlem", "hans-juergen": "Hans-Jürgen", "karl-juergen": "Karl-Jürgen"}
STOP_ENDE = {"und", "für", "fuer", "der", "des", "die", "dem", "den", "im", "in", "am", "an", "von", "vom", "zur", "zum", "&", "sowie", "mit", "bei", "-", "–"}
FALLBACK = "Ihre Zuschläge"
FALLBACK_DU = "Eure Zuschläge"  # Du-Mail mit Ersatzbetreff: nie „Ihre“ über einem „ihr“-Text (Alex, 16.09.: Vorname und Sie nie mischen)


# ---------- Notion ----------
def api(method, path, body=None):
    if TWENTY:
        if method == 'GET' and path.startswith('/pages/'):
            return TWENTY.page(path.rsplit('/', 1)[-1])
        raise RuntimeError('Twenty-Modus: Notion-API ist deaktiviert; Twenty-Adapter verwenden')
    # Eine Notion-Schreibantwort kann nach erfolgreichem Anlegen verloren gehen.
    # Darum ungewisse Writes niemals automatisch wiederholen.
    lesend = method == "GET" or path.endswith("/query")
    for attempt in range(6):
        req = urllib.request.Request("https://api.notion.com/v1" + path, data=json.dumps(body).encode() if body is not None else None, headers=H, method=method)
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if (e.code == 429 or (lesend and e.code in (500, 502, 503, 504))) and attempt < 5:
                time.sleep(float(e.headers.get("Retry-After") or 1.5) + attempt)
                continue
            raise SystemExit(f"Notion-Fehler {e.code}: {e.read().decode()[:300]}")
        except (urllib.error.URLError, TimeoutError):
            if lesend and attempt < 5:
                time.sleep(2 + attempt)
                continue
            raise


def txt(p):
    t = p.get("type"); v = p.get(t)
    if t in ("title", "rich_text"): return "".join(x.get("plain_text", "") for x in v)
    if t in ("email", "url", "phone_number"): return v or ""
    if t == "select": return (v or {}).get("name", "")
    if t in ("number", "checkbox"): return v
    return ""


def hole(db):
    if TWENTY:
        names = [name for name, value in {**LISTEN, **SCHUTZ_LISTEN}.items() if value == db]
        if len(names) != 1: raise RuntimeError('Unbekanntes Twenty-Segment')
        groups = TWENTY.groups() if TWENTY.records else TWENTY.refresh(complete=True)
        return groups[names[0]]
    rows, cur = [], None
    while True:
        body = {"page_size": 100}
        if cur: body["start_cursor"] = cur
        d = api("POST", f"/databases/{db}/query", body)
        rows += [{"id": pg["id"].replace("-", ""), **{k: txt(v) for k, v in pg["properties"].items()}} for pg in d["results"]]
        if not d.get("has_more"): break
        cur = d["next_cursor"]
    return rows


_ANDERE = None


def andere_listen():
    """Zeilen aller anderen Listen – damit keine Domain über zwei Listen doppelt Post bekommt."""
    global _ANDERE
    if _ANDERE is None: _ANDERE = [r for k, db in {**LISTEN, **SCHUTZ_LISTEN}.items() if k != LISTE for r in hole(db)]
    return _ANDERE


def import_belegt():
    """Frischer, listenübergreifender Schutz direkt vor jedem Import-Batch."""
    rows = [r for db in {**LISTEN, **SCHUTZ_LISTEN}.values() for r in hole(db)]
    domains = {basis(dom(r.get(k))) for r in rows for k in ("E-Mail", "Website", "Domain")}
    names = {firma_key(r.get("Firma")) for r in rows}
    for line in open(NACHTRAG, encoding="utf-8"):
        e = line.split(";")[0]
        if "@" in e: domains.add(basis(dom(e)))
    sperrtext = open(SPERR, encoding="utf-8").read().lower()
    frei_m = re.search(r"^## 8\. freigegeben.*?(?=^## |\Z)", sperrtext, re.S | re.M)
    if frei_m: sperrtext = sperrtext[:frei_m.start()] + sperrtext[frei_m.end():]
    domains |= {basis(x) for x in re.findall(r"[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:de|com|eu|net|at|ch|org|info|io|biz|group|ag|berlin|digital|it|nl|fr|be|lu|dk|se|pl|cz|es|uk|co|one|swiss|tech|cloud|ai|app|media|solutions|systems|consulting)\b", sperrtext)}
    domains.discard(""); names.discard("")
    return domains, names


def pull():
    """Alle CRM-Zeilen frisch holen und den lokalen Index im bisherigen Format neu schreiben."""
    rows, cur = [], None
    if TWENTY:
        groups = TWENTY.refresh(complete=True)
        rows = groups[LISTE]
        global _ANDERE
        _ANDERE = [r for name, group in groups.items() if name != LISTE for r in group]
    while not TWENTY:
        body = {"page_size": 100}
        if cur: body["start_cursor"] = cur
        d = api("POST", f"/databases/{DB}/query", body)
        rows += [{"id": pg["id"].replace("-", ""), **{k: txt(v) for k, v in pg["properties"].items()}} for pg in d["results"]]
        if not d.get("has_more"): break
        cur = d["next_cursor"]
    cols = list(next(csv.reader(open(INDEX, encoding="utf-8"), delimiter=";"))) if os.path.exists(INDEX) else ["page_id"] + sorted(k for k in rows[0] if k != "id")
    with open(INDEX, "w", newline="", encoding="utf-8") as h:
        w = csv.writer(h, delimiter=";"); w.writerow(cols)
        for r in rows:
            out = []
            for c in cols:
                v = r["id"] if c == "page_id" else r.get(c, "")
                if isinstance(v, bool): v = "True" if v else ""
                if isinstance(v, float) and v.is_integer(): v = int(v)
                out.append("" if v is None else v)
            w.writerow(out)
    return rows


def patch(pid, stage=None, notiz=None):
    if TWENTY:
        return TWENTY.patch(pid, stage, notiz)
    props = {}
    if stage: props["Stage"] = {"select": {"name": stage}}
    if notiz is not None:
        alt = txt(api("GET", f"/pages/{pid}")["properties"]["Notizen"])
        neu = notiz + (" | " + alt if alt else "")
        props["Notizen"] = {"rich_text": [{"text": {"content": neu[:1990]}}]}
    api("PATCH", f"/pages/{pid}", {"properties": props})


# ---------- Hilfen ----------
def dom(x):
    x = (x or "").strip().lower()
    if "@" in x: return x.split("@")[-1].strip(" >;,")
    x = re.sub(r"^https?://", "", x).split("/")[0]
    return x[4:] if x.startswith("www.") else x


def basis(host):
    """Firmendomain ohne Subdomain: ba.valantic.com und solutions.valantic.com sind dieselbe Firma (23.09.)."""
    teile = (host or "").split(".")
    return ".".join(teile[-3:]) if len(teile) > 2 and teile[-2] in ("co", "com") else ".".join(teile[-2:])


def firma_key(name):
    """Konservativer Namensabgleich für denselben Betrieb mit anderer Maildomain."""
    woerter = re.findall(r"[a-z0-9]+", ascii_((name or "").lower()))
    rechtsform = {"gmbh", "mbh", "ag", "se", "kg", "co", "ohg", "ug", "ev", "e", "v", "inc", "ltd", "llc", "deutschland", "germany"}
    return " ".join(w for w in woerter if w not in rechtsform)


def ascii_(s):
    return s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue").replace("ß", "ss")


def load(p):
    return json.load(open(p, encoding="utf-8"))


def save(p, d):
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def crm_gesperrt(r):
    return (r.get('_TwentyBlocked') in (True, 'True') or r.get("Stage") not in OFFEN or r.get("Gesprochen") in (True, "True")
            or bool(NICHT_ANSCHREIBEN.search(r.get("Notizen") or "")))


def gesperrte_domains(rows):
    policy = TWENTY.read_policy() if TWENTY else None
    text = ((policy['suppression_text']+'\n') if policy else '') + open(SPERR, encoding="utf-8").read()
    text = text.lower()
    frei_m = re.search(r"^## 8\. freigegeben.*?(?=^## |\Z)", text, re.S | re.M)  # Abschnitt 8: nach Recheck ausdrücklich freigegeben
    frei_txt = frei_m.group(0) if frei_m else ""
    if frei_m: text = text[:frei_m.start()] + text[frei_m.end():]
    frei = set(re.findall(r"`([a-z0-9.\-]+\.[a-z]{2,})`", frei_txt))
    sp = set(re.findall(r"[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:de|com|eu|net|at|ch|org|info|io|biz|group|ag|berlin|digital|it|nl|fr|be|lu|dk|se|pl|cz|es|uk|co|one|swiss|tech|cloud|ai|app|media|solutions|systems|consulting)\b", text))
    sp -= frei
    history = (policy['sent_history_csv'].splitlines() if policy else []) + list(open(NACHTRAG, encoding='utf-8'))
    for line in history:
        f = line.split(";")
        if "@" not in f[0]: continue
        # Versand, Absage, Antwort sperren immer. Nur reine Prüfvermerke („Adresse pruefen“ ohne Bounce) hebt eine Freigabe auf.
        pruefvermerk = len(f) > 1 and f[1] == "Adresse pruefen" and "bounce" not in line.lower()
        if not (pruefvermerk and dom(f[0]) in frei): sp.add(dom(f[0]))
    alle = rows + andere_listen()
    # Ein zweiter CRM-Eintrag kann trotz offener Stage bereits ein Gespräch oder eine Sperre
    # dokumentieren. Alle Zeilen einer Firma/Domain zählen, nicht nur die ausgewählte Zeile.
    kontakt = {dom(r.get("E-Mail")) for r in alle if crm_gesperrt(r)} | {dom(r.get("Website")) for r in alle if crm_gesperrt(r)}
    # Eine Domain, die in einer anderen Liste schon steht, gilt dort als vergeben – auch wenn sie noch offen ist
    kontakt |= {dom(r.get("E-Mail")) for r in andere_listen()}
    kontakt |= {dom(r.get("Domain")) for r in andere_listen()}  # ICP-CRM, auch ohne E-Mail
    kontakt.discard("")
    if policy:
        kontakt |= {dom(email) for email in policy['attempted_mailboxes']}
    return sp, kontakt


# Postfach-Teile, die nie ein Vorname sind (example: a role mailbox is not a first name)
KEINE_NAMEN = {"accounts", "account", "accounting", "buchhaltung", "rechnung", "rechnungen", "invoice", "invoices",
               "billing", "finance", "finanzen", "payable", "receivable", "uk", "de", "at", "ch", "eu", "us", "fr", "nl"}


def vorname(email):
    """Vorname aus vorname.nachname@ – sonst None (dann „Guten Tag,")."""
    lp = email.split("@")[0].lower()
    if any(t in ROLLE or t in KEINE_NAMEN for t in re.split(r"[.\-_]", lp)): return None
    m = re.fullmatch(r"([a-zäöüß]+(?:-[a-zäöüß]+)?)[._]([a-zäöüß][a-zäöüß\-]+)", lp)
    if not m or len(m.group(1)) < 2: return None
    v = m.group(1)
    if v not in VORNAMEN and len(v) < 3: return None
    return VORNAMEN.get(v, "-".join(p.capitalize() for p in v.split("-")))


QUELLEN = ("Bietbetriebe.csv", "Bietbetriebe-Kontakt-fehlt.csv", "IT-Dienstleister.csv", "Healthcare-IT.csv",
           "Gebaeudereinigung.csv", "Sicherheit_FM.csv")
NAMENSSPALTEN = ("Auftraggeber_Top3", "Beispiel_Auftraggeber", "Auftraggeber_Beispiele")  # mehrere Namen mit „|" getrennt


def volltexte():
    """Domain -> [(Auftraggebername, vollständig?)] aus den TED-Quelldateien; im CRM steht der Name auf 55 Zeichen gekappt.
    Manche Quelldateien kappen selbst (bei 70). Gekappte Namen werden nur markiert, nie zurechtgestutzt: 16.09. kam
    „…Dänischenhagen, Dänischer Wohld und Hü" gestutzt als vermeintlich voller Name an und wurde am Komma zerschnitten."""
    aus = C.defaultdict(list)
    for datei in QUELLEN:
        pfad = os.path.join(LEADS, datei)
        if not os.path.exists(pfad): continue
        with open(pfad, encoding="utf-8") as h:
            zeilen = list(csv.DictReader(h, delimiter=";"))
        for spalte in NAMENSSPALTEN:
            felder = [(z, z[spalte]) for z in zeilen if (z.get(spalte) or "").strip()]
            kapp = kappungslaenge([f for _, f in felder])  # am ungekürzten Feld messen: ein gekapptes endet oft auf Leerzeichen
            for z, feld in felder:
                namen = [n.strip() for n in feld.split("|") if n.strip()]
                doms = {dom(z.get("Domain")), dom(z.get("E-Mail")), dom(z.get("Website"))} - {""}
                for i, n in enumerate(namen):
                    ganz = not (kapp and len(feld) == kapp and i == len(namen) - 1)  # die Kappung trifft nur den letzten Namen
                    for d in doms: aus[d].append((n, ganz))
    return aus


def kappungslaenge(werte):
    """Länge, auf die ein Feld gekappt ist – eine echte Kappung trifft viele Werte genau gleich und kaum einer ist länger
    (ein von Hand verlängerter Wert darf die Erkennung nicht abschalten). Ungekürzt messen."""
    laengen = C.Counter(len(v or "") for v in werte)
    for l in sorted(laengen, reverse=True):
        if laengen[l] >= 5:
            laenger = sum(n for k, n in laengen.items() if k > l)
            return l if laenger <= max(2, sum(laengen.values()) // 100) else 0
    return 0


# ---------- Auftraggeber für Betreff und „u. a."-Teil ----------
# Der Name kommt vollständig und richtig heraus oder gar nicht (None → Ersatzbetreff). Nie Wörter hinten abschneiden:
# 16.09. wurde „Beschaffungsamt des BMI" zu „Beschaffungsamt" und „MEDICLIN Kliniken Plau am See GmbH & Co. KG" zu „… GmbH & Co.".
TRENNER = re.compile(r"(?P<vertr>\s+vertreten\s+durch\s)|(?P<semi>\s*;\s+)|(?P<komma>\s*,\s+)|(?P<strich>\s+[-–]\s+)"
                     r"|(?P<klammer>\s+-(?=[^\s-][^-]*-(?:\s|$)))|(?P<slash>\s+/\s+)"
                     r"|(?P<rechtsform>(?:(?<=\sgGmbH)|(?<=\sGmbH)|(?<=\smbH)|(?<=\se\.V\.)|(?<=\se\. V\.)|(?<=\sAöR)|(?<=\sKdöR)|(?<=\seGbR)"
                     r"|(?<=\sCo\. KG))(?=\s*[/(&]|\s+[-–]|\s+[A-ZÄÖÜ0-9]|\s+(?:für|f|vertreten|c/o)\b)|(?<=\sAG)(?=/))|(?P<auf>\s+\()")  # nicht „GmbH des Bundes"
# Wörter, die eine Dienststelle oder Untereinheit bezeichnen – davor steht ein fertiger Name. Codes („IT@M-BO4", „H14/") zählen mit.
EINHEIT = re.compile(r"Amt|\w+amt|Ämter|\w+ämter|Referat|\w+referat|Dezernat|Abteilung|\w+abteilung|Abt|Fachbereich|FB|Fachdienst|Fachgebiet"
                     r"|Geschäftsbereich|GB|Sachgebiet|SG|Ressort|Stabsstelle|\w+stelle|Standort|Niederlassung|Dienststätte|Außenstelle"
                     r"|Direktorium|Direktion|Eigenbetrieb|Team|AöR|KdöR|Anstalt|Körperschaft|Rechtsträger|Ordinariat|Generalvikariat"
                     r"|Präsidium|Magistrat|Kreisausschuss|\w*[Bb]ürgermeister(?:in)?|Landrat|Landrätin|Präsident(?:in)?|Vorstand"
                     r"|Geschäftsführung|Rektorat|Kanzler(?:in)?|\w*[Kk]anzlei|\w*[Mm]inisterium|\w*[Kk]ämmerei|Zentrale[rs]?|\w*[Ss]itz|\S*\d\S*")
THEMA = re.compile(r"Bereich|\w*[Ee]inkauf|Beschaffung\w*|Vergabe\w*|\w*[Bb]etrieb\w*|\w*[Vv]erwaltung|Stiftung|\w*[Mm]anagement")  # auch Aufzählungsglied möglich
KURZWORT = re.compile(r"\w*(?:bank|kanzlei|betrieb|zentrum|kasse|management)|(?=(?:\S*[A-ZÄÖÜ]){2})[\w.&-]+")  # „- Förderbank", „- KKH"
VERTRETUNG = re.compile(r"(?:vertreten\w*|in\s+Vertretung|im\s+Auftrag|namens|handelnd|c/o|kurz|als|über\s+(?:das|den|die|dem)|für\s+(?:das|den|die|dem))\b|vertr\.", re.I)
KONJ = re.compile(r"\b(?:und|sowie|oder)\b|\bu\.\s|&")
# Kopf, nach dem eine Aufzählung weitergehen kann: „für Wirtschaft, Arbeit und …", „der Ämter X, Y und Z"
AUFZAEHLUNG = re.compile(r"(?:\b(?:für|f\.|des|der|von|vom|zur|zum|im|am|in|an|mit|über|bei|und|sowie)\s+[^\s,]+"
                         r"|\b(?:der|die)\s+(?:Ämter|Städte|Gemeinden|Länder|Kreise|Landkreise|Kommunen|Bezirke|\w+gemeinden)\b.*)$")
RECHTSFORM = re.compile(r"(?:\s+|\s*,\s*)(?:(?:GmbH|AG|SE|UG)\s*&\s*Co\.\s*(?:KGaA|KG|OHG)|gemeinnützige\s+GmbH|gGmbH|GmbH|mbH|gAG|AG|SE|SA|KGaA|KG|e\.\s?V\.?|eGbR|GbR|AöR|KdöR|Kommunalunternehmen)$")
LAENDER = re.compile(r"\b(Sachsen|Baden|Mecklenburg|Nordrhein|Rheinland|Schleswig)(?:\s*[-–]\s*|\s+)(Anhalt|Württemberg|Vorpommern|Westfalen|Pfalz|Holstein)\b")


def _worte(teil, rest, ganz):
    """Wörter eines Abschnitts; reicht er bis zur Kappung, zählt das letzte (evtl. angeschnittene) Wort nicht."""
    w = [x.strip(".,;:()") for x in teil.split()]
    return [x for x in (w[:-1] if not ganz and len(teil) == len(rest) else w) if x]


def _einheit(w):
    return any(EINHEIT.fullmatch(t) for t in (w, *w.split("-")) if t)


def _schnitt_ok(s, m, ganz):
    """Ist der Trenner m in s eine echte Namensgrenze? ganz=False: s ist gekappt, das letzte Wort kann angeschnitten sein."""
    kopf, rest, art = s[:m.start()].strip(), s[m.end():], m.lastgroup
    if len(kopf) < 8 or kopf.count("(") > kopf.count(")"): return False
    if art in ("vertr", "semi", "klammer", "rechtsform"): return True
    if art == "auf":  # gekappt in der Klammer („… Sachsen Anhalt (LIS") oder Aktenzeichen am Ende („(Bukr 2B, 44)")
        if ")" not in rest: return not ganz and bool(re.match(r"[A-ZÄÖÜ]{2}|\S+\s+\S", rest))
        return bool(re.fullmatch(r"[^()]*\d[^()]*\)", rest))
    rechtsform_davor = bool(RECHTSFORM.search(kopf))
    if art == "strich":  # „AOK Bayern - Die Gesundheitskasse" ja; „Industrie - und Handelskammer", „Weilheim – Schongau" nein
        if re.match(r"(?:und|oder|sowie|bzw)\b", rest): return False
        teil = re.split(r"\s*[,;]\s+|\s+[-–/]\s+", rest)[0]
        if rechtsform_davor or len(teil.split()) >= 2: return True
        w = _worte(teil, rest, ganz)
        return bool(w) and bool(_einheit(w[0]) or THEMA.fullmatch(w[0]) or KURZWORT.fullmatch(w[0]))
    if kopf.endswith("-"): return False  # „Gebäude-, Bau- und …"
    if VERTRETUNG.match(rest): return True
    if art == "slash":  # „Stadt Bamberg / Zentrale Beschaffungsstelle" ja; „Hochschule Zittau / Görlitz" nein
        teil = re.split(r"\s*[,;]\s+|\s+[-–/]\s+|\s+\(", rest)[0]
        return any(_einheit(w) for w in _worte(teil, rest, ganz)) or bool(re.match(r"(?:Universität|Hochschule)\b|Univ\.", rest))
    if rechtsform_davor: return True
    teil = re.split(r"\s+[-–]\s+|\s*;\s+|\s+\(", rest)[0]  # bis zum nächsten stärkeren Trenner
    segmente = re.split(r"\s*,\s+", teil)
    for i, seg in enumerate(segmente):
        worte = _worte(seg, rest, ganz) if i == len(segmente) - 1 else _worte(seg, seg + "x", True)
        if any(_einheit(w) or THEMA.fullmatch(w) for w in worte): break
        if KONJ.search(seg): return False  # Aufzählung vor der Dienststelle
    else:
        return False  # keine Dienststelle erkennbar: Komma kann zum Namen gehören
    if AUFZAEHLUNG.search(kopf):
        erstes = _worte(segmente[0], rest, ganz) if len(segmente) == 1 else _worte(segmente[0], "", True)
        if not (erstes and _einheit(erstes[0])) and (KONJ.search(teil) or (not ganz and len(teil) == len(rest))): return False
    return True


def kuerzen(text, ganz):
    """Name an der ersten echten Namensgrenze kürzen. Ohne sichere Grenze bleibt ein vollständiger Name ganz,
    ein gekappter ergibt None."""
    s = re.sub(r"\s+", " ", re.sub(r"^(https?://)?(www\.)?", "", (text or "").strip(), flags=re.I))
    s = LAENDER.sub(r"\1-\2", s)  # 16.09.: „AOK Sachsen - Anhalt" wurde „AOK Sachsen"
    for m in TRENNER.finditer(s):
        if _schnitt_ok(s, m, ganz):
            s, ganz = s[:m.start()], True
            break
    if not ganz: return None
    s = RECHTSFORM.sub("", s.strip(" ,;-–/")).strip(" ,;-–/")
    if not s or s.split()[-1].lower() in STOP_ENDE or s.count("(") != s.count(")"): return None
    return s if 4 <= len(s) <= 70 else None


def _naht_ok(name, pos, ganz):
    """Handgekürzter CRM-Wert endet bei pos im längeren Quellnamen. Falsch gekürzt, wenn mitten im Wort, vor „und"
    oder an einem Satzzeichen, das dort keine Namensgrenze ist („Sächsisches Staatsministerium für Soziales" + „, Gesundheit und …")."""
    rest = name[pos:]
    if rest[:1].isalnum() or re.match(r"\s*(?:und|sowie|oder|u\.|&)(?:\s|$)", rest): return False
    if re.match(r"\s*[,;/–-]", rest):
        return any(m.start() <= pos <= m.end() and _schnitt_ok(name, m, ganz) for m in TRENNER.finditer(name))
    return True


def voller_name(ag, kappung, quellen):
    """CRM-Wert mit den Namen aus den Quelldateien derselben Domain abgleichen → (Text, vollständig?)."""
    text = re.sub(r"\s+", " ", ag or "").strip()
    a = text.lower()
    passend = [(n, g) for n, g in ((re.sub(r"\s+", " ", n).strip(), g) for n, g in quellen) if n.lower().startswith(a)]
    if any(g and n.lower() == a for n, g in passend): return text, True
    laenger = [(n, g) for n, g in passend if len(n) > len(text)]
    if not (kappung and len(ag) == kappung):  # CRM-Feld nicht gekappt (ungekürzt gemessen; länger = von Hand eingetragen)
        if all(_naht_ok(n, len(text.rstrip(" ,;-–/")), g) for n, g in laenger): return text, True
    ganze = {n.lower(): n for n, g in laenger if g}
    if len(ganze) == 1: return next(iter(ganze.values())), True
    teile = sorted((n for n, g in laenger if not g), key=len)
    if not ganze and teile and all(teile[-1].lower().startswith(t.lower()) for t in teile): return teile[-1], False
    return text, False  # mehrdeutig oder ohne Quelle: nur der gekappte Text


def betreff(ag, kappung, voll=None):
    """Auftraggeber-Kurzform, noch kein inhaltlich freigegebener Betreff → (Name oder None, zugrunde gelegter Text). ag ungekürzt übergeben (Kappung
    wird an der Länge erkannt); voll = Namen aus volltexte() für die Domain, [(Name, vollständig?)]."""
    quellen = [(voll, True)] if isinstance(voll, str) else list(voll or [])
    text, ganz = voller_name(ag or "", kappung, quellen)
    return kuerzen(text, ganz), text


def mailtext(vor, firma, ag):
    du = bool(vor)  # Name bekannt: duzen (Alex, 16.09.)
    gruss = f"Hallo {vor}," if du else "Guten Tag,"
    ua = f" (u. a. {ag})" if ag else ""
    return (f"{gruss}\n\n"
            "wir sind ein entstehendes Software-Unternehmen an der CODE University in Berlin. Wir entwickeln Software für Unternehmen, die Angebote auf öffentliche Ausschreibungen einreichen.\n\n"
            f"Auf {firma} sind wir über die EU-Vergabedatenbank TED gestoßen, in der {'ihr' if du else 'Sie'} regelmäßig als Zuschlagsempfänger {'auftaucht' if du else 'auftauchen'}{ua}.\n\n"
            f"Für unsere Entwicklung möchten wir verstehen, wie {'ihr' if du else 'Sie'} Ausschreibungsunterlagen {'prüft' if du else 'prüfen'} und Angebote im Team {'vorbereitet' if du else 'vorbereiten'}: Welche Schritte kosten besonders viel Zeit, und wo helfen {'eure' if du else 'Ihre'} bisherigen Werkzeuge noch nicht ausreichend?\n\n"
            f"{'Wärst du' if du else 'Wären Sie'} oder die dafür zuständige Person in {'eurem' if du else 'Ihrem'} Team für einen 10-15-minütigen Austausch offen?\n\n"
            f"Falls das bei {'euch' if du else 'Ihnen'} nicht passt, genügt eine kurze Rückmeldung - dann melden wir uns nicht wieder.\n\n"
            "Vielen Dank im Voraus!\n\nMit besten Grüßen\nOle Beispiel")


# ---------- Befehle ----------
def cmd_index():
    rows = pull()
    post = {dom(r.get("E-Mail")) for r in rows if r.get("Stage") == "E-Mail raus"}
    post |= {dom(l.split(";")[0]) for l in open(NACHTRAG, encoding="utf-8") if ";E-Mail raus;" in l and "@" in l.split(";")[0]}
    potential = [r for r in rows if r.get('Stage') == 'Nicht kontaktiert' and dom(r.get('E-Mail')) in post]
    minen = [r for r in potential if not (TWENTY and crm_gesperrt(r))]
    print(f"CRM: {len(rows)} Zeilen | {dict(C.Counter(r.get('Stage') for r in rows).most_common())}")
    print(f"Landminen (offene Zeile auf Domain, die schon Post bekommen hat): {len(minen)}")
    if TWENTY:print(f'Historisch offene Quellen bereits durch Twenty gesperrt: {len(potential)-len(minen)}')
    for r in minen[:10]: print("  !!", r.get("Firma"), r.get("E-Mail"))
    return rows, len(minen)


def cmd_kandidaten(n):
    rows, minen = cmd_index()
    pick_all = candidate_pool(rows)
    pick = pick_all[:4 * n]
    doms = [dom(r['E-Mail']) for r in pick]
    save(BATCH, {'batch_id':str(uuid.uuid4()), 'crm_backend':BACKEND, 'liste':LISTE,
                 'datum':datetime.date.today().isoformat(), 'n':n, 'landminen':minen,
                 'gmail_domains':doms, 'gmail_treffer':[],
                 'kandidaten':[{'page_id':r['id'], 'email':r['E-Mail'].strip(), 'firma':r.get('Firma'), 'zuschlaege':r.get('Zuschläge')} for r in pick]})
    print(f'Kandidaten: {len(pick)} von {len(pick_all)} möglichen Firmen (offen und sauber)')
    print('Gmail-Gegenprobe – muss nach Prüfung leer sein:')
    for i in range(0,len(doms),18):
        print('  in:anywhere ('+' OR '.join(f'to:{d} OR from:{d}' for d in doms[i:i+18])+')')


def candidate_pool(rows):
    sp, kontakt = gesperrte_domains(rows)
    gesperrt = sp | kontakt
    gesperrt_basis = {basis(x) for x in gesperrt}  # Schwesterfirma auf anderer Subdomain hat schon Post
    fremde_namen = {firma_key(r.get("Firma")) for r in andere_listen()}
    kontakt_namen = {firma_key(r.get("Firma")) for r in rows if crm_gesperrt(r)}
    belegte_namen = (fremde_namen | kontakt_namen) - {""}
    haeufig = C.Counter((r.get("E-Mail") or "").strip().lower() for r in rows)
    wahl = []
    for r in rows:
        e = (r.get("E-Mail") or "").strip()
        if r.get("Stage") != "Nicht kontaktiert" or crm_gesperrt(r) or "@" not in e: continue
        d = dom(e); lp = e.split("@")[0]
        if d in gesperrt or dom(r.get("Website")) in gesperrt or d in FREEMAIL: continue
        if d not in FREEMAIL and (basis(d) in gesperrt_basis or basis(dom(r.get("Website"))) in gesperrt_basis): continue
        if firma_key(r.get("Firma")) in belegte_namen: continue
        if TECH.match(lp) or PLATZHALTER.search(e) or haeufig[e.lower()] > 1: continue
        familien_treffer = FAMILIE.search(r.get("Firma") or "")
        # Das Berliner Forschungsinstitut ist nicht Teil der bereits kontaktierten NEXUS AG.
        nexus_institut = (r.get("Firma") or "").lower().startswith("nexus institut für kooperationsmanagement") and basis(d) == "nexusinstitut.de"
        if (familien_treffer and not nexus_institut) or NOTIZ_STOP.search((r.get("Notizen") or "").split(" | ")[0]): continue
        wahl.append(r)
    pro_domain = {}
    for r in sorted(wahl, key=lambda r: (vorname(r["E-Mail"]) is None, -(r.get("Zuschläge") or 0))):
        pro_domain.setdefault(basis(dom(r["E-Mail"])), r)
    pro_name = {}
    for r in pro_domain.values(): pro_name.setdefault(firma_key(r.get("Firma")) or r["E-Mail"].lower(), r)
    return sorted(pro_name.values(), key=lambda r: -(r.get('Zuschläge') or 0))


def cmd_ausschliessen(doms):
    b = load(BATCH); raus = {d.lower() for d in doms}
    b["kandidaten"] = [c for c in b["kandidaten"] if dom(c["email"]) not in raus]
    b["gmail_treffer"] = sorted(set(b["gmail_treffer"]) | raus)
    save(BATCH, b)
    print(f"ausgeschlossen: {len(raus)} | verbleibend: {len(b['kandidaten'])}")


def cmd_smtp(probe, trotzdem, remote=False):
    import smtp_pruefen as sp
    b = load(BATCH)
    addrs = [c["email"] for c in b["kandidaten"]]
    if remote:
        if trotzdem: raise SystemExit("--remote und --trotzdem duerfen nicht kombiniert werden.")
        if len(addrs) > 200: raise SystemExit("VPS-Pruefung: maximal 200 Adressen pro Lauf.")
        basis_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "hermes-host"]
        check = subprocess.run(basis_cmd + ["curl -4 -fsS --max-time 8 https://api.ipify.org"],
                               capture_output=True, text=True, timeout=20)
        ip = check.stdout.strip()
        if check.returncode or ip != "45.89.127.191":
            raise SystemExit(f"VPS-Ausgang nicht wie geprueft (IP {ip or '?'}) – kein SMTP-Lauf.")
        p = subprocess.run(basis_cmd + ["python3 /home/hermes/acribic-smtp/smtp_pruefen.py --jsonl"],
                           input="\n".join(addrs) + "\n", capture_output=True, text=True,
                           timeout=max(60, 65 * len(addrs)))
        if p.returncode:
            raise SystemExit(f"VPS-SMTP fehlgeschlagen: {p.stderr[:300]}; kein Batch-Befund gespeichert.")
        try:
            records = [json.loads(line) for line in p.stdout.splitlines()]
            if [r["email"] for r in records] != addrs:
                raise ValueError("Reihenfolge/Anzahl der VPS-Ergebnisse weicht vom Batch ab")
            res = [(r["email"], r["status"], r["real"], r["random"]) for r in records]
        except (ValueError, KeyError) as exc:
            raise SystemExit(f"VPS-Antwort unvollstaendig: {exc}; kein Batch-Befund gespeichert.")
    else:
        ip = subprocess.run(["curl", "-s", "--max-time", "8", "https://api.ipify.org"], capture_output=True, text=True).stdout.strip()
        if not ip.startswith(UNI_NETZ) and not trotzdem:
            raise SystemExit(f"IP {ip or '?'} ist kein bislang freigegebener SMTP-Ausgang (Uni: 83.135.x). "
                             "Keine Batch-Pruefung; zuhause --remote fuer den geprueften VPS nutzen. "
                             "Ein Netz-/Policy-Fehler ist kein Befund zur Empfaengeradresse.")
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(sp.pruefe, addrs))
    heute = datetime.date.today().strftime("%d.%m.%Y")
    for c, (_, st, real, rand) in zip(b["kandidaten"], res):
        if st in ("ABGELEHNT", "KEIN MX") and POLICY.search(real or ""): st = "POLICY"
        c["smtp"], c["smtp_antwort"], c["smtp_zufall"], c["smtp_geprueft_am"], c["smtp_probe"] = st, real, rand, datetime.date.today().isoformat(), probe
        if probe: continue
        if st in ("POLICY", "FEHLER", "GREY", "UNKLAR"):
            # Netz-/Reputations- und unklare Befunde sagen nichts Sicheres über die Mailbox.
            # Weder Stage noch Notiz dürfen dadurch zu einem falschen Adressbefund werden.
            continue
        if st != "OK":
            patch(c["page_id"], "Adresse pruefen", f"SMTP {heute} (IP {ip}): {st} – Adresse nicht bestaetigt; {ascii_(real or '')[:60]}")
    b["smtp_stats"] = dict(C.Counter(c["smtp"] for c in b["kandidaten"]))
    b['smtp_exit'] = {'ip':ip,'remote':remote,'verified':ip.startswith(UNI_NETZ) or ip == '45.89.127.191',
                      'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save(BATCH, b)
    print(f"IP {ip} | SMTP: {b['smtp_stats']}" + (" | Trockenlauf, nichts vermerkt" if probe else ""))
    for c in b["kandidaten"]:
        if c["smtp"] != "OK": print(f"  {c['smtp']:<10} {c['email']:<40} {(c['smtp_antwort'] or '')[:50]}")


def cmd_mails(n, probe):
    import smtp_pruefen as sp
    b = load(BATCH)
    if b.get("liste", "it") != LISTE: raise SystemExit(f"Batch gehört zur Liste {b.get('liste', 'it')!r}, nicht {LISTE!r} – mit --liste {b.get('liste', 'it')} aufrufen.")
    rows = pull()
    live = {r["id"]: r for r in rows}
    _, kontakt = gesperrte_domains(rows)
    sperr_namen = {firma_key(x.get("Firma")) for x in andere_listen()}
    sperr_namen |= {firma_key(x.get("Firma")) for x in rows if crm_gesperrt(x)}
    sperr_namen.discard("")
    haeufig = C.Counter((r.get("E-Mail") or "").strip().lower() for r in rows)
    kappung, voll = kappungslaenge([r.get("Beispiel-Auftraggeber") for r in rows]), volltexte()
    out, weg = [], []
    for c in b["kandidaten"]:
        if (c.get("smtp") != "OK" or c.get("smtp_probe", True)
                or c.get("smtp_geprueft_am") != datetime.date.today().isoformat()
                or sp.status(c.get("smtp_antwort", ""), c.get("smtp_zufall", "")) != "OK"):
            continue
        r = live.get(c["page_id"])
        e = c["email"].lower()
        if not r or r.get("Stage") != "Nicht kontaktiert" or crm_gesperrt(r) or (r.get("E-Mail") or "").strip().lower() != e:
            weg.append((c["email"], "Zeile live nicht mehr 'Nicht kontaktiert'")); continue
        if (dom(e) in kontakt or basis(dom(e)) in {basis(x) for x in kontakt}
                or firma_key(r.get("Firma")) in sperr_namen or haeufig[e] != 1):
            weg.append((c["email"], "Domain/Adresse inzwischen belegt")); continue
        ag, roh = betreff(r.get("Beispiel-Auftraggeber"), kappung, voll.get(dom(e), []) + voll.get(dom(r.get("Website")), []))
        firma = (r.get("Firma") or "").strip()
        vor = vorname(c["email"])
        flags = ["Betreff inhaltlich prüfen"]
        if re.search(r"\d{4,}|straße|str\.|campus|\bplatz\b|https?://|www\.", firma, re.I): flags.append("Firma prüfen")
        if not ag: flags.append("Betreff-Rückfall")
        out.append({"page_id": c["page_id"], "to": c["email"], "subject": ag or (FALLBACK_DU if vor else FALLBACK), "body": mailtext(vor, firma, ag), "vorname": vor,
                    "firma": firma, "ag_roh": roh, "gekuerzt": bool(ag and ag != roh), "subject_review": None, "flags": flags})
        if len(out) == n: break
    save(MAILS, out)
    print(f"Mails gebaut: {len(out)} von gewünschten {n}" + (f" – zu wenige bestätigte Adressen, `kandidaten` mit größerem N erneut laufen lassen" if len(out) < n else ""))
    for x in weg: print("  weggelassen:", *x)
    for i, m in enumerate(out, 1):
        print(f"  {i:>2}. {m['to']:<38} | {m['subject'][:44]:<44} | {('Hallo ' + m['vorname']) if m['vorname'] else 'Guten Tag'}" + (f" | {', '.join(m['flags'])}" if m["flags"] else ""))
    print(f"Mails liegen in {MAILS} (to/subject/body exakt so senden).")


def cmd_abschluss(ohne, probe):
    mails = load(MAILS)
    b = load(BATCH) if os.path.exists(BATCH) else {}
    if b.get("liste", "it") != LISTE: raise SystemExit(f"Batch gehört zur Liste {b.get('liste', 'it')!r}, nicht {LISTE!r} – mit --liste {b.get('liste', 'it')} aufrufen.")
    sent = [m for m in mails if m["to"].lower() not in ohne]
    if not probe and {m['to'].strip().lower() for m in sent} != set(globals().get('_CONFIRMED_COMPLETION', ())):
        raise SystemExit('CRM-Abschluss ausschließlich über versand_abschluss_sicher.py mit bestätigtem Versandjournal.')
    heute = datetime.date.today(); d_de, d_iso = heute.strftime("%d.%m.%Y"), heute.isoformat()
    nr = max([int(x) for x in re.findall(r"Batch (\d+)", open(SPERR, encoding="utf-8").read())] or [0]) + 1
    for m in sent:
        notiz = f"Outreach-Mail raus am {d_de}, Adresse per SMTP bestaetigt (eine Verbindung pro Adresse, Catch-all ausgeschlossen)."
        if m["vorname"]: notiz += f" Persoenliche Anrede (Hallo {ascii_(m['vorname'])})."
        if m["gekuerzt"]: notiz += f" Betreff auf '{ascii_(m['subject'])}' gekuerzt."
        if m["subject"] in (FALLBACK, FALLBACK_DU): notiz += f" Betreff-Rueckfall '{ascii_(m['subject'])}'."
        if probe:
            print("  [Probe]", m["to"], "→", notiz[:110]); continue
        patch(m["page_id"], "E-Mail raus", notiz)
        with open(NACHTRAG, "a", encoding="utf-8") as h: h.write(f"{m['to']};E-Mail raus;{d_iso};Batch {nr}\n")
    st = b.get("smtp_stats", {})
    zeile = (f"- **{d_de}** — Batch {nr}, {len(sent)} Mails (Skill acribic-versand). Landminen vorab: {b.get('landminen', '?')}. "
             f"Gmail-Gegenprobe über {len(b.get('gmail_domains', []))} Domains: {len(b.get('gmail_treffer', []))} Treffer. SMTP: "
             + ", ".join(f"{v}× {k}" for k, v in st.items()) + ".")
    if probe:
        print("[Probe] Historie:", zeile); return
    with open(SPERR, "a", encoding="utf-8") as h: h.write(("" if open(SPERR, encoding="utf-8").read().endswith("\n") else "\n") + zeile + "\n")
    pull()
    print(f"Batch {nr} abgeschlossen: {len(sent)} im CRM auf 'E-Mail raus'. Heute insgesamt: {heute_anzahl()}")


def cmd_bounce(addrs):
    rows = {(r.get("E-Mail") or "").strip().lower(): r for r in pull()}
    d_de, d_iso = datetime.date.today().strftime("%d.%m.%Y"), datetime.date.today().isoformat()
    for a in addrs:
        r = rows.get(a.lower())
        if not r: print("  nicht im CRM:", a); continue
        patch(r["id"], "Adresse pruefen", f"Bounce am {d_de} (Outreach-Mail kam zurueck).")
        with open(NACHTRAG, "a", encoding="utf-8") as h: h.write(f"{a};Adresse pruefen;{d_iso};Bounce\n")
        print("  vermerkt:", a)


def heute_anzahl():
    d = datetime.date.today().isoformat()
    return sum(1 for l in open(NACHTRAG, encoding="utf-8") if f";E-Mail raus;{d};" in l)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit()
    probe = "--probe" in a
    if a[0] in ("kandidaten", "mails") and (len(a) < 2 or not a[1].isdigit() or int(a[1]) <= 0):
        raise SystemExit("Für kandidaten und mails ist die aktuelle, ausdrücklich angegebene positive Zahl N Pflicht.")
    if a[0] == "index": cmd_index()
    elif a[0] == "kandidaten": cmd_kandidaten(int(a[1]))
    elif a[0] == "ausschliessen": cmd_ausschliessen([x for x in a[1:] if not x.startswith("--")])
    elif a[0] == "smtp": cmd_smtp(probe, "--trotzdem" in a, "--remote" in a)
    elif a[0] == "mails": cmd_mails(int(a[1]), probe)
    elif a[0] == "abschluss":
        ohne = set()
        if "--ohne" in a: ohne = {x.strip().lower() for x in a[a.index("--ohne") + 1].split(",")}
        cmd_abschluss(ohne, probe)
    elif a[0] == "bounce": cmd_bounce(a[1:])
    elif a[0] == "heute": print("Heute versendet:", heute_anzahl())
    else: print(__doc__)
