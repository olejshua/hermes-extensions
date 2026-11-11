# Hermes source and release rules

- Use this repository for new Hermes source changes. Commit reviewed code here before preparing a server release. Treat old local checkouts and deployment bundles as references.
- Keep credentials, profiles, configuration, real fixtures, logs, and job or conversation data out of Git. The server applies its private profile after fetching the reviewed commit. Never copy that profile into this checkout.
- Prepare an exact commit from the published branch, verify its bytes against the native publisher inputs, and use the existing component publisher with its normal approval and readiness checks. Accept a release only after installed source readback. Do not update the live checkout with a pull or overwrite private configuration.
- Keep normal outcome recovery enabled. Code proposed by automated repair stays private until reviewed, stripped of case data and committed here. A candidate is not a release.
- Preserve the running service configuration, queued work and external write permissions. Roll back code through the component publisher without rolling back application data.
