# Final metadata-aware distribution check

Both unreleased 1.0.0 distributions were rebuilt from a clean archive of
`bad3c550fdd09e149658be864399259cd9188ac3`, after README changed package metadata.
The older `9807a41` distributions and their review remain separate historical
preparation evidence; their artifact identities do not cover these new packages.

Build and twine succeeded. Both new packages passed eleven checks each outside
the checkout (22 total), with the llm/sandbox/pi/dsh extras installed. The proof
records exact artifact hashes and 261 matching Python sources per artifact.
All 293 wheel package payload files, including non-Python assets, are byte-identical
to the original preparation wheel. The wheel metadata body and sdist README match
the new source bytes. Raw installed-check logs are retained beside their hashes.

Local preparation helper failures are preserved separately: the system Python
lacked tarfile's filter argument; copying a uv-managed Python binary broke its
relative dylib lookup (fresh symlink-based environments succeeded); and a legacy
email parser replaced non-ASCII README characters (a raw UTF-8 byte comparison
succeeded). These did not require package runtime changes or provider calls.

These checks establish preparation package behavior. Current full live smoke,
main PR CI/Harbor and later exact-tag distribution gates remain separate.

The [independent readback](independent-distribution-and-docs-closure.json) accepts
the new artifact identities, all 293 package files, 298 wheel RECORD entries,
installed source/metadata/direct_url identities, all 22 check records and 20 raw
logs. It also closes the two documentation corrections, with no new execution.
