#!/usr/bin/env python3
"""Prüft E-Mail-Adressen per SMTP, ohne etwas zu senden (EHLO, MAIL FROM, RCPT TO, QUIT).

Eine Verbindung pro Adresse (mehrere RCPT in einer Sitzung lösen Artefakte wie "452 Too many recipients" aus)
und immer ein Catch-all-Test mit einer Zufallsadresse derselben Domain.

Nur aus einem zuvor einzeln geprüften Netz laufen lassen. Der bisher freigegebene Uni-Ausgang hat
83.135.x; der O2-Hotspot und die am 28.09.2026 getestete Heim-IP 24.40.188.130 lieferten
IP-Reputationssperren. Solche Befunde sagen nichts über die Empfängeradresse und gehören nicht
als Adressfehler ins CRM. Auch ein anderer 1&1-Versatel-Anschluss ist nicht automatisch geeignet.

Aufruf:
  python3 smtp_pruefen.py adressen.txt                      # eine Adresse pro Zeile
  python3 smtp_pruefen.py --person "Eva Krehl" prosoz.de    # Muster raten, bis zum ersten Treffer prüfen

Status: OK | CATCH-ALL (Domain nimmt alles an, nicht prüfbar) | GREY | POLICY | UNKLAR | FEHLER | ABGELEHNT
"""
import smtplib, ssl, socket, sys, random, string, re, unicodedata, concurrent.futures as cf
import dns.resolver

HELO = "primary.example.invalid"
FROM = "operator@primary.example.invalid"
# Häufigkeit im CRM (276 persönliche Adressen): ~47 % vorname.nachname, ~26 % ein Wort, ~11 % v.nachname
MUSTER = ["{f}.{l}", "{f0}.{l}", "{f0}{l}", "{f}", "{l}", "{f}{l}", "{f}_{l}", "{f}-{l}", "{l}.{f}"]


def mx(domain):
    try:
        return sorted((r.preference, str(r.exchange).rstrip(".")) for r in dns.resolver.resolve(domain, "MX"))[0][1]
    except Exception:
        return None


# Nur für die Prüfung, es wird nichts übertragen – Zertifikate der MX passen oft nicht zum Hostnamen.
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE


def rcpt(host, addr, tls=True):
    """Eine Verbindung, ein RCPT. Gibt die Serverantwort zurück.

    Bietet der Server STARTTLS an, wird verschlüsselt. Viele Server (M365, Proofpoint) lehnen RCPT
    sonst mit 421/454 ab oder brechen ab – das sah wie Greylisting aus (21.09.: agineo, IGES, digitevo).
    Scheitert der TLS-Handshake, einmal unverschlüsselt. Lehnt der Server schon MAIL FROM ab, kommt
    diese Antwort zurück (sonst stünde dort nur ein nichtssagendes 503).
    """
    try:
        s = smtplib.SMTP(host, 25, local_hostname=HELO, timeout=30)
    except Exception as e:
        return "FEHLER " + type(e).__name__
    try:
        s.ehlo_or_helo_if_needed()
        if tls and s.has_extn("starttls"):
            try:
                s.starttls(context=TLS)
            except (ssl.SSLError, smtplib.SMTPException, OSError):
                s.close()
                return rcpt(host, addr, tls=False)
            s.ehlo()
        code, msg = s.mail(FROM)
        if code == 250:
            code, msg = s.rcpt(addr)
        zeilen = msg.decode(errors="replace").splitlines() or [""]
        return f"{code} {zeilen[-1].strip()}"
    except Exception as e:
        return "FEHLER " + type(e).__name__
    finally:
        try:
            s.quit()
        except Exception:
            s.close()


def zufall(domain):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(16)) + "@" + domain


def status(real, rand):
    if real.startswith("FEHLER"):
        return "FEHLER"  # Timeout/Verbindungsfehler – keine Aussage über die Adresse
    if re.search(r"5\.7\.\d|bad reputation|spamhaus|\bblocked\b|client host|listed|spam policy|\brdns\b|reverse[ -]dns|cannot find (?:your )?hostname", real, re.I):
        return "POLICY"  # Der prüfende Anschluss wurde abgewiesen, nicht die Mailbox.
    if real[:3] == "250" and rand[:3] == "250":
        return "CATCH-ALL"
    if real[:3] == "250":
        # Nur eine empfängerbezogene Ablehnung der Zufallsadresse belegt die Mailbox.
        # 550 wegen IP-/Spam-Policy, Relay oder Befehlsfehler ist kein Gegenbeweis.
        r = rand.lower()
        if re.search(r"for explanation visit|postmaster\.|xgemail|command rejected|unable to relay|relay access denied|\bblocked\b|spam|reputation|listed|i=ip|\brdns\b|reverse[ -]dns|cannot find (?:your )?hostname", r):
            return "POLICY"
        if rand[:1] == "4":
            return "GREY"
        if rand.startswith("FEHLER"):
            return "FEHLER"
        if re.match(r"550 5\.4\.1\s+recipient address rejected: access denied", r):
            return "OK"  # Exchange Online DBEB: unbekannter Empfänger
        if re.search(r"5\.1\.[012]|unknown|not found|no such|unroutable|unrouteable|mailbox unavailable|address invalid|invalid recipient|#5\.1\.0 address rejected|no mailbox here by that name|does not exist", r):
            return "OK"
        return "UNKLAR"
    if real[:1] == "4":
        return "GREY"
    return "ABGELEHNT"


def pruefe(addr):
    host = mx(addr.split("@")[1])
    if not host:
        return addr, "KEIN MX", "", ""
    real, rand = rcpt(host, addr), rcpt(host, zufall(addr.split("@")[1]))
    return addr, status(real, rand), real[:160], rand[:160]


def ascii_name(s, umlaut=True):
    s = s.strip().lower()
    if umlaut:
        s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = s.replace("ß", "ss")
    return re.sub(r"[^a-z\-]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode())


def kandidaten(vorname, nachname):
    out = []
    for um in (True, False):  # Umlaute immer als ae/oe/ue UND ohne probieren
        f, l = ascii_name(vorname, um), ascii_name(nachname, um)
        for m in MUSTER:
            x = m.format(f=f, l=l, f0=f[:1])
            if x not in out:
                out.append(x)
    return out


def person(name, domain):
    vorname, nachname = name.split(" ", 1)
    host = mx(domain)
    if not host:
        return "KEIN MX", ""
    rand = rcpt(host, zufall(domain))
    if rand[:3] == "250":
        return "CATCH-ALL", "Muster aus bekannter Adresse der Domain übernehmen oder Surfe-Anreicherung"
    for lp in kandidaten(vorname, nachname):
        r = rcpt(host, f"{lp}@{domain}")
        if r[:3] == "250":
            return "OK", f"{lp}@{domain}"
        if r[:1] == "4":
            return "GREY", f"{lp}@{domain} ({r[:40]})"
    return "NICHT GEFUNDEN", ""


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--person":
        print(*person(sys.argv[2], sys.argv[3]))
    else:
        addrs = [l.strip() for l in open(sys.argv[1]) if "@" in l] if len(sys.argv) > 1 else [l.strip() for l in sys.stdin if "@" in l]
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for a, st, real, rand in ex.map(pruefe, addrs):
                print(f"{st:<10} {a:<42} real={real:<50} rand={rand}")
