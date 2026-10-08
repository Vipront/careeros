import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestSemanticNGS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.matcher import semantic
        cls.semantic = semantic

    def _classify_text(self, text, default_sem=0.35):
        # Mock semantic scores for the 3 domains
        scores = {
            "Bioinformatics": default_sem,
            "Wet Lab": default_sem,
            "Drug Design": default_sem * 0.5,
        }
        return self.semantic.classify(text, scores)

    def test_test1_next_generation_sequencing_data_analysis_is_bioinformatics(self):
        text = "Next Generation Sequencing data analysis"
        scores = {"Bioinformatics": 0.45, "Wet Lab": 0.20, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Bioinformatics")

    def test_test2_ngs_library_preparation_is_wet_lab(self):
        text = "NGS library preparation and quality control in lab"
        scores = {"Bioinformatics": 0.20, "Wet Lab": 0.45, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Wet Lab")

    def test_test3_molecular_diagnostics_is_wet_lab(self):
        text = "Molecular diagnostics assays and qPCR"
        scores = {"Bioinformatics": 0.20, "Wet Lab": 0.45, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Wet Lab")

    def test_test4_variant_calling_is_bioinformatics(self):
        text = "Variant calling from sequencing data"
        scores = {"Bioinformatics": 0.45, "Wet Lab": 0.20, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Bioinformatics")

    def test_test5_generic_software_incidental_ngs_is_other(self):
        text = "Software Engineer building billing tools for an NGS company"
        scores = {"Bioinformatics": 0.35, "Wet Lab": 0.20, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Other")

    def test_test6_medical_sales_is_other(self):
        text = "Medical sales representative for oncology and NGS products"
        scores = {"Bioinformatics": 0.35, "Wet Lab": 0.35, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Other")

    def test_test7_singleton_ngs_is_other(self):
        text = "NGS"
        scores = {"Bioinformatics": 0.40, "Wet Lab": 0.30, "Drug Design": 0.10}
        category, score, hits, ml_hits = self.semantic.classify(text, scores)
        self.assertEqual(category, "Other")

if __name__ == "__main__":
    unittest.main()
