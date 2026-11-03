"""Check the real local protocol without login or a model request."""
import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_images.rpc import AppServer
from codex_images.service import CONFIG


async def main(executable):
    with tempfile.TemporaryDirectory(prefix="astrbot-codex-smoke-") as directory:
        root = Path(directory)
        home, job = root / "codex", root / "job"
        home.mkdir()
        job.mkdir()
        (home / "config.toml").write_text(CONFIG, encoding="utf-8")
        rpc = AppServer(executable, home, job)
        try:
            await rpc.start()
            account = (await rpc.request("account/read", {"refreshToken": False})).get("account")
            if account is not None:
                raise RuntimeError("isolated runtime unexpectedly has an account")
            print("PASS: initialize, account/read (signed out), isolated CODEX_HOME")
        finally:
            await rpc.close()
        print("PASS: subprocess shutdown")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    asyncio.run(main(parser.parse_args().codex))
