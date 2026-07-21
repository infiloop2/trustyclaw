"""Regenerate host/param_guard_words.py (bundled word data for the parameter guard).

Run on demand from a venv that has the pinned generation-only packages:

    pip install mnemonic==0.21 wordfreq==3.1.1
    python tests/generate_param_guard_words.py

Runtime never fetches or recomputes this data; the generated module is checked
in and this script exists so refreshing it is a mechanical, reviewable diff
(same pattern as the Gitleaks rule fixtures). BIP-39 is a frozen standard, so
regeneration should only ever change the common-words section.
"""

from __future__ import annotations

import pathlib

BRAND_WORDS = """
adidas airbnb amazon android anthropic bitcoin chatgpt claude costco discord
dogecoin ethereum facebook ferrari fortnite gemini github gitlab google gucci
honda huawei hyundai instagram ipad iphone lamborghini linkedin macbook
microsoft minecraft netflix nike nintendo nvidia openai paypal pinterest
playstation pokemon polymarket porsche reddit roblox runway samsung shopify
snapchat spotify starbucks stripe telegram tesla tiktok toyota twitch twitter
uber venmo vuitton walmart whatsapp xbox youtube
""".split()


def main() -> None:
    from mnemonic import Mnemonic  # generation-only dependency
    import wordfreq  # generation-only dependency

    bip39 = list(Mnemonic("english").wordlist)
    assert len(bip39) == 2048

    common = [
        word
        for word in wordfreq.top_n_list("en", 10000)
        if word.isascii() and word.isalpha() and len(word) >= 3
    ]
    combined = sorted(set(common) | set(bip39) | set(BRAND_WORDS))

    def block(words: list[str]) -> str:
        lines: list[str] = []
        line: list[str] = []
        width = 0
        for word in words:
            if width + len(word) + 1 > 78:
                lines.append(" ".join(line))
                line, width = [], 0
            line.append(word)
            width += len(word) + 1
        if line:
            lines.append(" ".join(line))
        return "\n".join(lines)

    out = pathlib.Path(__file__).resolve().parent.parent / "host" / "param_guard_words.py"
    out.write_text(
        '"""Bundled word data for the outbound parameter guard (generated file).\n'
        "\n"
        "Regenerate with tests/generate_param_guard_words.py; do not edit by hand.\n"
        "BIP39_WORDS is the exact BIP-39 English wordlist (seed-phrase guard).\n"
        "COMMON_WORDS is the natural-token vocabulary (dictionary-segment signal\n"
        "and bigram statistics for the unnatural-token guard): frequent English\n"
        "words, the BIP-39 list, and common brand/product names.\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        'BIP39_WORDS: frozenset[str] = frozenset("""\n'
        f"{block(bip39)}\n"
        '""".split())\n'
        "\n"
        'COMMON_WORDS: frozenset[str] = frozenset("""\n'
        f"{block(combined)}\n"
        '""".split())\n'
    )
    print(f"wrote {out} ({len(bip39)} bip39, {len(combined)} common)")


if __name__ == "__main__":
    main()
