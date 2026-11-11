# Hermes Extensions

Custom source from a self-hosted Hermes Agent installation, captured on 2026-11-11. It includes locally committed fixes, the additional changes to the running core, and the installed extension code.

| Part | Location |
| --- | --- |
| Hermes core, Telegram and desktop session behavior | [Core patch](patches/hermes-agent.patch) |
| Native plugins, conversation routing and background jobs | [Hermes home](runtime/hermes-home/) |
| Mail drafts, calendar reservations, call briefings and CRM | [Scripts](runtime/hermes-home/scripts/) and [GTM package](runtime/service-packages/gtm/) |
| Outcome monitoring and bounded automated repair | [Observer](runtime/service-packages/reply-observer/) and [Repair](runtime/service-packages/self-repair/) |
| Memory, retrieval, Dreaming and source refresh | [Second Brain](runtime/service-packages/brain/) |

The patch applies to [Nous Research Hermes Agent at 28e6496](https://github.com/NousResearch/hermes-agent/commit/28e6496a5e3adfea57bebfc9571b981bff378523). Apply it in a separate checkout of that revision with git apply --check and then git apply, using the absolute path to patches/hermes-agent.patch.

This is a source snapshot. The services use separately provisioned Python environments, user service units and private configuration. Supply your own provider authorization, approved sources, calendars and CRM bindings. The example.invalid addresses, example IDs and sample room reservation do not point to live resources. Configure and test each feature before enabling scheduled jobs or repair publication.

The snapshot excludes credentials, data stores, user profiles, conversation history, installed dependencies, old release copies and one-off private migrations. Personal values embedded in source have been replaced. Some module and configuration names retain the installation's project labels.

Upstream copyright and license notices are in [LICENSES](LICENSES/). Parts of the memory integration draw on [Hermes x Jasper](https://github.com/JasperKallfelz/hermes-x-jasper). Existing upstream license terms are preserved. This snapshot does not grant a new blanket license over other code.
