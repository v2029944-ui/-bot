"""Штатный набор COPY Docker должен включать все сервисы, а не зависеть от cwd репозитория."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def test_docker_sources_import_without_repository_on_pythonpath(tmp_path):
    root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for line in (root / "Dockerfile").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        _, *sources, target = shlex.split(line)
        dest = bundle / target
        dest.mkdir(parents=True, exist_ok=True)
        for source in sources:
            path = root / source
            if path.is_dir():
                shutil.copytree(path, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(path, dest / path.name)
    script = """import importlib
import services.review
import core, main, max_
for module, names in core._SERVICE_EXPORTS.items():
    service = importlib.import_module(module)
    for name in names:
        assert getattr(core, name) is getattr(service, name)
assert core.on_action.__module__ == 'services.review'
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(bundle)}
    subprocess.run([sys.executable, "-c", script], cwd=bundle, env=env, check=True,
                   capture_output=True, text=True, timeout=20)
