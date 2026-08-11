import unittest

from backend.agents.variant_agent import build_input_list, generate_all_variants


class VariantAgentTests(unittest.TestCase):
    def extract_plain_substrings(self, variants, original_name, expected_lengths):
        """Return only genuine substring windows for patterns like '*TEXT*' without ? or other chars."""
        out = []
        upper = original_name.upper()
        for v in variants:
            if v.startswith("*") and v.endswith("*") and "?" not in v and "*" not in v[1:-1]:
                inner = v[1:-1]
                if inner.isalpha() and inner != upper and len(inner) in expected_lengths:
                    out.append(inner)
        return out

    def test_build_input_list_respects_single_length(self):
        name = "KARTEZIAN"
        vars_len4 = build_input_list(name, substring_lengths=[4])
        subs = self.extract_plain_substrings(vars_len4, name, {4})
        self.assertTrue(subs, "No plain substrings generated")
        lengths = {len(s) for s in subs}
        self.assertEqual(lengths, {4}, f"Expected only 4-letter substrings, got lengths {lengths}")

    def test_build_input_list_respects_range(self):
        name = "KARTEZIAN"
        vars_len34 = build_input_list(name, substring_lengths=[3, 4])
        subs = self.extract_plain_substrings(vars_len34, name, {3, 4})
        self.assertTrue(subs, "No plain substrings generated")
        lengths = {len(s) for s in subs}
        self.assertEqual(lengths, {3, 4}, f"Expected 3- and 4-letter substrings, got lengths {lengths}")

    def test_generate_all_variants_includes_wildcards(self):
        name = "KARTEZIAN"
        g = generate_all_variants(name, substring_lengths=[4])
        wild = g.get('wildcard', [])
        self.assertTrue(any(w.startswith('*') and w.endswith('*') for w in wild), "No wildcard patterns in generated variants")

    def test_generate_all_variants_exposes_truncated_root_terms(self):
        name = "KARTEZIAN"
        g = generate_all_variants(name)
        truncated = g.get('truncated_root', [])
        self.assertIn('*KARTEZIAN', truncated, "Left-truncated root term missing")
        self.assertIn('KARTEZIAN*', truncated, "Right-truncated root term missing")

    def test_build_input_list_generates_longer_prefix_suffix_windows(self):
        name = "KARTEZIAN"
        variants = build_input_list(name)
        self.assertIn('*KARTEZ*', variants, "Expected a 6-letter prefix window")
        self.assertIn('*RTEZIAN*', variants, "Expected a 7-letter suffix window")

    def test_build_input_list_progressive_left_right_trimming_to_four_chars(self):
        name = "KARTEZIAN"
        variants = build_input_list(name)

        # Trim from left: 1..5 characters removed -> minimum remaining length 4.
        self.assertIn('*ARTEZIAN*', variants)
        self.assertIn('*RTEZIAN*', variants)
        self.assertIn('*TEZIAN*', variants)
        self.assertIn('*EZIAN*', variants)
        self.assertIn('*ZIAN*', variants)

        # Trim from right: 1..5 characters removed -> minimum remaining length 4.
        self.assertIn('*KARTEZIA*', variants)
        self.assertIn('*KARTEZI*', variants)
        self.assertIn('*KARTEZ*', variants)
        self.assertIn('*KARTE*', variants)
        self.assertIn('*KART*', variants)

    def test_build_input_list_progressive_both_sides_trimming_to_four_chars(self):
        name = "KARTEZIAN"
        variants = build_input_list(name)

        # Tăieri simultane stânga + dreapta.
        self.assertIn('*ARTEZIA*', variants)  # -1 stânga, -1 dreapta
        self.assertIn('*RTEZI*', variants)    # -2 stânga, -2 dreapta
        self.assertIn('*TEZI*', variants)     # fereastră internă de 4 litere

    def test_generate_all_variants_includes_phonetic_forms_without_repeated_root(self):
        name = "KARTEZIAN"
        variants = generate_all_variants(name)
        terms = variants.get('search_terms', [])
        self.assertNotIn('KARTEZIAN KARTEZIAN', terms, "Repeated-root phrase variant should not be generated")
        self.assertNotIn('*KARTEZIAN KARTEZIAN*', terms, "Wildcarded repeated-root phrase variant should not be generated")
        self.assertIn('KARTEZYAN', terms, "Expected phonetic variant in combined search terms")

    def test_generate_all_variants_keeps_flat_search_terms(self):
        name = "KARTEZIAN"
        variants = generate_all_variants(name)
        self.assertNotIn('search_groups', variants)
        self.assertIn('KARTEZIAN', variants.get('search_terms', []))
        self.assertNotIn('KARTEZIAN KARTEZIAN', variants.get('search_terms', []))


if __name__ == '__main__':
    unittest.main()
