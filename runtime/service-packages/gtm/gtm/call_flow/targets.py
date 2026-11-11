"""Project-specific targets; known Stealth sources never become guessed archive parents."""
STEALTH_SOURCE_IDS = ('00000000-0000-4000-8000-00000000000c','00000000-0000-4000-8000-00000000000d')


def requirements(identity,phase,default):
    if identity.kind=='call' and identity.category=='stealth' and phase=='monolith':return ('analysis',)
    return default


def target_review(category,binding=None):
    if category=='stealth' and (binding or {}).get('stealth_crm_verified'):
        return dict(notion_archive={'state':'bound_existing_stealth_contact'},crm={'state':'bound_existing_stealth_contact'},twenty={'state':'not_applicable'})
    if category!='stealth':return None
    return dict(notion_archive=dict(state='target_review',selection_required='Exact approved Stealth Campus transcript archive parent page ID and existing native account/reader/writer route; contact datasources are not an archive parent.',known_contact_datasources=list(STEALTH_SOURCE_IDS)),
                crm=dict(state='crm_review',selection_required='Existing Stealth Notion contact/lead record, or explicitly reviewed existing Twenty person/company IDs. No migration, duplicate creation or name/domain guess.'))
