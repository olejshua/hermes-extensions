"""Deny unresolved protected-name collisions, without inferring group ownership."""
from collections import defaultdict
from difflib import SequenceMatcher
import re

def compact(canonical):
    return re.sub('[^a-z0-9]','',canonical.casefold())

def distinctive(canonical):
    # Professional/legal descriptors must not hide an otherwise identical
    # multi-part name, e.g. three founders followed by "Architekten BDA".
    descriptors={'architekten','architekt','architects','architect','planners','stadtplaner','stadtplanung',
        'landschaftsarchitekten','landschaftarchitekten','landschaftsarchitektur','architektur',
        'und','and','bda','partgmbb','partg','mbb','partner','partners'}
    tokens=frozenset(x for x in canonical.split()if len(x)>=4 and x not in descriptors)
    return tokens if len(tokens)>=2 else None

def near(left,right):
    # A short generic word can never create a fuzzy company match. This only
    # flags long, almost identical protected names for exclusion/review.
    return (min(len(left),len(right))>=24 and left[:4]==right[:4]
        and min(len(left),len(right))/max(len(left),len(right))>=.78
        and SequenceMatcher(None,left,right,autojunk=False).ratio()>=.86)

class ProtectedNames:
    def __init__(self,rows,key):
        self.exact=defaultdict(set);self.prefixes=defaultdict(set);self.distinctive=defaultdict(set)
        for row in rows:
            canonical=key(row.get('Firma')or'');name=compact(canonical)
            if not name:continue
            source=str(row.get('id','')).replace('-','');self.exact[name].add(source)
            signature=distinctive(canonical)
            if signature:self.distinctive[signature].add(source)
            if len(name)>=24:self.prefixes[name[:4]].add(name)
    def collision(self,canonical_names,source_id):
        own=str(source_id).replace('-','')
        for canonical in canonical_names:
            name=compact(canonical)
            if not name:continue
            if self.exact.get(name,set())-{own}:return 'exact_name_variant'
            signature=distinctive(canonical)
            if signature and self.distinctive.get(signature,set())-{own}:return 'ambiguous_distinctive_name'
            if len(name)<24:continue
            for other in self.prefixes.get(name[:4],()):
                if self.exact[other]-{own}and near(name,other):return 'ambiguous_near_name'
        return None
