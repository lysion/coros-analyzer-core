# Release scope notes

There are no known release blockers for the current public-core v0.1 scope.

## Future scope note: Garmin FIT SDK

The private parent repository declares `garmin-fit-sdk` as a dependency, but
its exact official license text and any notice obligations were not verified in
the readiness audit. The current public-core snapshot neither depends on nor
bundles `garmin-fit-sdk`, Garmin FIT SDK code, or generated FIT profile/schema
material; this is therefore not a blocker for this release scope.

Before a future public scope adds that dependency or material, verify and
record the exact version, official license text, compatibility, and required
notices.
