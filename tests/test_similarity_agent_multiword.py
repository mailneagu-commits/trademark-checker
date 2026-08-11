import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from agents.similarity_agent import SimilarityAgent


class SimilarityAgentMultiwordTests(unittest.TestCase):
    def test_exact_query_word_inside_multiword_is_high_risk(self):
        agent = SimilarityAgent()
        trademarks = [{
            "tmName": "VELORA GROUP",
            "tmOffice": "RO",
            "niceClass": [35],
            "tradeMarkStatus": "registered",
        }]

        analysis = agent.analyze("VELORA", trademarks, ["35"])

        self.assertEqual(len(analysis["conflicts"]), 1)
        match = analysis["conflicts"][0]
        self.assertEqual(match["risk_level"], "high")
        self.assertTrue(match["similarity"]["exact_word_match"])
        self.assertGreaterEqual(match["similarity"]["combined_score"], agent.threshold_high)
        self.assertLess(match["similarity"]["combined_score"], agent.threshold_very_high)


if __name__ == "__main__":
    unittest.main()