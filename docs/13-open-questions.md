# Open questions

Decisions genuinely not yet made. If a build task depends on one of these, stop and ask rather than guessing — a wrong guess here (especially the labor-tracking one) can mean reworking a schema after real data has been entered against it.

## Unresolved

- **Labor/harvester-level productivity tracking.** Harvest work is commonly piece-rate in this industry (paid per bunch or per kg), but the current design only tracks which administrative PIC Block user is responsible for a block, not which individual harvester did the physical work. `HarvestTree.recorded_by` (`03-data-model.md`) captures who *logged* an entry, which may or may not be the same person as who *harvested* it, depending on how crews are actually organized. Building this either way (adding a `harvester` field, or explicitly deciding it's out of scope) needs a direct answer first — don't assume.

## Resolved — recorded here so they aren't re-asked

- **Certification/compliance tracking** — needed. See `07-compliance.md`.
- **Model validation/retraining ownership** — owned directly by the project owner, not a dedicated ML-ops team. See `06-ml-pipeline.md` § Model governance.
- **Downstream/mill connection** — explicitly out of scope for now; system scope ends at harvest logged in the field. `HarvestBatch` (`07-compliance.md`) is the seam for later, not something to build now.
- **Field staff device capability** — confirmed: capable smartphones already available, so the offline PWA approach doesn't carry a hidden hardware-procurement cost.
- **Tree-level vs. block-level tracking** — both, explicitly. Operational data (health, yield) is tree-level; compliance/traceability data is block-level, because that's the unit certification schemes actually audit against. Not a conflict — see `07-compliance.md` § Block-level compliance, tree-level operations.
- **Language/framework choice (Python vs. staying on Laravel)** — Python/Django chosen. The deciding factors were GeoDjango's fit for the geospatial requirements and the need to build a real API + offline layer regardless of language — not a belief that Python is inherently superior to PHP for this kind of application.
