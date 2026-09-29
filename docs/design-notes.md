# Design notes

## The argument this completes

The design compares four patterns: central canonical repository (A), federated common-model repositories (B), metadata-mediated federation (C) and source-native preservation with materialised views (D). It finds that C is strongest on fidelity, lock-in and provenance, and weakest on query performance, resilience and population analytics. It concludes that C and D are complements: C supplies the control plane across everything, and D supplies the performance and resilience C lacks, *through views that C itself catalogues*.

The three earlier demonstrators built C. This one adds D in the form the design specifies, and puts the two side by side so the trade-off can be seen rather than asserted.

## A view is a derived source

| Rule in the design | How it is enforced here |
|---|---|
| Each view is registered in the catalogue as a source in its own right | `summary-view` is registered through a change request from an onboarding pack |
| Its derivation is recorded, with lineage back to the originals | The view is built through the broker; every stored statement keeps its source and record identifier; lineage checks (L1, L2) run at every build and in conformance (C10) |
| A view is never authoritative unless governance says so | `derived: true`; its authority must say "not authoritative" (C9); every served record says so |
| Canonical views need a named use case, owner and cost case (P5) | The `materialisation` policy (sponsor, use case, triggers, refresh, maximum age, review date) is mandatory (C8) |
| Every materialised view carries a refresh policy, a staleness indicator on each result, and a review date | `freshness` on every served record; *rebuild required* by age or catalogue change |
| A view must not be federated with its originals | The broker excludes derived sources from federated planning |

## Three serving modes

**Federated** is unchanged from the earlier demonstrators.

**Materialised** serves the stored record exactly as built. It carries its build time, age, the catalogue version it was built under, and an alert saying it is derived and blind to anything since. Nothing is re-reconciled: what the clinician sees is what the broker concluded at build time.

**Hybrid** asks every source live. For any source that fails (unavailable, timed out, errored), it adds that source's statements from the last build, marked `stale`, and *then* reconciles. Live sources are never replaced by view content. So the flags that depend on the missing source come back, each with a note giving the age of the evidence:

- Entries built partly from stale statements are flagged `includes-stale`.
- Flags that assert absence (*not on the GP list*, *not dispensed recently*) note the age of the GP or pharmacy evidence they were judged against.
- The source is reported as `stale`, not `complete` and not `unavailable`.

This is a deliberate extension of the rule from the second demonstrator. Absence needs a witness; in hybrid mode the witness may be old, and the record says how old.

## Freshness is measured, not assumed

Three kinds of staleness are distinguished:

| Kind | Detected by | Shown as |
|---|---|---|
| Age | Build time against the policy's maximum age | *Rebuild required: older than the 24 h refresh policy* |
| Catalogue drift | Build version against the current version | *Rebuild required: built under v2; the catalogue is now v3*, and a warning in impact analysis before the change is applied |
| Clinical change at the sources | Re-assembling live and comparing with the view | *missing from view* / *outdated in view* |

The third is the one that matters clinically, and it cannot be known without asking the sources, which is exactly the cost the view exists to avoid. So it is checked on demand, per person, and is framed as a comparison, not a guarantee.

## Population queries

A population question ("which patients have a medication needing review") is the design's *analytical shape* trigger. Answered from the view, it takes milliseconds and states the age of its oldest entry. Federated, it would mean asking every source about every person. The demonstrator answers it from the view only, and says so.

## Keeping clinical content out of the control plane

The control-plane store holds catalogue versions, change requests, governance events and the record-request log, and no clinical values. The materialised view necessarily holds clinical content, so it has its own store. This keeps the design's line between control plane and data plane (P12: move control, not content) visible in the file layout.

## Natural next steps

1. **Scheduled and change-triggered rebuilds.** Honour the refresh policy automatically, and rebuild on catalogue change as part of applying a change request.
2. **Incremental rebuild from source change notifications**, so only affected people are rebuilt.
3. **Real servers**: HAPI FHIR, EHRbase and Postgres under Docker Compose, in place of the mocks.
4. **A patient-facing view** over the same record, with an access log drawn from the audit trail.
5. **A second, analytical view** (for example OMOP-shaped) for secondary use, reached only under the research purpose through pseudonymous identity.
