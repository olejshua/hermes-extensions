"""Use provider readiness markers and current advertised input contracts."""

def check_input_schema(schema,args):
    if (not isinstance(schema,dict) or not isinstance(schema.get('properties'),dict)
            or set(schema.get('required',[]))-set(args) or set(args)-set(schema['properties'])):
        raise ValueError('native_mcp_input_schema_mismatch')
    return True


def ready(meeting):
    # A calendar or recording end timestamp never establishes finalisation.
    return meeting.get('finalized') is True and meeting.get('has_transcript') is True
