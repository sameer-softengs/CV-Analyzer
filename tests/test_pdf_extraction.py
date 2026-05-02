import shutil
import tempfile
import unittest
from pathlib import Path

import fitz

from ai import (
    analyze_cv_ats,
    assess_cv_document,
    detect_sections,
    extract_text_from_pdf,
    get_last_pdf_parse_debug,
    normalize_text,
)


class PdfExtractionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="resume_pdf_tests_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _pdf_path(self, name):
        return self.temp_dir / name

    def _create_plain_resume_pdf(self, path):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        text = (
            "Jane Doe\n"
            "jane.doe@example.com | +1 555 444 1212 | linkedin.com/in/janedoe\n\n"
            "SUMMARY\n"
            "Product-minded software engineer with 5 years of experience building backend systems.\n\n"
            "EXPERIENCE\n"
            "Senior Backend Engineer | 2020 - Present\n"
            "Built FastAPI services, improved API latency by 38%, and automated release workflows.\n\n"
            "EDUCATION\n"
            "B.Sc Computer Science, State University\n\n"
            "SKILLS\n"
            "Python, FastAPI, SQL, Docker, AWS\n"
        )
        page.insert_textbox(fitz.Rect(42, 40, 550, 790), text, fontsize=11, fontname="helv")
        doc.save(path)
        doc.close()

    def _create_two_column_resume_pdf(self, path):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)

        page.draw_rect(fitz.Rect(24, 100, 170, 810), color=(0.2, 0.4, 0.7), fill=(0.09, 0.15, 0.25))
        page.insert_textbox(
            fitz.Rect(36, 40, 560, 92),
            "ALEX MORGAN\nSenior Data Engineer",
            fontsize=20,
            fontname="helv",
        )
        left_text = (
            "CONTACT\n"
            "alex.morgan@example.com\n"
            "+1 222 333 4444\n"
            "github.com/alexmorgan\n\n"
            "SKILLS\n"
            "Python\nSQL\nSpark\nAirflow\nDocker\nAWS\n\n"
            "CERTIFICATIONS\n"
            "AWS Certified Developer\n"
        )
        right_text = (
            "SUMMARY\n"
            "Data engineer with 7 years of experience building analytics pipelines and cloud platforms.\n\n"
            "EXPERIENCE\n"
            "Lead Data Engineer | 2019 - Present\n"
            "Designed ETL pipelines, reduced reporting delays by 42%, and scaled event ingestion.\n\n"
            "PROJECTS\n"
            "Customer 360 Platform\n"
            "Built a multi-source warehouse with governance and observability.\n\n"
            "EDUCATION\n"
            "M.Sc Information Systems\n"
        )

        page.insert_textbox(fitz.Rect(34, 118, 162, 790), left_text, fontsize=10.5, fontname="helv")
        page.insert_textbox(fitz.Rect(190, 118, 560, 790), right_text, fontsize=11, fontname="helv")
        doc.save(path)
        doc.close()

    def _create_non_linear_resume_pdf(self, path):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)

        page.insert_textbox(
            fitz.Rect(40, 34, 560, 90),
            "PRIYA SHAH\npriya.shah@example.com | +91 99999 11111 | linkedin.com/in/priyashah",
            fontsize=18,
            fontname="helv",
        )
        page.draw_rect(fitz.Rect(36, 120, 200, 330), color=(0.4, 0.5, 0.8))
        page.draw_rect(fitz.Rect(220, 120, 558, 470), color=(0.4, 0.5, 0.8))
        page.draw_rect(fitz.Rect(36, 500, 558, 770), color=(0.4, 0.5, 0.8))

        page.insert_textbox(
            fitz.Rect(48, 132, 188, 320),
            "SKILLS\nPython\nReact\nTypeScript\nNode.js\nPostgreSQL\nFigma",
            fontsize=10.5,
            fontname="helv",
        )
        page.insert_textbox(
            fitz.Rect(232, 132, 546, 458),
            "EXPERIENCE\n"
            "Full Stack Engineer | 2021 - Present\n"
            "Built internal platforms, improved onboarding speed by 30%, and maintained CI/CD.\n\n"
            "SUMMARY\n"
            "Versatile engineer focused on product delivery, UX quality, and measurable outcomes.",
            fontsize=11,
            fontname="helv",
        )
        page.insert_textbox(
            fitz.Rect(48, 512, 546, 756),
            "PROJECTS\n"
            "Design System Revamp\n"
            "Unified components across products and reduced duplicate UI effort.\n\n"
            "EDUCATION\n"
            "B.Tech Computer Engineering\n\n"
            "ACHIEVEMENTS\n"
            "Received engineering excellence award in 2023.",
            fontsize=11,
            fontname="helv",
        )
        doc.save(path)
        doc.close()

    def test_plain_resume_extraction_preserves_basic_sections(self):
        pdf_path = self._pdf_path("plain_resume.pdf")
        self._create_plain_resume_pdf(pdf_path)

        text = extract_text_from_pdf(str(pdf_path))
        normalized = normalize_text(text)
        self.assertIn("jane.doe@example.com", normalized)
        self.assertIn("summary", normalized)
        self.assertIn("experience", normalized)
        self.assertIn("education", normalized)
        self.assertIn("skills", normalized)

        sections = detect_sections(text)
        for required in ["summary", "experience", "education", "skills"]:
            self.assertIn(required, sections["required_found"])

    def test_two_column_resume_extraction_captures_sidebar_and_main_sections(self):
        pdf_path = self._pdf_path("two_column_resume.pdf")
        self._create_two_column_resume_pdf(pdf_path)

        text = extract_text_from_pdf(str(pdf_path))
        normalized = normalize_text(text)
        self.assertIn("alex.morgan@example.com", normalized)
        self.assertIn("aws certified developer", normalized)
        self.assertIn("customer 360 platform", normalized)
        self.assertIn("data engineer", normalized)

        sections = detect_sections(text)
        for required in ["experience", "education", "skills"]:
            self.assertIn(required, sections["required_found"])

        parse_debug = get_last_pdf_parse_debug()
        self.assertGreaterEqual(parse_debug.get("multi_column_pages", 0), 1)
        self.assertIn(parse_debug.get("strategy"), {"layout_aware", "plain_text_fallback"})

    def test_non_linear_resume_maps_content_to_expected_fields(self):
        pdf_path = self._pdf_path("non_linear_resume.pdf")
        self._create_non_linear_resume_pdf(pdf_path)

        text = extract_text_from_pdf(str(pdf_path))
        sections = detect_sections(text)
        report = analyze_cv_ats(text, use_llm=False)
        validation = assess_cv_document(text)

        self.assertTrue(validation["is_cv"])
        for required in ["experience", "education", "skills"]:
            self.assertIn(required, sections["required_found"])
        self.assertNotIn("experience", report["missing_sections"])
        self.assertNotIn("education", report["missing_sections"])
        self.assertNotIn("skills", report["missing_sections"])
        self.assertGreater(report["ats_score"], 40)


if __name__ == "__main__":
    unittest.main()
