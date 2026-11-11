"""Add call follow-up drafts and native Stealth targets to the existing phase plan."""
from .followup import DraftAdapter
from .stealth import ContactArchive,ContactFields,NotApplicable,TOOLS


class Phases:
    def __init__(self,base,analysis,binding,directory,followup,stealth_updates,factory,*,enabled,projects):
        self.base=base;self.analysis=analysis;self.binding=binding;self.directory=directory
        self.enabled=enabled and analysis.identity.category in projects
        self.extra=dict(getattr(base,'extra',{}))
        self.followup=DraftAdapter(analysis.identity,followup,directory)if followup is not None and self.enabled else None
        if self.followup:self.extra['call_email_followup']='analysis'
        self.archive=(ContactArchive(factory,analysis,binding)if self.enabled and analysis.identity.category=='stealth'and binding.get('stealth_crm_verified') else None)
        self.updates=stealth_updates
    def __call__(self,name):
        if name=='call_email_followup':return self.followup
        if self.analysis.identity.category=='stealth'and name in ('notion','twenty_note','twenty_link','twenty_status'):
            if not self.archive:return None
            if name=='notion':return self.archive
            if name in ('twenty_note','twenty_link'):return NotApplicable()
            return ContactFields(self.archive,self.updates)
        return self.base(name)if callable(self.base)else self.base.get(name)
