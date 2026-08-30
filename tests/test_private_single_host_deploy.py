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
        self.assertIn("PYTHONPATH=/app/src", dockerfile)
        self.assertIn("COPY src ./src", dockerfile)
        self.assertIn("COPY fixtures ./fixtures", dockerfile)
        self.assertIn("USER auremgrid", dockerfile)
        self.assertIn("--host\", \"0.0.0.0\"", dockerfile)
        self.assertIn('CMD ["auremgrid", "serve"', dockerfile)
        self.assertNotIn('CMD ["python", "-m", "auremgrid"', dockerfile)
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
        self.assertIn('["auremgrid", "serve"', compose)
        self.assertIn('["auremgrid", "worker-loop"', compose)
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
        local_deployment = ROOT.joinpath("docs", "local-deployment.md").read_text(encoding="utf-8")
        operator_runtime = ROOT.joinpath("docs", "operator-runtime.md").read_text(encoding="utf-8")
        production_checklist = ROOT.joinpath("docs", "production-checklist.md").read_text(encoding="utf-8")
        self.assertIn("AUREMGRID_ORGANIZATION_ID=org_your_agency", env_example)
        self.assertIn("AUREMGRID_DOMAIN=localhost", env_example)
        self.assertIn("AUREMGRID_UPSTREAM=127.0.0.1:8791", env_example)
        self.assertIn(".env.example", prepare)
        self.assertIn("private_host_smoke.py", prepare)
        self.assertIn("Backup schedule not installed by this script", prepare)
        self.assertNotIn("Backup schedule set", prepare)
        self.assertIn("{$AUREMGRID_DOMAIN:localhost}", caddyfile)
        self.assertIn("{$AUREMGRID_UPSTREAM:127.0.0.1:8791}", caddyfile)
        self.assertIn("Copy `.env.example` to `deploy/.env`", local_deployment)
        self.assertIn("env_file: .env", local_deployment)
        self.assertIn("docker compose --env-file deploy/.env -f deploy/docker-compose.yml config --quiet", local_deployment)
        self.assertIn("Copy `.env.example` to `deploy/.env`", operator_runtime)
        self.assertIn("does not create", operator_runtime)
        self.assertIn("`backup`", operator_runtime)
        self.assertIn("`verify-backup`", operator_runtime)
        self.assertIn("Install an operator-owned daily backup schedule manually", production_checklist)
        self.assertIn("forward-migration rehearsal", production_checklist)
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

    def test_ci_builds_container_and_validates_private_host_compose(self) -> None:
        workflow = ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
        self.assertIn("private-host-container:", workflow)
        self.assertIn("cp .env.example deploy/.env", workflow)
        self.assertIn("docker compose --env-file deploy/.env -f deploy/docker-compose.yml config --quiet", workflow)
        self.assertIn("docker build -t auremgrid-company-os:ci .", workflow)
        self.assertIn("http://127.0.0.1:8791/health", workflow)
        self.assertIn("-p 127.0.0.1:8791:8791", workflow)
        self.assertIn("--read-only", workflow)
        self.assertIn("--cap-drop ALL", workflow)
        self.assertNotIn("--pull", workflow)
        for forbidden in ("AUREMGRID_ACCESS_TOKEN", "CLIENT_SECRET", "API_KEY"):
            self.assertNotIn(forbidden, workflow)

    def test_release_validate_runs_documented_non_browser_release_checks(self) -> None:
        release = ROOT.joinpath("scripts", "release.py").read_text(encoding="utf-8")
        for marker in (
            "compileall",
            "scripts/dashboard_showcase_svg.py",
            "auremgrid.cli",
            "evaluate-intelligence",
            "scripts/private_host_smoke.py",
            "unittest",
            "git\", \"diff\", \"--check",
            "git\", \"status\", \"--porcelain",
        ):
            self.assertIn(marker, release)


if __name__ == "__main__":
    unittest.main()
