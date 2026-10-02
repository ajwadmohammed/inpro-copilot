"""Publish InPro Copilot to a Hugging Face Space: free hosting with an https link that opens anywhere.

Two ways to run it:
  * Automatically, by GitHub Actions, after every push to the main branch (.github/workflows/deploy.yml).
    GitHub secrets used: HF_TOKEN (required), GEMINI_API_KEY and GROQ_API_KEY (optional).
  * By hand from the laptop: double-click deploy_hf.bat. It asks for your Hugging Face token once
    (typed by you, saved by Hugging Face's own login) and reads the AI keys from your .env file.

What it does: creates the Space if it does not exist (Docker type, public), stores the AI keys as Space
SECRETS (encrypted at Hugging Face, never in the code or the image), uploads the app, and with --wait
follows the build until the app is running, then checks that it answers.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ["GEMINI_API_KEY", "GROQ_API_KEY", "INPRO_OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
VARIABLES = ["INPRO_GEMINI_MODELS", "INPRO_GROQ_MODELS", "INPRO_GEMINI_PAID", "INPRO_LLM_DAILY_LIMIT",
             "INPRO_LLM_MONTHLY_BUDGET_USD", "INPRO_OPENAI_BASE_URL", "INPRO_OPENAI_MODEL", "INPRO_ANTHROPIC_MODELS"]
INCLUDE = ["Dockerfile", ".dockerignore", "requirements.txt", "src", "ui", "data/real", "data/synthetic",
           "eval/make_dataset.py", "TECH_STACK.md", "API_SETUP.md"]
FRONT_MATTER = """---
title: InPro Copilot
emoji: 🧾
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: AI pre-review for invoice approvals, with a fraud lab
---

"""


def dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.strip()
                if v[:1] in "\"'" and v[-1:] == v[:1]:
                    v = v[1:-1]
                elif " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                out[k.strip()] = v
    return out


def setting(name: str, local: dict[str, str]) -> str | None:
    v = os.getenv(name) or local.get(name)
    return v.strip() if v and v.strip() else None


def token() -> str | None:
    t = os.getenv("HF_TOKEN")
    if t:
        return t.strip()
    try:
        from huggingface_hub import get_token
        t = get_token()
    except Exception:
        t = None
    if t or not sys.stdin.isatty():
        return t
    print("\nPaste your Hugging Face token (Settings > Access Tokens, type 'Write').")
    print("Nothing appears while you paste: that is normal. Press Enter afterwards.")
    t = getpass.getpass("Token: ").strip()
    if t:
        from huggingface_hub import login
        login(token=t, add_to_git_credential=False)        # remembered by Hugging Face on this computer
    return t or None


def stage(folder: Path) -> None:
    for item in INCLUDE:
        src = ROOT / item
        if not src.exists():
            continue
        dst = folder / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    (folder / "README.md").write_text(FRONT_MATTER + readme, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--space", default=os.getenv("HF_SPACE") or "inpro-copilot", help="Space name (or owner/name)")
    ap.add_argument("--wait", action="store_true", help="follow the build until the app is running")
    args = ap.parse_args()

    from huggingface_hub import HfApi
    tok = token()
    if not tok:
        if os.getenv("GITHUB_ACTIONS"):
            print("::notice::HF_TOKEN is not set as a GitHub secret yet, so nothing was deployed. "
                  "Add it under Settings > Secrets and variables > Actions, then re-run this workflow.")
            return 0
        print("No Hugging Face token: nothing deployed.")
        return 1
    api = HfApi(token=tok)
    owner = api.whoami()["name"]
    repo_id = args.space if "/" in args.space else f"{owner}/{args.space}"
    local = dotenv()

    print(f"1/4  Space {repo_id}")
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True, private=False)

    print("2/4  Keys and settings (stored encrypted at Hugging Face as Space secrets; values are never printed)")
    for k in SECRETS:
        v = setting(k, local)
        if v:
            api.add_space_secret(repo_id, k, v)
            print(f"       {k}: saved")
    for k in VARIABLES:
        v = setting(k, local)
        if v:
            api.add_space_variable(repo_id, k, v)
            print(f"       {k} = {v}")

    print("3/4  Uploading the app")
    sha = (os.getenv("GITHUB_SHA") or "")[:7]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        stage(folder)
        api.upload_folder(folder_path=str(folder), repo_id=repo_id, repo_type="space",
                          commit_message=f"Deploy {sha or time.strftime('%Y-%m-%d %H:%M')}",
                          delete_patterns=["src/**", "ui/**", "data/**", "eval/**", "*.md", "Dockerfile", "requirements.txt"])

    host = getattr(api.space_info(repo_id), "host", None) or \
        "https://" + repo_id.replace("/", "-").replace("_", "-").replace(".", "-").lower() + ".hf.space"
    print(f"4/4  Building. The app will be at:\n\n       {host}\n")
    if not args.wait:
        print("     (Building takes 3 to 6 minutes. Progress: https://huggingface.co/spaces/" + repo_id + ")")
        return 0

    started, last = time.time(), None
    while time.time() - started < 20 * 60:
        stage_now = api.get_space_runtime(repo_id).stage
        if stage_now != last:
            print(f"       {time.strftime('%H:%M:%S')}  {stage_now}")
            last = stage_now
        if stage_now == "RUNNING":
            break
        if stage_now in ("BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"):
            print(f"The Space reported {stage_now}. Open https://huggingface.co/spaces/{repo_id} and check the logs.")
            return 1
        time.sleep(15)
    else:
        print("Still not running after 20 minutes; check the Space page.")
        return 1

    import urllib.request
    for _ in range(12):                       # the app needs a few seconds after the container starts
        try:
            with urllib.request.urlopen(host + "/api/health", timeout=10) as r:
                if r.status == 200:
                    print(f"Live and answering: {host}")
                    return 0
        except Exception:
            time.sleep(10)
    print("The Space is running but the app did not answer yet; open the link in a minute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
