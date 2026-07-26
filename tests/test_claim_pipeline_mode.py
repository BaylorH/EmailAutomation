import unittest

from email_automation.claim_pipeline.mode import (
    ClaimPipelineMode,
    PipelineGate,
    PipelineScope,
    parse_pipeline_mode,
)


class ClaimPipelineModeTests(unittest.TestCase):
    def test_blank_and_unknown_modes_fail_closed_to_off(self):
        self.assertIs(parse_pipeline_mode(""), ClaimPipelineMode.OFF)
        self.assertIs(parse_pipeline_mode("surprise"), ClaimPipelineMode.OFF)
        self.assertIs(parse_pipeline_mode(None), ClaimPipelineMode.OFF)

    def test_known_mode_parsing_is_case_and_whitespace_tolerant(self):
        self.assertIs(
            parse_pipeline_mode("  ShAdOw "),
            ClaimPipelineMode.SHADOW,
        )

    def test_parsing_an_existing_mode_preserves_it(self):
        self.assertIs(
            parse_pipeline_mode(ClaimPipelineMode.REPLAY),
            ClaimPipelineMode.REPLAY,
        )

    def test_enforce_requires_both_tenant_and_campaign_allowlist(self):
        tenant_only = PipelineGate(
            mode=ClaimPipelineMode.ENFORCE,
            allowed_scopes=(),
        )
        fully_scoped = PipelineGate(
            mode=ClaimPipelineMode.ENFORCE,
            allowed_scopes=(PipelineScope("uid-1", "campaign-1"),),
        )

        self.assertFalse(tenant_only.allows_enforcement("uid-1", "campaign-1"))
        self.assertTrue(fully_scoped.allows_enforcement("uid-1", "campaign-1"))

    def test_allowlist_matching_is_exact(self):
        gate = PipelineGate(
            mode=ClaimPipelineMode.SHADOW,
            allowed_scopes=(PipelineScope("uid-1", "campaign-1"),),
        )

        self.assertFalse(gate.allows_shadow("uid-10", "campaign-1"))
        self.assertFalse(gate.allows_shadow("uid-1", "campaign-10"))

    def test_shadow_permission_does_not_enable_enforcement(self):
        gate = PipelineGate(
            mode=ClaimPipelineMode.SHADOW,
            allowed_scopes=(PipelineScope("uid-1", "campaign-1"),),
        )

        self.assertTrue(gate.allows_shadow("uid-1", "campaign-1"))
        self.assertFalse(gate.allows_enforcement("uid-1", "campaign-1"))

    def test_off_mode_denies_every_capability(self):
        gate = PipelineGate(
            mode=ClaimPipelineMode.OFF,
            allowed_scopes=(PipelineScope("uid-1", "campaign-1"),),
        )

        self.assertFalse(gate.allows_replay("uid-1", "campaign-1"))
        self.assertFalse(gate.allows_shadow("uid-1", "campaign-1"))
        self.assertFalse(gate.allows_enforcement("uid-1", "campaign-1"))

    def test_scope_pairs_do_not_form_a_cartesian_product(self):
        gate = PipelineGate(
            mode=ClaimPipelineMode.ENFORCE,
            allowed_scopes=(
                PipelineScope("uid-1", "campaign-a"),
                PipelineScope("uid-2", "campaign-b"),
            ),
        )

        self.assertTrue(gate.allows_enforcement("uid-1", "campaign-a"))
        self.assertTrue(gate.allows_enforcement("uid-2", "campaign-b"))
        self.assertFalse(gate.allows_enforcement("uid-1", "campaign-b"))
        self.assertFalse(gate.allows_enforcement("uid-2", "campaign-a"))

    def test_scope_collection_is_immutable_after_construction(self):
        scopes = [PipelineScope("uid-1", "campaign-a")]
        gate = PipelineGate(
            mode=ClaimPipelineMode.ENFORCE,
            allowed_scopes=scopes,
        )

        scopes.append(PipelineScope("uid-2", "campaign-b"))

        self.assertFalse(gate.allows_enforcement("uid-2", "campaign-b"))


if __name__ == "__main__":
    unittest.main()
