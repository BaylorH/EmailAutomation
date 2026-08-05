# B1 Verified Opt-Out Contact Identity Binding Amendment

**Status:** Approved by two independent reviewers

**Goal:** Cryptographically bind every newly verified B1 hard opt-out to the
exact and canonical user-scoped mailbox identities that B2-C may suppress,
without persisting a raw mailbox or wiring B2 into production runtime.

**Depends on:**

- `docs/superpowers/specs/2026-08-02-shared-exact-source-coordinator-design.md`
- `docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`
- B2-B exact-SHA milestone
  `48a23dbf31e2b3c04f8e745239768f6f264c9e0b`

**Deliverable:** both (provider-free B1/B2 bridge code and production-clearance
evidence)

## Blocking defect

B1 currently proves that a deterministic hard opt-out was verified, but its
three-field evidence object contains only `schemaVersion`, `evidenceKind`, and
`evidenceHash`. The selected contact-opt-out candidate and B2 `B1Link` retain
that evidence hash, but no mailbox identity hash. B2-C would therefore have to
trust a caller-supplied mailbox beside otherwise valid B1 authority. That could
pair contact A's verified opt-out with contact B's mailbox and mint priority-3
suppression for B.

B2-C implementation is blocked until this amendment is green and published.

## Frozen solution

### Trust and derivation boundary

1. The injected B1 hard-opt-out verifier remains the sole authority that may
   classify deterministic evidence as a hard opt-out. Its callback contract
   remains the exact version-1 three-field evidence object; callers cannot
   submit a candidate, winner, mailbox hash, or augmented evidence object.
2. The verifier receives the existing deeply frozen complete classification
   input. Only after it returns a valid non-local hard-opt-out proof does B1
   derive contact identity from the exact
   `classification_input["message"]["from"]` value already covered by
   `classificationInputHash`.
3. Only a newly claimed hard-opt-out snapshot and an existing version-2 retry
   require this identity shape. Ordinary model classification, a verifier
   returning `None`, local source-policy evidence, and exact replay of an
   existing version-1 hard snapshot retain their historical inputs. On the v2
   path, `classification_input["canonicalSourceId"]` must equal the method's
   exact canonical source ID, and the message object and `from` value must be
   exact JSON mappings/strings. Missing, malformed, control-bearing,
   over-bound, or wrong-source identity input is a configuration failure with
   zero writes and zero model calls. Raw case and surrounding-whitespace
   variants are normalized, not rejected.
4. B1 independently implements the already approved B2 mailbox and user-scope
   hash algorithm. It must not reuse B1's existing canonical-JSON or control
   helpers because they intentionally differ: the contact helper uses
   `ensure_ascii=False`, rejects every Unicode category beginning with `C`,
   limits the exact verified user ID to 512 UTF-8 bytes, and limits the
   normalized mailbox to 320 UTF-8 bytes. It does not import
   `row_authority.py`, and B2 does not import `source_coordinator.py`.
   Hard-coded independent vectors and cross-module parity tests make drift fail
   closed.
5. Raw verified user IDs and raw mailbox identities are never added to the B1
   classification snapshot, B2 link, evidence file, logs, or Brain. Only
   complete lowercase SHA-256 hashes persist.

### Exact identity algorithm

The input mailbox is Unicode-NFC normalized, stripped of surrounding Unicode
whitespace, lowercased with Python `str.lower()`, and NFC-normalized again. It
must contain exactly one `@`, nonempty local/domain parts, no Unicode control
characters, valid UTF-8, and at most 320 UTF-8 bytes. The canonical mailbox
removes the first `+` and everything after it from the local part. It does not
remove dots or apply domain-specific rules.

The user scope is:

```text
sha256(
  b"sitesift.user.scope.v1\0" +
  canonical_json({"verifiedUserId": verified_user_id})
)
```

Each exact/canonical identity hash is:

```text
sha256(
  b"sitesift.contact.identity.v1\0" +
  canonical_json({
    "normalizationVersion": "sitesift-mailbox-v1",
    "normalizedMailboxIdentity": normalized_identity,
    "schemaVersion": 1,
    "userScopeHash": user_scope_hash
  })
)
```

This contact-only canonical JSON is UTF-8, key-sorted, compact,
`ensure_ascii=False`, and non-ASCII-preserving, exactly as frozen by B2; B1's
existing general canonical helper is not reused. The exact verified user ID is
an exact control-free string of 1–512 UTF-8 bytes. The exact identity is the
normalized full mailbox; the canonical identity is the plus-stripped mailbox.
When no plus tag exists the two hashes are equal.

### Bound deterministic evidence

The hard-opt-out verifier still returns:

```text
{
  schemaVersion: 1,
  evidenceKind: nonempty non-local code,
  evidenceHash: h64
}
```

For every newly persisted verified hard opt-out, B1 transforms that trusted
result into this exact nested evidence object before building the deterministic
proposal or snapshot:

```text
{
  schemaVersion: 2,
  evidenceKind: nonempty non-local code,
  evidenceHash: h64,
  exactIdentityHash: h64,
  canonicalMailboxIdentityHash: h64
}
```

`deterministicEvidenceHash` is the existing B1 canonical JSON hash of the full
five-field object. The deterministic contact-opt-out candidate's
`evidenceHash`, the selection/owner/ledger chain, and the B2
`hardOptOutEvidenceHash` all bind that same full-object hash. Extra, missing,
mistyped, uppercase, or swapped identity fields are invalid.

Version-1 non-local three-field evidence is a legacy unbound form. When an
existing snapshot contains that exact form, B1 validates the historical input
hash and strict verifier result, reconstructs v1 without requiring or deriving
a mailbox, and compares the immutable snapshot byte-for-byte. It may
reconstruct the legacy B1 link, but B2-C may not consume that link. New claimed
hard opt-outs always persist version 2; existing v2 retry re-derives both
identity hashes. Exact retry never rewrites version 1 into version 2; legacy
evidence is a B2-D migration-review blocker, not an implicit backfill. Local
source-policy evidence remains exact version 1 and cannot elect hard opt-out.

### B1Link amendment

The existing exact v1 `B1Link` shape and
`sitesift.row.b1_authority_link.v1` hash domain remain byte-compatible for
legacy contact evidence and all `terminal|human_decision` links. A new exact v2
contact-only shape adds two mandatory non-null keys:

```text
exactIdentityHash: h64
canonicalMailboxIdentityHash: h64
```

The v2 domain is `sitesift.row.b1_authority_link.v2`. A v2 link requires
`ownerKind == contact_optout`, non-null `hardOptOutEvidenceHash`, and both
identity hashes. The B1 builder validates exact bound version-2 deterministic
evidence, requires the selected candidate and work-ledger payload to bind its
full evidence hash, and copies both identity hashes. New terminal/human and
legacy-unbound contact links continue to emit the unchanged v1 shape/domain;
v1 material with v2 keys or v2 material under the v1 domain is invalid.

The public `build_b1_authority_link(...)` signature does not change because the
identity authority is derived from the stored classification document. No API
accepts caller-supplied contact hashes. Generic link validation discriminates
the two exact shapes. Existing direct B1 claims and legacy row evidence remain
byte-compatible; B2-C contact-fanout claims, contact settlements, and their
source-settlement links require and preserve v2. The existing private
`contact_fanout` claim planner rejects a v1 contact link before deriving a
request ID or planning any row mutation.

### B2-C consumption rule

A verified contact opt-out transition must derive its exact and canonical
identity solely from the stored amended B1 link. Its optional raw mailbox input
may be used only to reproduce and compare those hashes; it can never select or
replace them. The contact settlement's `exactIdentityHash`, canonical document
path, `authorityLinkHash`, and `hardOptOutEvidenceHash` must equal the amended
link. Any missing legacy field or mismatch is a zero-write conflict.

The normative B2-C contact-compliance amendment adds one exact already-active
case. When a different valid v2 B1 opt-out arrives while that canonical contact
is already active, B2-C appends no contact settlement or row epoch. Instead its
immutable `already_active` transition receipt must copy this new link's exact
and canonical identity hashes, authority-link hash, and hard-opt-out evidence
hash; atomically validate the existing active settlement, its own creating
receipt, contact head, aliases, and current apply fan-out; and point to that
existing contact generation/settlement/fan-out. That receipt is first-class B2
completion evidence for the later B1 source. It never authorizes a row
source-settlement link for the later B1 link because that link did not create a
row generation. All other verified opt-out transitions retain the contact
settlement equality rule above.

Authenticated release continues to derive authority from the exact active
contact settlement/head and authenticated actor scope. It cannot introduce a
new mailbox binding.

## Compatibility and containment

- This amendment changes no provider client, route, worker, frontend,
  Firestore rule, deployment, environment flag, campaign, or production data.
- B1 production wiring still has no hard-opt-out verifier; B4 owns the reviewed
  real verifier adapter. Static construction/inventory tests must prove that
  absence before publication. Therefore this milestone cannot send, suppress,
  or mutate a live contact.
- Existing provider-free B2 documents have not been deployed. The v2 contact
  link is introduced before B2 runtime adoption without rewriting v1
  terminal/human claim evidence.
- B1 classification document schema version stays 1; only the nested verified
  hard-opt-out evidence is versioned to 2. Legacy unbound evidence remains
  readable but unusable by B2 contact authority.
- `source_coordinator.py` remains the only B1 writer. `row_authority.py` remains
  standard-library-only, runtime-unwired, and read-only toward every B1
  collection.

## Acceptance and refutation

The amendment is accepted only if hermetic evidence proves:

1. A trusted hard-opt-out proof over an exact frozen sender persists the exact
   version-2 evidence and independent expected exact/canonical hash vectors.
2. Case, Unicode, whitespace, and plus-tag normalization match B2 exactly;
   non-ASCII JSON is unescaped, Unicode `Cf` is rejected, a 513-byte user ID is
   rejected, cross-user hashes differ, and no raw identity persists.
3. Changing the sender, source ID, evidence, exact hash, canonical hash, or
   user scope cannot replay or mint contact authority.
4. Invalid sender identity after verifier approval produces zero writes and
   zero model calls; verifier failure remains ambiguous and fail-closed.
5. Legacy version-1 hard-opt-out evidence and v1 links remain byte-exact
   immutable history but are rejected by B2-C contact transitions.
6. Terminal/human and legacy contact links retain the exact v1 shape/domain;
   only a newly verified contact link uses v2 and carries the two identity
   fields plus hard-evidence hash.
7. Independent digest vectors prove v1 byte compatibility, the new v2 domain,
   and strict shape/domain discrimination.
8. Existing B1, B2, release/auth, and retained M2 suites remain green with all
   provider network paths blackholed.
9. Two independent reviewers approve the exact diff with no Critical or
   Important finding, and the owned GitHub branch plus exact-SHA CI run match.

Until those conditions pass, B2-C, production deployment, campaign execution,
and Jill's return remain **NO-GO**.
