# Hermes Extensions

Custom source from a self-hosted Hermes Agent installation, captured on 2026-11-11. It includes locally committed fixes, the additional changes to the running core, and the installed extension code.

| Part | Location |
| --- | --- |
| Hermes core, Telegram and desktop session behavior | [Core patch](patches/hermes-agent.patch) |
| Native plugins, conversation routing and background jobs | [Hermes home](runtime/hermes-home/) |
| Mail drafts, calendar reservations, call briefings and CRM | [Scripts](runtime/hermes-home/scripts/) and [GTM package](runtime/service-packages/gtm/) |
| Outcome monitoring and bounded automated repair | [Observer](runtime/service-packages/reply-observer/) and [Repair](runtime/service-packages/self-repair/) |
| Memory, retrieval, Dreaming and source refresh | [Second Brain](runtime/service-packages/brain/) |
| Daily Acribic preparation and dispatch code | [Daily package](runtime/service-packages/acribic-daily/) |
| Google Workspace helpers | [Workspace scripts](runtime/hermes-home/skills/productivity/google-workspace/scripts/) |

The patch applies to [Nous Research Hermes Agent at 28e6496](https://github.com/NousResearch/hermes-agent/commit/28e6496a5e3adfea57bebfc9571b981bff378523). Apply it in a separate checkout of that revision with git apply --check and then git apply, using the absolute path to patches/hermes-agent.patch.

The first commit is the captured source snapshot. The services use separately provisioned Python environments, user service units and private configuration. Supply your own provider authorization, approved sources, calendars and CRM bindings. The example.invalid addresses, example IDs and sample room reservation do not point to live resources. Configure and test each feature before enabling scheduled jobs or repair publication.

The snapshot excludes credentials, data stores, user profiles, conversation history, installed dependencies, old release copies and one-off private migrations. Personal values embedded in source have been replaced. Some module and configuration names retain the installation's project labels.

Upstream copyright and license notices are in [LICENSES](LICENSES/). Parts of the memory integration draw on [Hermes x Jasper](https://github.com/JasperKallfelz/hermes-x-jasper). Existing upstream license terms are preserved. This snapshot does not grant a new blanket license over other code.

## Preparing a server release

This repository is the source of truth for new Hermes code. Public source and the private host profile are independent. The running installation never pulls directly into its working files.

On the enrolled host, use the installed hermes-source-release command:

    hermes-source-release status
    hermes-source-release prepare FULL_REVIEWED_COMMIT_ID

Preparation fetches the exact published commit, applies the upstream patch and the private host profile, compiles the Python source, and writes a private plan with only the source changes needed. It stops on live drift, profile conflicts, and attempts to overwrite unmanaged files. It does not start, stop, or write live service code.

Use the plan's payload as source for the existing publisher for the affected component. The publisher retains its own approval, idle checks, activation, and rollback. Before activating, verify its actual source inputs:

    hermes-source-release verify FULL_REVIEWED_COMMIT_ID --native-inputs /private/path/native-inputs.json

The input is a JSON array of objects with target and source fields. Each source is the absolute path that the native publisher will actually read for that target. The verifier checks the exact target set and bytes. Publisher-generated binding metadata, backups and private service configuration remain with the native publisher. Keep the verified candidate and publisher plan fixed until activation.

After the native publisher succeeds, record the verified installed release:

    hermes-source-release accept FULL_REVIEWED_COMMIT_ID

Acceptance requires live byte equality for the complete managed set. A failed or partial activation stays unaccepted. Source removals, dependency changes and service configuration migrations still need the component's reviewed migration procedure. There is no automatic deployment from a push.

Automatic result recovery still runs. Repair-generated code is reviewed and staged privately before going through this Git release path.
