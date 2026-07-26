import unittest
from dataclasses import FrozenInstanceError

from email_automation.claim_pipeline.contracts import (
    Actor,
    ActorRole,
    Direction,
    EntityType,
    EvidenceSource,
)
from email_automation.claim_pipeline.entities import EntitySeed, resolve_entities
from email_automation.claim_pipeline.evidence import (
    ExternalEvidenceInput,
    RawMessageEvidence,
    normalize_message_evidence,
)


ACTOR = Actor("Alex Broker", "alex@example.com", ActorRole.BROKER)


def evidence_for(
    body,
    *,
    subject="RE: 123 Industrial Ave",
    signature="",
    external=(),
):
    return normalize_message_evidence(
        RawMessageEvidence(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            message_id="message-1",
            direction=Direction.INBOUND,
            actor=ACTOR,
            observed_at="2026-07-22T12:00:00Z",
            subject=subject,
            body=body,
            signature=signature,
            external=external,
        )
    ).evidence


def target_seed(**overrides):
    values = {
        "entity_type": EntityType.TARGET_PROPERTY,
        "label": "123 Industrial Ave",
        "canonical_address": "123 Industrial Avenue",
        "relationship": "target",
        "aliases": ("123 Industrial Ave", "123 Industrial Avenue, Boise"),
    }
    values.update(overrides)
    return EntitySeed(**values)


def resolved(body, **evidence_kwargs):
    return resolve_entities(
        tenant_id="uid-1",
        campaign_id="campaign-1",
        seeds=(target_seed(),),
        evidence=evidence_for(body, **evidence_kwargs),
    )


class EntityResolutionTests(unittest.TestCase):
    def test_target_alias_resolves_to_seeded_target(self):
        result = resolved("123 Industrial Avenue is available.")

        targets = [
            item for item in result.entities if item.entity_type is EntityType.TARGET_PROPERTY
        ]
        alternates = [item for item in result.entities if item.relationship == "alternate"]
        self.assertEqual(1, len(targets))
        self.assertEqual([], alternates)
        self.assertTrue(any(match.match_kind == "target_exact" for match in result.matches))

    def test_post_directional_target_with_city_resolves_to_street_identity(self):
        seed = target_seed(
            label="22 Oak Ave North, Austin",
            canonical_address="22 Oak Avenue North, Austin",
            aliases=("22 Oak Ave North",),
        )
        result = resolve_entities(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            seeds=(seed,),
            evidence=evidence_for(
                "22 Oak Avenue North remains available.",
                subject="RE: 22 Oak Ave North, Austin",
            ),
        )

        targets = [
            item for item in result.entities if item.entity_type is EntityType.TARGET_PROPERTY
        ]
        self.assertEqual(1, len(targets))
        self.assertEqual("22 oak avenue north", targets[0].canonical_address)
        self.assertEqual([], [item for item in result.entities if item.relationship == "alternate"])

    def test_different_explicit_address_is_an_alternate_never_the_target(self):
        result = resolved("999 Other Road is available instead.")

        alternate = next(item for item in result.entities if item.relationship == "alternate")
        target = next(
            item for item in result.entities if item.entity_type is EntityType.TARGET_PROPERTY
        )
        self.assertEqual("999 other road", alternate.canonical_address)
        self.assertNotEqual(target.entity_id, alternate.entity_id)
        alternate_matches = [
            match for match in result.matches if match.entity_id == alternate.entity_id
        ]
        self.assertTrue(alternate_matches)
        self.assertTrue(all(match.match_kind == "alternate_address" for match in alternate_matches))

    def test_split_suites_create_distinct_subjects_under_target(self):
        result = resolved("Suite A is leased. Suite B remains available.")

        suites = [item for item in result.entities if item.entity_type is EntityType.SUITE]
        self.assertEqual({"A", "B"}, {item.suite for item in suites})
        self.assertEqual(
            {"123 industrial avenue"},
            {item.canonical_address for item in suites},
        )
        self.assertTrue(all(item.relationship == "suite_of_target" for item in suites))

    def test_equivalent_addresses_deduplicate(self):
        result = resolved(
            "999 Other Rd is available.\nThe flyer for 999 Other Road is attached.",
            external=(
                ExternalEvidenceInput(
                    EvidenceSource.ATTACHMENT,
                    "attachment:flyer.pdf",
                    content="999 OTHER ROAD",
                ),
            ),
        )

        alternates = [item for item in result.entities if item.relationship == "alternate"]
        self.assertEqual(1, len(alternates))
        self.assertGreaterEqual(len(alternates[0].evidence_ids), 2)

    def test_actor_email_creates_one_contact_across_all_evidence(self):
        result = resolved(
            "Suite B is available.",
            signature="Alex Broker\nalex@example.com",
        )

        contacts = [item for item in result.entities if item.entity_type is EntityType.CONTACT]
        self.assertEqual(1, len(contacts))
        self.assertEqual("alex@example.com", contacts[0].canonical_address)
        self.assertEqual("contact", contacts[0].relationship)

    def test_contact_identity_does_not_fork_when_display_name_changes(self):
        first = evidence_for("123 Industrial Ave is available.")
        second = normalize_message_evidence(
            RawMessageEvidence(
                tenant_id="uid-1",
                campaign_id="campaign-1",
                message_id="message-2",
                direction=Direction.INBOUND,
                actor=Actor("A. Broker", "alex@example.com", ActorRole.BROKER),
                observed_at="2026-07-22T12:05:00Z",
                body="Suite B is available.",
            )
        ).evidence

        result = resolve_entities(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            seeds=(target_seed(),),
            evidence=first + second,
        )

        contacts = [item for item in result.entities if item.entity_type is EntityType.CONTACT]
        self.assertEqual(1, len(contacts))
        self.assertEqual("alex@example.com", contacts[0].label)

    def test_contact_identity_does_not_fork_when_actor_role_changes(self):
        first = evidence_for("123 Industrial Ave is available.")
        second = normalize_message_evidence(
            RawMessageEvidence(
                tenant_id="uid-1",
                campaign_id="campaign-1",
                message_id="message-2",
                direction=Direction.INBOUND,
                actor=Actor("Alex Broker", "alex@example.com", ActorRole.UNKNOWN),
                observed_at="2026-07-22T12:05:00Z",
                body="Suite B is available.",
            )
        ).evidence

        result = resolve_entities(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            seeds=(target_seed(),),
            evidence=first + second,
        )

        contacts = [item for item in result.entities if item.entity_type is EntityType.CONTACT]
        self.assertEqual(1, len(contacts))

    def test_ambiguous_other_building_creates_review_issue_not_fake_property(self):
        result = resolved("The other building may be a fit.")

        self.assertIn("ambiguous_alternate", {issue.code for issue in result.issues})
        self.assertEqual(
            [],
            [item for item in result.entities if item.relationship == "alternate"],
        )

    def test_multiple_competing_addresses_create_review_issue(self):
        result = resolved("999 Other Road or 456 Market Street could work.")

        self.assertEqual(
            {"456 market street", "999 other road"},
            {
                item.canonical_address
                for item in result.entities
                if item.relationship == "alternate"
            },
        )
        self.assertIn(
            "multiple_property_candidates",
            {issue.code for issue in result.issues},
        )

    def test_quoted_alternate_has_lower_confidence_and_does_not_replace_target(self):
        result = resolved(
            "123 Industrial Ave is available.\n\n"
            "On Tue, Jul 21, 2026 at 9:30 AM Pat wrote:\n"
            "> 999 Other Road was available."
        )

        target = next(
            item for item in result.entities if item.entity_type is EntityType.TARGET_PROPERTY
        )
        alternate = next(item for item in result.entities if item.relationship == "alternate")
        quoted_match = next(
            match
            for match in result.matches
            if match.entity_id == alternate.entity_id
        )
        target_match = next(
            match for match in result.matches if match.entity_id == target.entity_id
        )
        self.assertLess(quoted_match.confidence, target_match.confidence)
        self.assertEqual("123 industrial avenue", target.canonical_address)

    def test_unbound_suite_with_competing_addresses_is_visible(self):
        result = resolved("999 Other Road and 456 Market Street have Suite B available.")

        self.assertIn("unbound_suite", {issue.code for issue in result.issues})
        self.assertEqual([], [x for x in result.entities if x.entity_type is EntityType.SUITE])

    def test_ordinary_space_and_unit_prose_does_not_create_suites(self):
        result = resolved(
            "The building has 10 parking spaces on Industrial Road. "
            "The suite includes warehouse lighting, the unit pricing is $14/SF, "
            "and the space requirement is 20,000 SF."
        )

        self.assertEqual(
            [],
            [item for item in result.entities if item.entity_type is EntityType.SUITE],
        )
        self.assertEqual(
            [],
            [item for item in result.entities if item.relationship == "alternate"],
        )
        self.assertNotIn(
            "ambiguous_alternate",
            {issue.code for issue in result.issues},
        )

    def test_drive_in_count_does_not_fabricate_a_street_address(self):
        result = resolved("123 Industrial Ave has 2 drive-ins and 4 docks.")

        self.assertEqual(
            [],
            [item for item in result.entities if item.relationship == "alternate"],
        )

    def test_hyphenated_drive_in_street_name_remains_a_valid_address(self):
        result = resolved(
            "The alternate is 12 Drive-In Lane and it remains available."
        )

        self.assertEqual(
            ["12 drive-in lane"],
            [
                item.canonical_address
                for item in result.entities
                if item.relationship == "alternate"
            ],
        )

    def test_non_property_target_seed_is_rejected(self):
        with self.assertRaises(ValueError):
            EntitySeed(
                entity_type=EntityType.CONTACT,
                label="alex@example.com",
            )

    def test_multiple_target_property_seeds_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_entities(
                tenant_id="uid-1",
                campaign_id="campaign-1",
                seeds=(
                    target_seed(),
                    target_seed(
                        label="999 Other Road",
                        canonical_address="999 Other Road",
                        aliases=("999 Other Road",),
                    ),
                ),
                evidence=evidence_for("Suite B is available."),
            )

    def test_cross_tenant_evidence_is_rejected(self):
        foreign = normalize_message_evidence(
            RawMessageEvidence(
                tenant_id="uid-2",
                campaign_id="campaign-1",
                message_id="foreign-message",
                direction=Direction.INBOUND,
                actor=ACTOR,
                observed_at="2026-07-22T12:00:00Z",
                body="999 Other Road is available.",
            )
        ).evidence

        with self.assertRaises(ValueError):
            resolve_entities(
                tenant_id="uid-1",
                campaign_id="campaign-1",
                seeds=(target_seed(),),
                evidence=foreign,
            )

    def test_cross_campaign_evidence_is_rejected(self):
        foreign = normalize_message_evidence(
            RawMessageEvidence(
                tenant_id="uid-1",
                campaign_id="campaign-2",
                message_id="foreign-message",
                direction=Direction.INBOUND,
                actor=ACTOR,
                observed_at="2026-07-22T12:00:00Z",
                body="999 Other Road is available.",
            )
        ).evidence

        with self.assertRaises(ValueError):
            resolve_entities(
                tenant_id="uid-1",
                campaign_id="campaign-1",
                seeds=(target_seed(),),
                evidence=foreign,
            )

    def test_addressless_child_suite_inherits_unique_alternate_parent(self):
        evidence = evidence_for(
            "999 Other Road is another option.",
            external=(
                ExternalEvidenceInput(
                    EvidenceSource.ATTACHMENT,
                    "attachment:floorplan.pdf",
                    content="Suite 200 floor plan",
                ),
            ),
        )
        result = resolve_entities(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            seeds=(target_seed(),),
            evidence=evidence,
        )

        suite = next(item for item in result.entities if item.entity_type is EntityType.SUITE)
        self.assertEqual("999 other road", suite.canonical_address)
        self.assertEqual("suite_of_alternate", suite.relationship)

    def test_directional_aliases_resolve_to_same_target(self):
        seed = target_seed(
            label="123 N Main St",
            canonical_address="123 N Main Street",
            aliases=("123 N Main St",),
        )
        result = resolve_entities(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            seeds=(seed,),
            evidence=evidence_for(
                "123 North Main Street is available.",
                subject="RE: 123 N Main St",
            ),
        )

        self.assertEqual(
            [],
            [item for item in result.entities if item.relationship == "alternate"],
        )

    def test_result_and_seed_sequences_are_immutable(self):
        aliases = ["123 Industrial Ave"]
        seed = target_seed(aliases=aliases)
        aliases.append("999 Other Road")
        evidence = list(evidence_for("123 Industrial Ave is available."))
        result = resolve_entities(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            seeds=[seed],
            evidence=evidence,
        )
        evidence.clear()

        self.assertEqual(("123 Industrial Ave",), seed.aliases)
        self.assertIsInstance(result.entities, tuple)
        self.assertTrue(result.entities)
        with self.assertRaises(FrozenInstanceError):
            seed.label = "changed"


if __name__ == "__main__":
    unittest.main()
