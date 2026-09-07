import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_sbom import generate_sbom, main


class SbomTests(unittest.TestCase):
    def test_bom_is_valid_cyclonedx_with_project(self):
        bom = generate_sbom()
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["specVersion"], "1.5")
        project = next(component for component in bom["components"] if component["name"] == "auremgrid-company-os")
        self.assertTrue(project["name"])
        self.assertTrue(project["version"])
        self.assertIsInstance(json.loads(json.dumps(bom)), dict)

    def test_main_writes_json_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bom.json"
            self.assertEqual(main(["--output", str(destination)]), 0)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["specVersion"], "1.5")


if __name__ == "__main__":
    unittest.main()
