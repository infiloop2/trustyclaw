"""Unit tests for the outbound request parameter guard.

The negative corpora here are load-bearing: the handle/brand/query corpus is
what tunes the unnatural-token score threshold, and the provider-identifier
corpus is what keeps the card/phone/digit rules from denying legitimate
numeric ids. A failure in either is a red build, not a tuning suggestion.
"""

from __future__ import annotations

import time
import unittest

from host.param_guard import (
    MAX_PARAMETER_BYTES,
    MAX_UNBROKEN_TOKEN_CHARS,
    ParamGuardDenied,
    find_denial,
    guard_request_parameter_string,
)

# Ordinary queries, prompts, handles, brands, slugs, and multilingual text
# that the guard must pass untouched. Grow this list whenever a false
# positive is found in the field.
LEGITIMATE_VALUES = [
    "flights to Seattle next thursday",
    "latest news about Tim Cook",
    "olympics 2028 tickets",
    "world cup 2026 schedule",
    "iphone 16 pro max review",
    "nvidia rtx5090 benchmarks",
    "playstation5 vs xbox series x",
    "mrbeast6000 latest videos",
    "khaby.lame reels",
    "taylor swift eras tour setlist",
    "restaurants near me open late",
    "how to change password on iphone",
    "reset password help",
    "error code 404 meaning",
    "error code 1001 windows",
    "zip code 90210 homes for sale",
    "area code 415 businesses",
    "boeing 747 specifications",
    "un resolution 2334 text",
    "song 505 arctic monkeys",
    "google pixel 9a review",
    "web3 development tutorial",
    "covid19 vaccine schedule 2026",
    "python asyncio tutorial",
    "k8s deployment yaml example",
    "supercalifragilisticexpialidocious meaning",
    "antidisestablishmentarianism definition",
    "will bitcoin reach 150,000 this year",
    "top 100 movies of all time",
    "watercolor painting of the eiffel tower at dawn, golden light",
    "a cinematic drone shot over manhattan at night, rain, neon reflections",
    "site:linkedin.com/posts hiring machine learning engineers",
    "https://www.instagram.com/reel/DAbC123xyz/",
    "hashtag fitnessmotivation trending",
    "fitnessmotivation",
    "cristiano ronaldo transfer news",
    "shah rukh khan new movie",
    "dhoni retirement announcement",
    "meilleurs restaurants paris 7eme",
    "como aprender programacion rapido",
    "wie funktioniert photosynthese",
    "presidential election polls florida",
    "openai vs anthropic comparison",
    "instagram-reel-download.mp4",
    "summer_photo_final.png",
    # Public wallet addresses are deliberately allowed (operator preference):
    # broadcast-public identifiers, legitimate in crypto discovery queries.
    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    "whale watch 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 polymarket",
    "0x1234567890abcdef1234567890abcdef12345678",
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
    "balance of 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045, please",
]

# Real-shaped bundled-provider identifiers (tweet ids are 19 digits, Meta
# ids similar, Polymarket token ids longer): all 17+ digits, above the
# 10-16 digit window, so pasting one into a guarded query is allowed.
PROVIDER_IDENTIFIERS = [
    "1846923871234567890",  # X tweet id (snowflake)
    "17841400008460056",  # Instagram business account id
    "9223372036854775807",  # 64-bit id ceiling
    "71321045679812345678901234567890",  # Polymarket CLOB token id prefix
]


class FindDenialTest(unittest.TestCase):
    def test_legitimate_values_pass(self) -> None:
        failures = []
        for value in LEGITIMATE_VALUES:
            denial = find_denial(value)
            if denial is not None:
                failures.append((value, denial.guard))
        self.assertEqual(failures, [])

    def test_provider_identifiers_are_allowed(self) -> None:
        # 17+ digit runs are above the phone/card window: pasted provider
        # ids are legitimate lookups, and valid long cards are still caught
        # by Luhn+issuer before the digit rule.
        for identifier in PROVIDER_IDENTIFIERS:
            self.assertIsNone(find_denial(f"lookup {identifier} details"), identifier)

    def assert_guard(self, value: str, guard: str) -> None:
        denial = find_denial(value)
        self.assertIsNotNone(denial, value)
        assert denial is not None
        self.assertEqual(denial.guard, guard, value)

    # --- G1-G3 structural rules ----------------------------------------

    def test_length_limit_is_exact_utf8_bytes(self) -> None:
        # 128 words of 7 chars + 127 spaces = 1023 bytes: exactly under.
        spaced = " ".join(["seaside"] * 128)
        self.assertEqual(len(spaced.encode()), MAX_PARAMETER_BYTES - 1)
        self.assertIsNone(find_denial(spaced))
        self.assert_guard(spaced + "xx", "LENGTH")
        # Multibyte: each "é" is 2 bytes, so 400 spaced pairs = 1199 bytes.
        multibyte = " ".join(["éé"] * 240)
        self.assertGreater(len(multibyte.encode()), MAX_PARAMETER_BYTES)
        self.assert_guard(multibyte, "LENGTH")

    def test_lone_surrogates_deny_instead_of_crashing(self) -> None:
        denial = find_denial("broken \ud800 text")
        self.assertIsNotNone(denial)
        assert denial is not None
        self.assertEqual(denial.guard, "PRINTABLE")

    def test_control_and_invisible_characters_deny(self) -> None:
        self.assert_guard("hello\x00world", "PRINTABLE")
        self.assert_guard("zero​width", "PRINTABLE")
        self.assertIsNone(find_denial("tabs\tand\nnewlines are fine"))

    def test_long_unbroken_token_denies_unless_plain_https_url(self) -> None:
        blob = "A" * (MAX_UNBROKEN_TOKEN_CHARS + 1)
        self.assert_guard(f"find {blob}", "TOKEN_RUN")
        url = "https://example.com/" + "a/" * 80
        self.assertGreater(len(url), MAX_UNBROKEN_TOKEN_CHARS)
        self.assertIsNone(find_denial(f"read {url}"))
        userinfo_url = "https://user:pass@example.com/" + "a" * 120
        self.assertIsNotNone(find_denial(f"read {userinfo_url}"))

    def test_token_rules_off_allows_machine_shaped_github_values(self) -> None:
        # The GitHub read path disables both token-shape guards: long refs
        # and commit shas are legitimate there...
        sha = "9c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b"
        self.assertIsNone(find_denial(sha, token_rules=False))
        self.assertIsNone(find_denial("A" * 200, token_rules=False))
        # ...but every other guard still applies.
        self.assertIsNotNone(find_denial("alice@example.com", token_rules=False))
        self.assertIsNotNone(find_denial("AKIAIOSFODNN7EXAMPLE", token_rules=False))

    # --- Secret rules ---------------------------------------------------

    def test_credential_prefixes_deny(self) -> None:
        for secret in [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            "github_pat_11ABCDEFG0123456789abc_def",
            "glpat-AbCdEfGhIjKlMnOpQrSt",
            "xoxb-123456789012-abcdefghijkl",
            "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx",
            "rk_live_AbCdEfGhIjKlMnOpQrSt",
            "AIzaSyA1bC2dE3fG4hI5jK6lM7nO8pQ9rS0tU1v",
            "ya29.a0AbCdEfGhIjKlMnOpQrStUv",
            "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"[:40],
            "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        ]:
            self.assert_guard(f"check {secret} now", "CRED_PREFIX")

    def test_private_key_block_and_jwt_deny(self) -> None:
        self.assert_guard("-----BEGIN RSA PRIVATE KEY----- MIIE", "KEY_BLOCK")
        self.assert_guard("-----BEGIN OPENSSH PRIVATE KEY-----", "KEY_BLOCK")
        self.assert_guard(
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N65OQag",
            "JWT",
        )

    def test_seed_phrase_denies_but_prose_with_common_words_passes(self) -> None:
        seed = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        self.assert_guard(seed, "SEED_PHRASE")
        # Eleven BIP-39 words do not deny; function words break real prose runs.
        self.assertIsNone(find_denial(" ".join(seed.split()[:11])))
        self.assertIsNone(
            find_denial("the ability of the actor to act on advice about the anger was amazing")
        )

    def test_raw_crypto_keys_deny(self) -> None:
        self.assert_guard("key " + "a1" * 32, "CRYPTO_KEY")  # 64 hex
        self.assert_guard("5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ", "CRYPTO_KEY")

    def test_credential_url_denies(self) -> None:
        self.assert_guard("fetch https://alice:secret@internal.example.com/x", "CRED_URL")
        self.assert_guard(
            "read https://example.com/cb?access_token=AbCdEfGh1234567890",
            "CRED_URL",
        )
        self.assertIsNone(find_denial("read https://example.com/page?id=42&sort=asc"))

    def test_entropy_near_keyword_denies_but_needs_the_keyword(self) -> None:
        token = "x7Kp2mQv9zR4tYw8LbN3"
        self.assert_guard(f"api key {token}", "ENTROPY_NEAR_KEYWORD")
        # Same token with no keyword falls through to the unnatural-token rule
        # rather than the entropy rule (entropy alone is never the signal).
        denial = find_denial(f"lookup {token} details")
        assert denial is not None
        self.assertNotEqual(denial.guard, "ENTROPY_NEAR_KEYWORD")

    def test_password_requires_a_connective(self) -> None:
        self.assert_guard("password: hunter2secret", "PASSWORD_KEYWORD")
        self.assert_guard("my password is hunter2secret", "PASSWORD_KEYWORD")
        self.assert_guard("pwd=abc12345", "PASSWORD_KEYWORD")
        self.assertIsNone(find_denial("how to change password on iphone"))

    # --- Identifier rules ----------------------------------------------

    def test_email_denies(self) -> None:
        self.assert_guard("find linkedin of alice.smith@acme-corp.co.uk", "EMAIL")

    def test_payment_card_needs_luhn_and_issuer(self) -> None:
        self.assert_guard("charge 4111 1111 1111 1111 now", "CARD")
        self.assert_guard("card 5500-0000-0000-0004", "CARD")
        # Luhn-valid but no issuer prefix: not a card (falls to digit run).
        denial = find_denial("id 1111111111111117")
        assert denial is not None
        self.assertEqual(denial.guard, "DIGIT_RUN")

    def test_iban_aba_ssn(self) -> None:
        self.assert_guard("transfer to DE89 3704 0044 0532 0130 00", "IBAN")
        self.assert_guard("routing 021000021 wire", "ABA")
        self.assert_guard("ssn 219-09-9999 lookup", "SSN")
        self.assertIsNone(find_denial("ssn 000-12-3456 invalid area"))

    def test_phone_formats_deny_but_short_numbers_pass(self) -> None:
        self.assert_guard("call +1 415 555 2671 today", "PHONE")
        self.assert_guard("call (415) 555-2671", "PHONE")
        self.assertIsNone(find_denial("open 9-5 call 911"))

    def test_keyword_adjacent_codes_and_documents(self) -> None:
        self.assert_guard("verification code 482913", "OTP_KEYWORD")
        self.assert_guard("PIN 1234 for the card", "OTP_KEYWORD")
        self.assert_guard("born 04/12/1985 in ohio", "DOB_KEYWORD")
        self.assert_guard("passport K1234567 renewal", "GOV_ID_KEYWORD")
        self.assertIsNone(find_denial("passport renewal fees 2026"))

    def test_ip_literals_are_allowed(self) -> None:
        # Operator preference: IP literals carry little signal worth the
        # usability cost, private ranges included.
        self.assertIsNone(find_denial("geolocate 8.8.8.8"))
        self.assertIsNone(find_denial("dashboard at 192.168.1.10"))
        self.assertIsNone(find_denial("host 10.20.30.40 config"))

    def test_bare_digit_runs_deny_only_the_phone_card_window(self) -> None:
        self.assert_guard("call 4155552671", "DIGIT_RUN")  # 10 digits
        self.assert_guard("account 12345678901234", "DIGIT_RUN")  # 14 digits
        self.assertIsNone(find_denial("id 482913 lookup"))  # short ids allowed
        self.assertIsNone(find_denial("order 48291365 status"))
        self.assertIsNone(find_denial("year 2026 review"))
        self.assertIsNone(find_denial("zip 90210"))

    def test_public_wallet_addresses_are_exempt_but_keys_are_not(self) -> None:
        # The exemption masks address-shaped tokens only; everything around
        # them is still scanned, and key material keeps denying.
        self.assertIsNotNone(
            find_denial("send to 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 from alice@x.com")
        )
        self.assert_guard("key " + "a1" * 32, "CRYPTO_KEY")
        self.assert_guard("5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ", "CRYPTO_KEY")
        # Without the 0x prefix a 40-hex run is not an address shape.
        denial = find_denial("1234567890abcdef1234567890abcdef12345678")
        self.assertIsNotNone(denial)

    def test_unnatural_token_scored_check(self) -> None:
        for gibberish in [
            "x9Qv7Kp2mZr8TbN4",  # 16 chars, mixed case+digits, no dict segment
            "aGVsbG8gd29ybGQhIQzz".replace(" ", ""),  # base64-ish
            "qZx8vKpRw2mTn7Yb4Js6",
        ]:
            self.assert_guard(f"payload {gibberish}", "UNNATURAL_TOKEN")
        # Long natural compounds and handles pass.
        for natural in [
            "fitnessmotivation2026"[:14],
            "iphoneprodiscount",
            "manchesterunited",
            "arnoldschwarzenegger",
        ]:
            self.assertIsNone(find_denial(f"search {natural}"), natural)


class AllowIdentifiersTest(unittest.TestCase):
    def test_allows_identifiers_but_still_denies_secrets_and_codes(self) -> None:
        # allow_identifiers skips the personal-identifier guards (mailbox
        # search syntax), but keeps secrets, one-time codes, and encoded blobs.
        for allowed in (
            "from:alice@example.com invoice",
            "subject:budget from:boss@acme.com",
            "call +1 415 555 2671",
            "order 12345678901234",
            "verification code 482913",           # one-time codes allowed too
        ):
            self.assertEqual(
                guard_request_parameter_string(allowed, allow_identifiers=True), allowed
            )
        # OTP is a personal identifier by default, denied on public surfaces.
        with self.assertRaises(ParamGuardDenied):
            guard_request_parameter_string("verification code 482913")
        for denied in (
            "invoice AKIAIOSFODNN7EXAMPLE",       # credential prefix
            "password: hunter2secret",            # password
            "-----BEGIN RSA PRIVATE KEY----- MIIE",  # key block
            "blob x9Qv7Kp2mZr8TbN4",              # unnatural token stays enforced
        ):
            with self.assertRaises(ParamGuardDenied):
                guard_request_parameter_string(denied, allow_identifiers=True)


class GuardFunctionTest(unittest.TestCase):
    def test_returns_value_unchanged(self) -> None:
        value = "flights to Seattle"
        self.assertIs(guard_request_parameter_string(value), value)

    def test_raises_with_descriptive_value_free_message(self) -> None:
        with self.assertRaises(ParamGuardDenied) as ctx:
            guard_request_parameter_string("email alice@example.com about pricing")
        message = str(ctx.exception)
        self.assertIn("email address", message)
        self.assertIn("retry", message)
        self.assertNotIn("alice", message)
        self.assertEqual(ctx.exception.denial.reason, "request_param_pii_denied")

    def test_adversarial_inputs_complete_quickly(self) -> None:
        adversarial = [
            ("a" * 60 + "!") * 15,
            "eyJ" + "a" * 1000,
            "+1 " * 300,
            ("4111 " * 200)[:1024],
            "AKIA" + "A" * 1000,
            ("ab" * 500)[:1024],
        ]
        start = time.monotonic()
        for value in adversarial:
            find_denial(value[:1024])
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main()


class GeneratedWordDataTest(unittest.TestCase):
    """Guards against corruption of host/param_guard_words.py and, when the
    generation-only packages are installed, against divergence from the
    generator (tests/generate_param_guard_words.py)."""

    def test_word_data_is_wellformed(self) -> None:
        from host.param_guard_words import BIP39_WORDS, COMMON_WORDS

        # BIP-39 is a frozen 2048-word standard.
        self.assertEqual(len(BIP39_WORDS), 2048)
        for word in BIP39_WORDS:
            self.assertTrue(word.isascii() and word.islower() and word.isalpha())
        # COMMON_WORDS is the natural-token vocabulary; it includes BIP-39.
        self.assertTrue(BIP39_WORDS <= COMMON_WORDS)
        self.assertGreater(len(COMMON_WORDS), 5000)
        for word in ("iphone", "playstation", "nvidia", "the", "seattle"):
            self.assertIn(word, COMMON_WORDS)

    def test_word_data_matches_generator(self) -> None:
        # Regenerate and diff only when the pinned generation packages are
        # present (they are generation-only, not a runtime/CI dependency). To
        # make this run in CI, add mnemonic and wordfreq to tests/requirements.
        try:
            import mnemonic  # noqa: F401
            import wordfreq  # noqa: F401
        except ImportError:
            self.skipTest("generation-only packages (mnemonic, wordfreq) not installed")
        import importlib.util
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        checked_in = (root / "host" / "param_guard_words.py").read_text()
        spec = importlib.util.spec_from_file_location(
            "generate_param_guard_words", root / "tests" / "generate_param_guard_words.py"
        )
        assert spec and spec.loader
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        # Regenerate into a temp path and compare bytes.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "param_guard_words.py"
            # The generator writes to host/param_guard_words.py; capture its
            # output by monkeypatching the destination.
            original = pathlib.Path.write_text
            written: dict[str, str] = {}

            def capture(self, data, *a, **k):  # type: ignore[no-untyped-def]
                if self.name == "param_guard_words.py":
                    written["out"] = data
                    return len(data)
                return original(self, data, *a, **k)

            pathlib.Path.write_text = capture  # type: ignore[method-assign]
            try:
                gen.main()
            finally:
                pathlib.Path.write_text = original  # type: ignore[method-assign]
        self.assertEqual(
            written.get("out"),
            checked_in,
            "host/param_guard_words.py diverges from the generator; run "
            "tests/generate_param_guard_words.py",
        )
