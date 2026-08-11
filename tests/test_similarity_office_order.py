import unittest

from backend.agents.similarity_agent import SimilarityAgent


class SimilarityOfficeOrderTests(unittest.TestCase):
    def test_conflicts_are_ordered_requested_then_em_then_wo(self):
        agent = SimilarityAgent(threshold_very_high=90.0, threshold_high=75.0, threshold_medium=60.0, threshold_small=35.0)

        marks = [
            {"tmName": "ALFA WORLD", "office": "WO", "status": "registered"},
            {"tmName": "ALFA EUROPE", "office": "EM", "status": "registered"},
            {"tmName": "ALFA ROMANIA", "office": "RO", "status": "registered"},
            {"tmName": "ALFA SWISS", "office": "CH", "status": "registered"},
        ]

        analysis = agent.analyze("ALFA", marks, ["35"], requested_offices=["RO"])
        offices = [m.get("office") for m in analysis["conflicts"]]

        self.assertGreaterEqual(len(offices), 3)
        self.assertEqual(offices[0], "RO")
        self.assertEqual(offices[1], "EM")
        self.assertEqual(offices[2], "WO")

    def test_primary_territory_stays_before_em_even_with_other_selected_territories(self):
        agent = SimilarityAgent(threshold_very_high=90.0, threshold_high=75.0, threshold_medium=60.0, threshold_small=35.0)

        marks = [
            {"tmName": "ALFA GERMANY", "office": "DE", "status": "registered", "applicationDate": "2016-01-01"},
            {"tmName": "ALFA EUROPE", "office": "EM", "status": "registered", "applicationDate": "2015-01-01"},
            {"tmName": "ALFA ROMANIA", "office": "RO", "status": "registered", "applicationDate": "2017-01-01"},
            {"tmName": "ALFA WORLD", "office": "WO", "status": "registered", "applicationDate": "2014-01-01"},
        ]

        analysis = agent.analyze("ALFA", marks, ["35"], requested_offices=["RO", "DE", "EM"])
        offices = [m.get("office") for m in analysis["conflicts"]]

        self.assertEqual(offices[:4], ["RO", "EM", "WO", "DE"])

    def test_conflicts_are_ordered_by_filing_date_oldest_first_inside_office(self):
        agent = SimilarityAgent(threshold_very_high=90.0, threshold_high=75.0, threshold_medium=60.0, threshold_small=35.0)

        marks = [
            {"tmName": "ALFA RO NEW", "office": "RO", "status": "registered", "applicationDate": "2023-06-10", "registrationDate": "2024-01-01"},
            {"tmName": "ALFA RO OLD", "office": "RO", "status": "registered", "applicationDate": "2018-02-01", "registrationDate": "2025-01-01"},
            {"tmName": "ALFA EM NEW", "office": "EM", "status": "registered", "applicationDate": "2022-01-15"},
            {"tmName": "ALFA EM OLD", "office": "EM", "status": "registered", "applicationDate": "2017-04-20"},
            {"tmName": "ALFA WO", "office": "WO", "status": "registered", "applicationDate": "2019-09-09"},
        ]

        analysis = agent.analyze("ALFA", marks, ["35"], requested_offices=["RO"])
        names = [m.get("tmName") for m in analysis["conflicts"]]

        self.assertEqual(names[0], "ALFA RO OLD")
        self.assertEqual(names[1], "ALFA RO NEW")
        self.assertEqual(names[2], "ALFA EM OLD")
        self.assertEqual(names[3], "ALFA EM NEW")
        self.assertEqual(names[4], "ALFA WO")


if __name__ == "__main__":
    unittest.main()
