"""Energy Atlas collector base URLs (ADR-0019 §2). The only hostnames in this public repo."""

BASE_URLS = {
    "staging": "https://atlas-api-staging.datamindz.io",
    "production": "https://atlas-api.datamindz.io",
}

# Public by design (ADR-0019 amendment T-411 §B1): a phone-only owner cannot mint and
# paste a secret, so the enrollment credential ships baked into this public release
# instead of a form field. Every credential is a `public` one (§A): multi-use, capped
# at ATLAS_ENROLLMENT_PUBLIC_SITES_PER_DAY new sites/24h collector-side, revocable
# without breaking already-registered sites. `None` = this release cannot register on
# that environment yet (§B2 `environment_unavailable`). Rotation is by release: a new
# credential ships in a new version; the old one is revoked only after that release has
# been out >= 30 days (§B "Rotation").
ENROLLMENT_SECRETS = {
    "staging": "eac_68a1270d-2914-46e4-8a23-61b2bb92f118_0Bb3reWp5UbCqPAwQTtbmP1zGx7640-GVVC13k4l5_A",
    "production": None,
}
