"""命令行端到端测试。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

import support
from protocol_schema_evolution_lake.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.lake = Path(self.tempdir.name) / "lake"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, *args: str) -> int:
        return main(list(args))

    def _fixture_args(self, name: str) -> list[str]:
        root = support.FIXTURES / name
        args = ["--data", str(root)]
        if (root / "semantic_reviews.csv").is_file():
            args += ["--reviews", str(root / "semantic_reviews.csv")]
        if (root / "unit_conversions.csv").is_file():
            args += ["--conversions", str(root / "unit_conversions.csv")]
        if (root / "mapping_adjudications.csv").is_file():
            args += ["--adjudications", str(root / "mapping_adjudications.csv")]
        return args

    def test_build_reports_comparability(self) -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self._run("build", "--name", "coherent", *self._fixture_args("coherent"))
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("G-v1-F-01", payload["comparable_groups"])
        self.assertTrue(payload["total_rows"] > 0)

    def test_publish_verify_trace_round_trip(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                self._run(
                    "publish", "--name", "coherent", "--lake", str(self.lake),
                    "--dataset-version", "v1", *self._fixture_args("coherent"),
                ),
                0,
            )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(
                self._run(
                    "verify", "--name", "coherent", "--lake", str(self.lake),
                    "--dataset-version", "v1",
                ),
                0,
            )
        self.assertTrue(json.loads(buffer.getvalue())["ok"])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(
                self._run(
                    "trace", "--name", "coherent", "--lake", str(self.lake),
                    "--dataset-version", "v1", "--run-id", "RUN-V2-01",
                ),
                0,
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["run"]["run_id"], "RUN-V2-01")

    def test_verify_reports_failure_exit_code(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self._run(
                "publish", "--name", "coherent", "--lake", str(self.lake),
                "--dataset-version", "v1", *self._fixture_args("coherent"),
            )
        manifest = self.lake / "datasets" / "coherent" / "v1" / "manifest.json"
        manifest.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        partition = sorted((self.lake / "datasets" / "coherent" / "v1" / "partitions").glob("*.csv"))[0]
        partition.write_text(partition.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self._run(
                "verify", "--name", "coherent", "--lake", str(self.lake),
                "--dataset-version", "v1",
            )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_lineage_outputs_hashes(self) -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self._run("lineage", *self._fixture_args("coherent"))
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("registry", payload["fingerprints"])
        self.assertEqual(len(payload["protocol_lineage"]["families"]), 1)

    def test_catalog_lists_published(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self._run(
                "publish", "--name", "coherent", "--lake", str(self.lake),
                "--dataset-version", "v1", *self._fixture_args("coherent"),
            )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(self._run("catalog", "--lake", str(self.lake)), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["datasets"]["coherent"]["latest"], "v1")


if __name__ == "__main__":
    unittest.main()
