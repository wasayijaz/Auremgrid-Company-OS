from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PrivateSingleHostDeployTests(unittest.TestCase):
    def test_dockerfile_packages_non_root_python_312_without_secret_material(self) -> None:
        dockerfile = ROOT.joinpath("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("USER auremgrid", dockerfile)
        self.assertIn("--host\", \"0.0.0.0\"", dockerfile)
        for forbidden in ("AUREMGRID_ACCESS_TOKEN", "SECRET", "PASSWORD", "TOKEN="):
            self.assertNotIn(forbidden, dockerfile)

    def test_dockerignore_excludes_local_state_secrets_and_tests(self) -> None:
        dockerignore = ROOT.joinpath(".dockerignore").read_text(encoding="utf-8")
        for marker in (".env", ".env.*", "*.sqlite", "*.key", "*.pem", "secrets", "tests", ".git"):
            self.assertIn(marker, dockerignore)

    def test_compose_builds_local_image_and_keeps_host_listener_private(self) -> None:
        compose = ROOT.joinpath("deploy", "docker-compose.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(compose.count("dockerfile: Dockerfile"), 2)
        self.assertIn('"0.0.0.0"', compose)
        self.assertIn('"127.0.0.1:443:443"', compose)
        self.assertIn("--to\", \"web:8791\"", compose)
        self.assertIn("cap_drop: [\"ALL\"]", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotIn("ports: [\"8791:8791\"]", compose)


if __name__ == "__main__":
    unittest.main()
