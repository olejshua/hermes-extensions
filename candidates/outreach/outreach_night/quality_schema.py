"""Only a typed same-run quality commit; no external write tool parameters."""
def schema(identity,candidate):
    properties={'contract':{'type':'string','enum':['outreach-judgement-v1']},
        'identity':{'type':'object','enum':[identity]},'candidate':{'type':'object','enum':[candidate]},
        'decision':{'type':'string','enum':['ready','rejected','review']},'subject':{'type':'string'},
        'body':{'type':'string'},'html_body':{'type':['string','null']},
        'template_source_id':{'type':'string'},'template_sha256':{'type':'string'},
        'evidence':{'type':'array','items':{'type':'object','properties':{'source_id':{'type':'string'},
            'quote':{'type':'string'}},'required':['source_id','quote'],'additionalProperties':False}},
        'proposed_slots':{'type':'array','items':{'type':'string'}}}
    return {'name':'outreach_quality_commit','description':'Commit your exact source-bound Outreach DRAFT judgement from the current complete native wire. Saves only private result, never creates/sends/schedules mail.',
            'parameters':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}}
