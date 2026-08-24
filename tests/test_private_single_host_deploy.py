from __future__ import annotations

import importlib.util
import tempfile
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
        self.assertIn("AUREMGRID_UPSTREAM: web:8791", compose)
        self.assertIn("./Caddyfile:/etc/caddy/Caddyfile:ro", compose)
        self.assertIn("cap_drop: [\"ALL\"]", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotIn("ports: [\"8791:8791\"]", compose)

    def test_env_example_prepare_deploy_and_caddy_are_aligned_without_secrets(self) -> None:
        env_example = ROOT.joinpath(".env.example").read_text(encoding="utf-8")
        prepare = ROOT.joinpath("scripts", "prepare-deploy.ps1").read_text(encoding="utf-8")
        caddyfile = ROOT.joinpath("deploy", "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("AUREMGRID_ORGANIZATION_ID=org_your_agency", env_example)
        self.assertIn("AUREMGRID_DOMAIN=localhost", env_example)
        self.assertIn("AUREMGRID_UPSTREAM=127.0.0.1:8791", env_example)
        self.assertIn(".env.example", prepare)
        self.assertIn("private_host_smoke.py", prepare)
        self.assertIn("{$AUREMGRID_DOMAIN:localhost}", caddyfile)
        self.assertIn("{$AUREMGRID_UPSTREAM:127.0.0.1:8791}", caddyfile)
        for forbidden in ("AUREMGRID_ACCESS_TOKEN=", "PASSWORD=", "TOKEN=", "API_KEY=", "CLIENT_SECRET="):
            self.assertNotIn(forbidden, env_example)

    def test_python_private_host_smoke_rehearses_without_docker(self) -> None:
        script = ROOT.joinpath("scripts", "private_host_smoke.py")
        spec = importlib.util.spec_from_file_location("private_host_smoke", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            result = module.run_smoke(Path(directory))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["docker_required"])
        self.assertTrue(result["checks"]["health_ok"])
        self.assertTrue(result["checks"]["worker_succeeded"])
        self.assertTrue(result["checks"]["backup_verified"])
        self.assertTrue(result["checks"]["restore_recovery_mode"])
        self.assertTrue(result["checks"]["restore_outbound_disabled"])


if __name__ == "__main__":
    unittest.main()
