# Release blockers

## Garmin FIT SDK license verification

The private parent repository declares `garmin-fit-sdk` as a dependency, but
its exact official license text and any notice obligations were not verified in
the readiness audit. This public-core staging snapshot does not depend on or
bundle `garmin-fit-sdk`.

Do not add Garmin FIT SDK code, generated profile/schema material, or that
dependency to a public release scope until the exact version, official license
text, compatibility, and required notices have been verified and recorded.
