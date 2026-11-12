"""Produce only four exact, source-mutator candidates from preserved bytes.

Run once while building this private capsule, never against production paths.
No legacy module is imported or executed.
"""
from pathlib import Path
import ast

BASE=Path(__file__).resolve().parents[2]
BUNDLE=BASE/'bundle';ORIGINAL=BASE/'originals/mac';MAC=BUNDLE/'candidates/mac'


def replace(text,old,new,count=1):
    if text.count(old)!=count:raise RuntimeError('candidate preimage did not match: '+old[:70])
    return text.replace(old,new)


def function(text,name,new):
    tree=ast.parse(text);node=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name==name)
    lines=text.splitlines(keepends=True)
    lines[node.lineno-1:node.end_lineno]=[new.rstrip()+'\n']
    return ''.join(lines)


def save(name,text):
    ast.parse(text);(MAC/name).write_text(text)


def with_imports(text,imports):
    """Keep shebang, encoding comments, module docstring and future imports."""
    tree=ast.parse(text);lines=text.splitlines(keepends=True);anchor=0
    for i,node in enumerate(tree.body):
        is_doc=i==0 and isinstance(node,ast.Expr)and isinstance(node.value,ast.Constant)and isinstance(node.value.value,str)
        if is_doc or isinstance(node,ast.ImportFrom)and node.module=='__future__':anchor=node.end_lineno;continue
        if not anchor:anchor=node.lineno-1
        break
    lines[anchor:anchor]=[imports.rstrip()+'\n']
    return ''.join(lines)


def build():
    t=with_imports((ORIGINAL/'triage.py').read_text(),'import central_source_client as central')
    t=function(t,'sperrliste_eintragen','''def sperrliste_eintragen(firma, domain, wortlaut, datum, probe, action_id=None):
    return central.suppress(firma, domain, wortlaut, datum, probe, action_id=action_id)
''')
    t=replace(t,'sperrliste_eintragen(firma, dom(adresse), wortlaut, heute_de, probe)',
                'sperrliste_eintragen(firma, dom(adresse), wortlaut, heute_de, probe, action_id="triage-msg:" + msgid)')
    t=replace(t,'with open(NACHTRAG, "a", encoding="utf-8") as h: h.write(zeile)','central.append_csv(zeile, "triage")')
    t=t.replace('def cmd_vermerken(a):','def cmd_vermerken(a):\n    central.request({"operation":"current"})',1)
    save('triage.py',t)
    # Preserve B-v2's existing native write/SMTP boundaries in versand.py.
    t=with_imports((ORIGINAL/'versand-b-v2.py').read_text(),'import central_source_client as central\nimport io')
    start=t.index('    policy = TWENTY.read_policy() if TWENTY else None\n',t.index('def gesperrte_domains'))
    end=t.index('    alle = rows + andere_listen()',start)
    t=t[:start]+'''    current=central.view()
    sp=set(current['blocked']['domains'])
'''+t[end:]
    t=replace(t,'open(SPERR, encoding="utf-8").read())] or [0]) + 1','central.view()["history_text"])] or [0]) + 1')
    t=replace(t,'with open(NACHTRAG, "a", encoding="utf-8") as h: h.write(f"{m[\'to\']};E-Mail raus;{d_iso};Batch {nr}\\n")',
                'central.append_csv(f"{m[\'to\']};E-Mail raus;{d_iso};Batch {nr}\\n", "versand-completion")')
    t=replace(t,'with open(SPERR, "a", encoding="utf-8") as h: h.write(("" if open(SPERR, encoding="utf-8").read().endswith("\\n") else "\\n") + zeile + "\\n")',
                'central.record_history(zeile + "\\n", "versand-completion")')
    t=replace(t,'with open(NACHTRAG, "a", encoding="utf-8") as h: h.write(f"{a};Adresse pruefen;{d_iso};Bounce\\n")',
                'central.append_csv(f"{a};Adresse pruefen;{d_iso};Bounce\\n", "versand-bounce")')
    t=t.replace('open(SPERR, encoding="utf-8").read()','central.view()["suppression_text"]')
    t=t.replace('open(NACHTRAG, encoding="utf-8")','io.StringIO(central.view()["addendum_csv"])')
    t=t.replace("open(NACHTRAG, encoding='utf-8')","io.StringIO(central.view()['addendum_csv'])")
    t=function(t,'heute_anzahl','def heute_anzahl():\n    return central.today_count()')
    t=t.replace('def cmd_bounce(addrs):','def cmd_bounce(addrs):\n    central.request({"operation":"current"})',1)
    save('versand.py',t)
    t=with_imports((ORIGINAL/'icp.py').read_text(),'import central_source_client as central\nimport io')
    t=t.replace('open(v.SPERR, encoding="utf-8").read())] or [0]) + 1','central.view()["history_text"])] or [0]) + 1')
    t=t.replace('open(v.SPERR, encoding="utf-8").read()','central.view(icp=True)["suppression_text"]')
    t=t.replace('open(v.NACHTRAG, encoding="utf-8")','io.StringIO(central.view()["addendum_csv"])')
    t=replace(t,'with open(v.NACHTRAG, "a", encoding="utf-8") as h: h.write(f"{m[\'to\']};E-Mail raus;{d_iso};ICP-Welle {nr}\\n")',
                'central.append_csv(f"{m[\'to\']};E-Mail raus;{d_iso};ICP-Welle {nr}\\n", "icp-completion")')
    t=replace(t,'with open(v.SPERR, "a", encoding="utf-8") as h: h.write(("" if central.view(icp=True)["suppression_text"].endswith("\\n") else "\\n") + zeile + "\\n")',
                'central.record_history(zeile + "\\n", "icp-completion")')
    t=t.replace('def cmd_abschluss(ohne):','def cmd_abschluss(ohne):\n    central.request({"operation":"current"})',1)
    save('icp.py',t)
    t=with_imports((ORIGINAL/'versand_abschluss_sicher.py').read_text(),'import central_source_client as central')
    t=function(t,'sent_history_counts','def sent_history_counts(path):\n    return central.sent_counts()')
    t=t.replace('    journal = Path(journal)\n','    central.request({"operation":"current"})\n    journal = Path(journal)\n',1)
    save('versand_abschluss_sicher.py',t)
    print('four private source candidates built; no legacy import')


if __name__=='__main__':build()
