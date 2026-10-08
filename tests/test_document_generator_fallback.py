import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.documents import generator


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self._current = rows

    def execute(self, query, *_args, **_kwargs):
        if "table_info" in query:
            self._current = [(0, "id"), (1, "status"), (2, "url"), (3, "liveness_status"), (4, "liveness_checked_at"), (5, "liveness_http_code"), (6, "liveness_detail")]
        elif "liveness_status" in query:
            from datetime import datetime, timezone
            now_ts = datetime.now(timezone.utc).isoformat()
            self._current = [("https://example.org/1", "ACTIVE", now_ts, 200, "liveness-v2:positive-posting - Page accessible and application open")]
        else:
            self._current = self.rows
        return self

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None

    def commit(self):
        pass

    def close(self):
        pass


class _Tracker:
    VERSION_METADATA = {}

    def __init__(self, _job_id):
        pass

    def start_stage(self, *_args, **_kwargs):
        pass

    def end_stage(self, *_args, **_kwargs):
        pass

    def complete(self):
        pass

    def to_metrics_json(self):
        return {}


class DocumentGeneratorFallbackTests(unittest.TestCase):
    def test_api_failure_writes_fallback_apply_info(self):
        desc = "Research Assistant role in computational biology. Working language is English. Python required. No prior experience required."
        row = (
            1, "evaluated", "Research Assistant", "Example Lab", "Berlin", desc,
            "", "", "", "", 75, "success", '{"is_match": true, "match_score": 75}',
        )
        con = _Connection([row])
        fallback = {"notes": "", "vault_documents_needed": [], "application_instructions": "Apply online"}

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            with (
                patch.object(generator, "OUTPUT", output),
                patch.object(generator, "load_master", return_value="master cv"),
                patch.object(generator, "get_connection", return_value=con),
                patch.object(generator, "call_claude", side_effect=RuntimeError("provider unavailable")),
                patch.object(generator, "fallback_document", return_value=fallback),
                patch.object(generator, "render_cv", return_value="cv"),
                patch.object(generator, "render_cover_letter", return_value="letter"),
                patch.object(generator, "render_application_prep", return_value="prep"),
                patch.object(generator, "process_vault_documents", return_value=[]),
                patch.object(generator, "generate_cv_docx"),
                patch.object(generator, "to_docx"),
                patch.object(generator, "convert_docx_to_pdf"),
                patch("src.observability.telemetry.PipelineRunTracker", _Tracker),
                patch(
                    "src.extraction.job_parser.parse_and_validate_job",
                    return_value=SimpleNamespace(application_target=None, sanitized_text="Description: Description"),
                ),
            ):
                generator.run()

            apply_info_files = list(output.rglob("apply_info.json"))
            self.assertEqual(len(apply_info_files), 1)
            self.assertEqual(json.loads(apply_info_files[0].read_text(encoding="utf-8"))["instructions"], "Apply online")


if __name__ == "__main__":
    unittest.main()
