# Core artifact verification

`core-lock.json` is the consumer-side trust root. It pins the actual Core development producer run and wheel; it does not assert that Core `0.7.0.dev1` or Telegram `0.1.0.dev1` is a stable release. The Actions artifact has an expiry date. A consumer repository's default `GITHUB_TOKEN` has not yet been verified to download it across repositories; a denied or expired download stops CI before pip runs.

The version 2 lock records repository and workflow IDs, the run ID and attempt, event, run head, checkout commit and tree, artifact ID, name, archive digest, size and expiry, and the wheel and source distribution names and digests. GitHub's pull-request run head and the commit checked out by that run can differ. The fetcher checks the run head against run and artifact records, and checks the checkout independently against the reusable packaging workflow reference and the Git commit API's tree. It then verifies the downloaded archive bytes, its source manifest, checksums and both distribution hashes. The downloaded manifest is not the trust root.

CI's first package step uses the consumer's own default token to fetch and verify that exact artifact. A 403 or other HTTP denial reports the status without printing response bodies or tokens. There is no floating or `latest` fallback. This step is the consumer-token access probe; a successful local download with another credential is not evidence that it will pass.

For a release asset, the fetcher checks the release asset record, resolves the tag to the recorded source commit, and verifies the pinned wheel digest and metadata. The release tag, version and URL must refer to a real independently verified release. No future URL is inferred.

Before a stable Telegram release, update the Core dependency in `pyproject.toml`, runtime compatibility constant, artifact lock and the verified installer/container inputs together. Re-run the download, clean wheel and sdist installs, runtime checks, and CI against the actual release artifact.
