"""Unit tests for the signature website validator.

No network is touched: the only Tier 2 tests stub socket.getaddrinfo /
urllib.request.urlopen inside the module under test.
"""

import os
import socket
import unittest
from unittest import mock

from email_automation.signature_website_validation import (
    SEVERITY_BLOCK,
    SEVERITY_WARN,
    SIGNATURE_WEBSITE_REACHABILITY_ENV,
    SIGNATURE_WEBSITE_VALIDATION_ENV,
    apply_signature_website_policy,
    check_signature_website_reachable,
    extract_plain_text_website_candidates,
    extract_signature_link_targets,
    inspect_signature_websites,
    signature_website_advisory,
    signature_website_reachability_enabled,
    signature_website_validation_enabled,
    validate_signature_website_url,
)
from email_automation import signature_website_validation as swv


def _flag_on(**extra):
    env = {SIGNATURE_WEBSITE_VALIDATION_ENV: "true"}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=False)


def _flag_off():
    return mock.patch.dict(
        os.environ,
        {SIGNATURE_WEBSITE_VALIDATION_ENV: "", SIGNATURE_WEBSITE_REACHABILITY_ENV: ""},
        clear=False,
    )


SIGNATURE_TEMPLATE = (
    '<div data-sitesift-professional-signature="v1">'
    '<a href="mailto:broker@example-real-firm.co">broker</a>'
    '<a href="{url}" target="_blank" rel="noopener noreferrer" '
    'style="color:#CC0000;">{text}</a>'
    "</div>"
)


class FlagDefaultTests(unittest.TestCase):
    def test_both_flags_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(signature_website_validation_enabled())
            self.assertFalse(signature_website_reachability_enabled())

    def test_reachability_requires_master_flag(self):
        with mock.patch.dict(
            os.environ,
            {SIGNATURE_WEBSITE_VALIDATION_ENV: "", SIGNATURE_WEBSITE_REACHABILITY_ENV: "true"},
            clear=True,
        ):
            self.assertFalse(signature_website_reachability_enabled())

    def test_flag_is_exact_match_not_truthy(self):
        for value in ("1", "yes", "TRUE ", "on", "false"):
            with mock.patch.dict(os.environ, {SIGNATURE_WEBSITE_VALIDATION_ENV: value}, clear=True):
                expected = value.strip().lower() == "true"
                self.assertEqual(signature_website_validation_enabled(), expected, value)


class ReservedDomainTests(unittest.TestCase):
    def test_rfc2606_documentation_domains_are_blocked(self):
        for url in ("example.com", "https://example.net", "http://www.example.org/", "example.edu"):
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.severity, SEVERITY_BLOCK, url)
            self.assertEqual(verdict.code, "reserved_domain", url)

    def test_reserved_tlds_are_blocked(self):
        cases = {
            "https://acme.test": "reserved_tld",
            "https://acme.invalid": "reserved_tld",
            "https://acme.localhost": "private_or_loopback_address",
            "https://printer.local": "reserved_tld",
            "https://anything.example": "reserved_tld",
            "https://box.internal": "reserved_tld",
            "https://router.home.arpa": "reserved_domain",
        }
        for url, code in cases.items():
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.severity, SEVERITY_BLOCK, url)
            self.assertEqual(verdict.code, code, url)

    def test_subdomain_of_reserved_domain_is_blocked(self):
        verdict = validate_signature_website_url("https://mail.example.com")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.code, "reserved_domain")


class PlaceholderHostTests(unittest.TestCase):
    def test_obvious_placeholders_are_blocked(self):
        for url in (
            "yourcompany.com",
            "https://www.yourdomain.com",
            "http://mysite.com",
            "changeme.io",
            "https://todo.com",
            "lorem.net",
            "https://placeholder.co.uk",
            "https://your-company.com",
            "https://COMPANYNAME.com",
        ):
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.severity, SEVERITY_BLOCK, url)
            self.assertEqual(verdict.code, "placeholder_host", url)

    def test_placeholder_match_is_label_exact_not_substring(self):
        # A real domain that merely CONTAINS a placeholder word must pass.
        for url in (
            "https://beyourcompany.com",
            "https://mysitegroup.com",
            "https://todos.app",
            "https://loremrealty.com",
            "https://domainregistrar.com",
        ):
            self.assertIsNone(validate_signature_website_url(url), url)

    def test_public_suffix_is_never_judged_as_placeholder(self):
        # ".website" and ".domains" are real gTLDs; the suffix label is skipped.
        self.assertIsNone(validate_signature_website_url("https://realbrokerage.website"))


class PrivateAddressTests(unittest.TestCase):
    def test_loopback_and_private_ips_are_blocked(self):
        for url in (
            "http://127.0.0.1",
            "http://localhost",
            "http://localhost:8080/",
            "http://192.168.1.10",
            "http://10.0.0.5/site",
            "http://172.16.4.4",
            "http://169.254.10.10",
            "http://[::1]/",
        ):
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.severity, SEVERITY_BLOCK, url)
            self.assertEqual(verdict.code, "private_or_loopback_address", url)

    def test_public_bare_ip_is_blocked_as_bare_ip(self):
        verdict = validate_signature_website_url("http://93.184.216.34")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.code, "bare_ip_address")

    def test_single_label_host_is_blocked(self):
        verdict = validate_signature_website_url("http://intranet")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.code, "no_public_suffix")


class MalformedUrlTests(unittest.TestCase):
    def test_non_http_schemes_are_blocked(self):
        for url in ("ftp://files.acmerealty.com", "javascript:alert(1)", "file:///etc/passwd"):
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.code, "unsupported_scheme", url)

    def test_structurally_broken_hosts_are_blocked(self):
        for url in (
            "https://",
            "https://-leading.com",
            "https://trailing-.com",
            "https://double..dot.com",
            "https://has space.com",
            "https://under_score.com",
            "https://acme.1",
            "https://acme.c",
        ):
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.severity, SEVERITY_BLOCK, url)

    def test_unresolved_merge_tokens_are_blocked(self):
        for url in ("https://[COMPANY].com", "{{website}}", "https://%DOMAIN%.com", "${site}.com"):
            verdict = validate_signature_website_url(url)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.code, "unresolved_placeholder_token", url)

    def test_empty_is_not_a_finding(self):
        for value in (None, "", "   "):
            self.assertIsNone(validate_signature_website_url(value))


class LegitimateUrlTests(unittest.TestCase):
    def test_real_looking_websites_pass_offline_checks(self):
        for url in (
            "acmerealty.com",
            "https://www.acmerealty.com",
            "https://acme-realty.co.uk/about",
            "http://acme.realty",
            "https://sub.domain.acmerealty.com:8443/team",
            "https://xn--bcher-kva.example-real.de",
        ):
            self.assertIsNone(validate_signature_website_url(url), url)

    def test_unicode_host_is_idna_encoded_not_rejected(self):
        self.assertIsNone(validate_signature_website_url("https://bücher-realty.de"))


class ExtractionTests(unittest.TestCase):
    def test_hrefs_are_extracted_and_mailto_skipped(self):
        html = SIGNATURE_TEMPLATE.format(url="https://yourcompany.com", text="yourcompany.com")
        self.assertEqual(extract_signature_link_targets(html), ["https://yourcompany.com"])

    def test_cid_data_and_tel_targets_are_skipped(self):
        html = (
            '<a href="cid:signature-logo">x</a>'
            '<a href="tel:+15550000000">call</a>'
            '<a href="data:image/png;base64,AAAA">img</a>'
            '<a href="#top">top</a>'
        )
        self.assertEqual(extract_signature_link_targets(html), [])

    def test_plain_text_candidates_only_when_no_anchors(self):
        self.assertEqual(
            extract_plain_text_website_candidates("Jane Doe\nyourcompany.com\ncall 555-0100"),
            ["yourcompany.com"],
        )
        self.assertEqual(
            extract_plain_text_website_candidates('<a href="https://yourcompany.com">x</a>'),
            [],
        )

    def test_plain_text_extraction_ignores_email_addresses(self):
        self.assertEqual(
            extract_plain_text_website_candidates("reach me at jane.doe@acmerealty.com"),
            [],
        )


class PolicyEnforcementTests(unittest.TestCase):
    def test_flag_off_returns_byte_identical_html(self):
        html = SIGNATURE_TEMPLATE.format(url="https://yourcompany.com", text="yourcompany.com")
        with _flag_off():
            out, findings = apply_signature_website_policy(html)
        self.assertEqual(out, html)
        self.assertEqual(findings, [])

    def test_flag_on_delinks_dangerous_website_but_keeps_text(self):
        html = SIGNATURE_TEMPLATE.format(url="https://yourcompany.com", text="yourcompany.com")
        with _flag_on():
            out, findings = apply_signature_website_policy(html)
        self.assertNotIn("https://yourcompany.com", out)
        self.assertNotIn("href=", out.split("mailto:broker@example-real-firm.co")[1])
        self.assertIn("yourcompany.com</a>", out)          # visible text survives
        self.assertIn("mailto:broker@example-real-firm.co", out)  # mailto untouched
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, SEVERITY_BLOCK)
        self.assertEqual(findings[0].code, "placeholder_host")

    def test_flag_on_leaves_a_good_website_untouched(self):
        html = SIGNATURE_TEMPLATE.format(url="https://acmerealty.com", text="acmerealty.com")
        with _flag_on():
            out, findings = apply_signature_website_policy(html)
        self.assertEqual(out, html)
        self.assertEqual(findings, [])

    def test_policy_never_makes_a_network_call(self):
        html = SIGNATURE_TEMPLATE.format(url="https://acmerealty.com", text="acmerealty.com")
        with _flag_on(**{SIGNATURE_WEBSITE_REACHABILITY_ENV: "true"}):
            with mock.patch.object(swv.socket, "getaddrinfo", side_effect=AssertionError("network!")):
                out, findings = apply_signature_website_policy(html)
        self.assertEqual(out, html)
        self.assertEqual(findings, [])

    def test_policy_fails_open_when_the_validator_explodes(self):
        html = SIGNATURE_TEMPLATE.format(url="https://yourcompany.com", text="yourcompany.com")
        with _flag_on():
            with mock.patch.object(swv, "_blocked_hrefs", side_effect=RuntimeError("boom")):
                out, findings = apply_signature_website_policy(html)
        self.assertEqual(out, html)
        self.assertEqual(findings, [])

    def test_empty_and_non_string_inputs_are_safe(self):
        with _flag_on():
            self.assertEqual(apply_signature_website_policy(""), ("", []))
            self.assertEqual(apply_signature_website_policy(None), ("", []))

    def test_plain_text_signature_is_never_rewritten(self):
        plain = "Jane Doe<br>yourcompany.com<br>555-0100"
        with _flag_on():
            out, findings = apply_signature_website_policy(plain)
        self.assertEqual(out, plain)
        self.assertEqual(findings, [])


class ReachabilityTierTests(unittest.TestCase):
    def test_dns_failure_is_warn_never_block(self):
        with mock.patch.object(swv.socket, "getaddrinfo", side_effect=socket.gaierror("nope")):
            verdict = check_signature_website_reachable("https://acmerealty.com")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.severity, SEVERITY_WARN)
        self.assertEqual(verdict.code, "dns_unresolvable")

    def test_local_network_failure_says_nothing(self):
        with mock.patch.object(swv.socket, "getaddrinfo", side_effect=OSError("no route")):
            self.assertIsNone(check_signature_website_reachable("https://acmerealty.com"))

    def test_http_connection_failure_is_warn(self):
        import urllib.error

        with mock.patch.object(swv.socket, "getaddrinfo", return_value=[()]):
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
                verdict = check_signature_website_reachable("https://acmerealty.com")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.severity, SEVERITY_WARN)
        self.assertEqual(verdict.code, "http_unreachable")

    def test_http_error_status_counts_as_reachable(self):
        import urllib.error

        err = urllib.error.HTTPError("https://acmerealty.com", 405, "Method Not Allowed", {}, None)
        with mock.patch.object(swv.socket, "getaddrinfo", return_value=[()]):
            with mock.patch("urllib.request.urlopen", side_effect=err):
                self.assertIsNone(check_signature_website_reachable("https://acmerealty.com"))

    def test_probe_fails_open_on_unexpected_error(self):
        with mock.patch.object(swv, "_probe", side_effect=RuntimeError("boom")):
            self.assertIsNone(check_signature_website_reachable("https://acmerealty.com"))

    def test_reachability_never_returns_block_severity(self):
        for side_effect in (socket.gaierror("x"), OSError("y")):
            with mock.patch.object(swv.socket, "getaddrinfo", side_effect=side_effect):
                verdict = check_signature_website_reachable("https://acmerealty.com")
            if verdict is not None:
                self.assertEqual(verdict.severity, SEVERITY_WARN)


class InspectionTests(unittest.TestCase):
    def test_structured_website_field_is_inspected(self):
        findings = inspect_signature_websites(website="yourcompany.com")
        self.assertEqual([f.code for f in findings], ["placeholder_host"])

    def test_reachability_is_off_by_default_in_inspection(self):
        with mock.patch.object(swv.socket, "getaddrinfo", side_effect=AssertionError("network!")):
            self.assertEqual(inspect_signature_websites(website="acmerealty.com"), [])

    def test_social_chrome_hosts_are_not_probed(self):
        html = '<a href="https://www.linkedin.com/company/acme">in</a>'
        with mock.patch.object(swv.socket, "getaddrinfo", side_effect=AssertionError("network!")):
            findings = inspect_signature_websites(signature_html=html, check_reachability=True)
        self.assertEqual(findings, [])

    def test_duplicate_candidates_are_reported_once(self):
        html = SIGNATURE_TEMPLATE.format(url="https://yourcompany.com", text="yourcompany.com")
        findings = inspect_signature_websites(website="yourcompany.com", signature_html=html)
        self.assertEqual(len(findings), 1)


class AdvisoryTests(unittest.TestCase):
    def test_advisory_is_empty_when_flag_off(self):
        with _flag_off():
            self.assertEqual(
                signature_website_advisory({"professionalSignature": {"website": "yourcompany.com"}}),
                {},
            )

    def test_advisory_reports_blocking_finding_when_flag_on(self):
        with _flag_on():
            advisory = signature_website_advisory(
                {"professionalSignature": {"website": "yourcompany.com"}}
            )
        self.assertTrue(advisory["hasBlockingFinding"])
        self.assertEqual(advisory["findings"][0]["code"], "placeholder_host")

    def test_advisory_tolerates_garbage_profiles(self):
        with _flag_on():
            for profile in (None, {}, {"professionalSignature": "not-a-dict"}, {"emailSignature": 12}):
                self.assertEqual(signature_website_advisory(profile), {})


if __name__ == "__main__":
    unittest.main()
