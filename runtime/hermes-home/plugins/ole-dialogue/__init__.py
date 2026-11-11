"""Standalone native plugin; the overlay is reviewed/installed separately by the operator."""
from pathlib import Path
from .policy import ROLE, role_for_targets
from .runtime import BindingPolicy, Dialogue

class NativeBackend:
    def __init__(self,ctx):
        self.ctx=ctx
    def __getattr__(self,name):
        if name not in ('list_owned','status','cancel','resume','accepted_proof','foreground_started','foreground_finished','item_page','result_read','family_page'):
            raise AttributeError(name)
        plugin=self.ctx._manager._plugins.get('ole-background')
        if plugin is None or not plugin.enabled or plugin.module is None:
            raise RuntimeError('native_background_plugin_unavailable')
        return getattr(plugin.module.service(),name)

    def desktop_identity(self,actor,session_id):
        plugin=self.ctx._manager._plugins.get('ole-background')
        if plugin is None or not plugin.enabled or plugin.module is None:
            raise RuntimeError('native_background_plugin_unavailable')
        return plugin.module.desktop_identity(actor,session_id)

    def desktop_delivery_binding(self,descriptor):
        plugin=self.ctx._manager._plugins.get('ole-background')
        if plugin is None or not plugin.enabled or plugin.module is None:
            raise RuntimeError('native_background_plugin_unavailable')
        return plugin.module.desktop_delivery_binding(descriptor)

    def desktop_transport_allowed(self,transport):
        plugin=self.ctx._manager._plugins.get('ole-background')
        if plugin is None or not plugin.enabled or plugin.module is None:
            return False
        return plugin.module.desktop_transport_allowed(transport)

def register(ctx):
    from hermes_constants import get_hermes_home
    policy=BindingPolicy(ctx.get_config('telegram_user_id'),ctx.get_config('telegram_chat_id'),ctx.get_config('gateway_profile'))
    instance=Dialogue(Path(get_hermes_home())/'ole-dialogue',policy,NativeBackend(ctx))
    from .fast_reads import register as register_reads
    reads=register_reads(ctx,Path(get_hermes_home())/'ole-dialogue',instance)
    from .coding_handoff import register as register_coding_handoff
    register_coding_handoff(ctx,Path(get_hermes_home())/'ole-dialogue',reads)
    from .fast_first import register as register_fast_first
    register_fast_first(ctx,reads)
    desktop=None
    if ctx.get_config('desktop_dialogue_enabled',False) is True:
        from .desktop import DesktopDialogue
        desktop=DesktopDialogue(Path(get_hermes_home())/'ole-dialogue',NativeBackend(ctx),ctx)
        ctx.register_desktop_dialogue(desktop)
        ctx.on_unload(desktop.unload)
    ctx.register_system_prompt_section('ole-dialogue.role',role_for_targets(ctx.get_config('coding_targets',{})),position='after_memory',max_chars=4000)
    ctx.register_skill('dialogue',Path(__file__).parent/'skills/dialogue.md',description='Direkter deutscher Dialog; native dauerhafte Hintergrundaufträge und belegte Rückmeldungen')
    ctx.register_hook('gateway_message_admitted',instance.admitted)
    ctx.register_hook('pre_tool_call',instance.steering.before_tool)
    ctx.register_hook('pre_tool_dispatch',instance.steering.before_tool)
    ctx.register_hook('gateway_ready',instance.ready)
    ctx.register_hook('gateway_shutdown',instance.shutdown)
    ctx.register_hook('gateway_turn_finished',instance.finished)
    ctx.subscribe('hermes:gateway_text_delivery',instance.delivered)
    def handoff(**event):
        return (desktop.handoff(**event) if desktop is not None else None) or instance.handoff(**event)
    ctx.register_tool_handoff('background_job_submit',handoff)
    ctx.on_unload(instance.unload)
    # Test/review inspection only; no public tool accepts a notification target.
    ctx._manager._ole_dialogue_instance=instance
