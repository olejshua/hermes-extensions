"""Narrow automatic publication surface, checked independently of model output."""
from __future__ import annotations
import ast
import hashlib
from pathlib import Path

EDITABLE = {
    'gtm/calendar/runtime/calendar_routing.py': {'owned_business_source'},
    'gtm/calendar/runtime/mail_policy.py': {'format_body'},
    'gtm/calendar/runtime/mail_workflow.py': {
        'UNDERSTAND', 'COMPOSE', 'validate_understanding', 'calendar_facts', 'validate_draft'},
    'gtm/calendar/runtime/email_realtime.py': {
        'unquoted_sender_text', 'notice_text', 'notice_party', 'notice_summary', 'reply_signature'},
    'gtm/briefing.py': {'render_briefing'},
    'gtm/briefing_daemon.py': {
        'PROMPT', 'original_quote_span', 'optional_opener', 'validate_output',
        'latest_own_addressing', 'unquoted_top_body'},
}
DENIED_NAMES = {
    'open','exec','eval','compile','__import__','globals','locals','vars','getattr',
    'setattr','delattr','input','breakpoint','help','exit','quit','memoryview',
    'os','sys','subprocess','Path','pathlib','sqlite3','importlib','shutil','socket',
    'requests','httpx','urllib','tempfile','Runtime','Store','CLI','CORE','HOME',
    'DATA','w','native_worker','native_worker_command','model_json','notify',
    'remember_case','save','case_path','read_case',
}
PURE_BUILTINS = {
    'abs','all','any','bool','dict','enumerate','float','int','isinstance','len','list',
    'max','min','next','range','reversed','round','set','sorted','str','sum','tuple','type','zip',
    'ValueError','TypeError','KeyError','IndexError','timedelta',
}
PURE_METHODS = {
    'get','values','items','keys','pop','append','extend','insert','add',
    'strip','rstrip','lstrip','lower','upper','casefold','split','rsplit',
    'splitlines','join','replace','count','find','rfind','index','startswith','endswith',
    'date','weekday','isoformat','total_seconds','astimezone','strftime','group',
    'search','match','fullmatch','findall','finditer','escape','sub','loads','dumps','category','start',
}

class PolicyError(ValueError): pass

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _key(node, index):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
        return node.targets[0].id
    return '@'+str(index)

def _dump(node): return ast.dump(node, include_attributes=False)

def check_source(relative, before, after):
    """Freeze executable wrappers, account rules, API writes, grants and imports.

    Edits may change the listed interpretation/rendering functions. A candidate
    that needs another surface remains a review request; it never widens itself.
    """
    if relative not in EDITABLE: raise PolicyError('file_outside_automatic_scope')
    original, candidate = ast.parse(before), ast.parse(after)
    module_names=set()
    for statement in original.body:
        targets=(statement.targets if isinstance(statement,ast.Assign) else [statement.target]
                 if isinstance(statement,(ast.AnnAssign,ast.AugAssign)) else [])
        module_names.update(n.id for target in targets for n in ast.walk(target) if isinstance(n,ast.Name))
    module_names.update(n.name for n in original.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)))
    for statement in original.body:
        if isinstance(statement,(ast.Import,ast.ImportFrom)):
            module_names.update(a.asname or a.name.split('.')[0] for a in statement.names)
    left = [(_key(n,i),n) for i,n in enumerate(original.body)]
    right = [(_key(n,i),n) for i,n in enumerate(candidate.body)]
    if [k for k,_ in left] != [k for k,_ in right]: raise PolicyError('module_structure_changed')
    changed=[]
    for (name,old),(_,new) in zip(left,right):
        if _dump(old)==_dump(new): continue
        if name not in EDITABLE[relative]: raise PolicyError('protected_execution_surface_changed')
        if isinstance(old,ast.Assign):
            # Prompt expressions are strings combined with the unchanged RULES.
            if any(isinstance(n,(ast.Call,ast.Attribute,ast.Subscript,ast.Lambda)) for n in ast.walk(new)):
                raise PolicyError('executable_prompt_expression')
            if any(isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id!='RULES' for n in ast.walk(new)):
                raise PolicyError('unexpected_prompt_dependency')
        elif isinstance(old,ast.FunctionDef) and isinstance(new,ast.FunctionDef):
            if _dump(old.args)!=_dump(new.args): raise PolicyError('function_signature_changed')
            if [_dump(n) for n in old.decorator_list]!=[_dump(n) for n in new.decorator_list]:
                raise PolicyError('function_decorator_changed')
            if (old.returns is None)!=(new.returns is None) or (old.returns and _dump(old.returns)!=_dump(new.returns)):
                raise PolicyError('function_annotation_changed')
            imports={_dump(n) for n in ast.walk(old) if isinstance(n,(ast.Import,ast.ImportFrom))}
            nested={_dump(n) for n in ast.walk(old) if isinstance(n,(ast.FunctionDef,ast.Lambda)) and n is not old}
            calls={n.func.id for n in ast.walk(old) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}|PURE_BUILTINS
            for node in ast.walk(new):
                if isinstance(node,(ast.FunctionDef,ast.Lambda)) and node is not new and _dump(node) not in nested:
                    raise PolicyError('nested_callable_in_pure_function')
                if isinstance(node,(ast.Import,ast.ImportFrom)) and _dump(node) not in imports:
                    raise PolicyError('new_import_in_pure_function')
                if isinstance(node,ast.Name) and (node.id in DENIED_NAMES or node.id.startswith('__')):
                    raise PolicyError('unsafe_name_in_pure_function')
                if isinstance(node,ast.Attribute) and node.attr.startswith('_'):
                    raise PolicyError('private_attribute_in_pure_function')
                if isinstance(node,ast.Attribute) and node.attr in DENIED_NAMES|{'modules','builtins','f_globals','f_locals','gi_frame','ag_frame','tb_frame'}:
                    raise PolicyError('unsafe_attribute_in_pure_function')
                if isinstance(node,ast.Name) and isinstance(node.ctx,(ast.Store,ast.Del)) and node.id in calls|module_names:
                    raise PolicyError('callable_or_global_rebinding')
                if isinstance(node,ast.ExceptHandler) and node.name in calls|module_names:
                    raise PolicyError('callable_or_global_rebinding')
                if isinstance(node,(ast.Global,ast.Nonlocal,ast.ClassDef,ast.AsyncFunctionDef)):
                    raise PolicyError('unexpected_stateful_definition')
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id not in calls:
                    raise PolicyError('new_callable_in_pure_function')
                if (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr not in PURE_METHODS
                        and not (node.func.attr == 'now' and isinstance(node.func.value, ast.Name)
                                 and node.func.value.id == 'datetime')):
                    raise PolicyError('unsafe_method_in_pure_function')
                if isinstance(node,ast.Call) and not isinstance(node.func,(ast.Name,ast.Attribute)):
                    raise PolicyError('indirect_callable_in_pure_function')
                if isinstance(node,(ast.Attribute,ast.Name)) and isinstance(node.ctx,(ast.Store,ast.Del)) and isinstance(node,ast.Attribute):
                    raise PolicyError('attribute_assignment_in_pure_function')
        else: raise PolicyError('statement_type_changed')
        changed.append(name)
    if not changed: raise PolicyError('no_code_change')
    return changed

def changed_files(before, after):
    before,after=Path(before),Path(after)
    result={}
    def visible(root):
        found=set()
        for p in root.rglob('*'):
            rel=p.relative_to(root)
            if any(x in {'.git','.tmp','.pytest_cache','__pycache__'} for x in rel.parts):continue
            if p.is_symlink():raise PolicyError('symlink_candidate')
            if p.is_file():
                if p.stat().st_size>1_000_000:raise PolicyError('unbounded_candidate_file')
                found.add(str(rel))
        return found
    files=visible(after);old=visible(before)
    for rel in sorted(files|old):
        a,b=before/rel,after/rel
        if a.is_symlink() or b.is_symlink(): raise PolicyError('symlink_candidate')
        if rel=='repro.py': continue  # Isolated proof only; never published.
        if not a.is_file() or not b.is_file(): raise PolicyError('file_addition_or_deletion')
        if a.read_bytes()!=b.read_bytes():
            result[rel]=check_source(rel,a.read_text(),b.read_text())
    if not result: raise PolicyError('no_publishable_change')
    return result
