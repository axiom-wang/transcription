"""Explicit, temporary OAuth access to public gated models; never persists a token."""

import os
import sys

from .diarization import MODEL_FILES, REPO, REVISION


def main():
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
    from huggingface_hub import constants, snapshot_download
    from huggingface_hub.utils._http import get_session
    from huggingface_hub.utils._oauth_device import poll_device_token

    response = get_session().post(
        constants.ENDPOINT + "/oauth/device",
        data={"client_id": constants.DEVICE_CODE_OAUTH_CLIENT_ID, "scope": "gated-repos"},
        timeout=20,
    )
    response.raise_for_status()
    info = response.json()
    info.setdefault("interval", 5)
    info.setdefault("expires_in", 300)
    print(f"请打开 {info['verification_uri']}，输入配对码：{info['user_code']}", flush=True)
    print(
        "仅授权读取你有权访问的公开受限模型；不申请仓库管理或账单权限，不保存账号令牌。", flush=True
    )
    token_response = poll_device_token(info)
    snapshot_download(
        REPO,
        revision=REVISION,
        token=token_response["access_token"],
        local_dir=sys.argv[1],
        allow_patterns=[*MODEL_FILES, "README.md", "*/README.md"],
        max_workers=2,
        force_download="--refresh" in sys.argv[2:],
    )
    print("下载完成；未保存账号令牌。", flush=True)


if __name__ == "__main__":
    main()
