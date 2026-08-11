import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from agents.variant_agent import build_offices_and_territories


class SearchAgentOfficeFilterTests(unittest.TestCase):
    def test_ro_search_does_not_add_euipo_office(self):
        offices, territories = build_offices_and_territories(["RO"])
        self.assertEqual(offices, [])
        self.assertEqual(territories, ["RO"])

    def test_benelux_office_includes_euipo(self):
        offices, territories = build_offices_and_territories(["BX"])
        self.assertEqual(offices, ["EM"])
        self.assertEqual(territories, ["BX"])

    def test_benelux_keyword_includes_euipo(self):
        offices, territories = build_offices_and_territories(["BENELUX"])
        self.assertEqual(offices, ["EM"])
        self.assertEqual(territories, ["BX"])

    def test_country_be_stays_belgium(self):
        offices, territories = build_offices_and_territories(["BE"])
        self.assertEqual(offices, [])
        self.assertEqual(territories, ["BE"])


if __name__ == "__main__":
    unittest.main()
