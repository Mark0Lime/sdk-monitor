import json
import os
import re
import subprocess
import sys
import traceback
from typing import Any, Dict, Optional, Tuple

import requests
from packaging.version import InvalidVersion, parse as parse_version


VERSIONS_FILE = "versions.json"
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main")


def set_github_env(key: str, value: str) -> None:
    """
    写入 GitHub Actions 的跨 step 环境变量。
    注意：os.environ 只对当前 Python 进程有效，后续 step 读取不到。
    """
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        print(f"⚠️ GITHUB_ENV 未设置，跳过写入环境变量: {key}={value}")
        return

    value = "" if value is None else str(value)

    with open(github_env, "a", encoding="utf-8") as f:
        # 多行值兼容写法
        if "\n" in value:
            delimiter = f"EOF_{key}"
            f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            f.write(f"{key}={value}\n")


def set_default_env() -> None:
    """
    默认设置，避免后续 step 读取到空值或上一次残留逻辑误判。
    """
    set_github_env("VERSION_UPDATED", "false")
    set_github_env("SDK", "")
    set_github_env("NEW_VERSION", "")
    set_github_env("RELEASE_URL", "")


def normalize_version(version: str) -> str:
    """
    将 GitHub release tag 尽量转换成 packaging 可比较的版本号。
    例如：
    v12.3.0 -> 12.3.0
    SDK-1.2.3 -> 1.2.3
    AdjustSDK5.4.0 -> 5.4.0
    """
    if not version:
        return ""

    original = version.strip()

    # 去掉常见前缀
    cleaned = original.strip()
    cleaned = re.sub(r"^(refs/tags/)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(release[-_/ ]*)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(sdk[-_/ ]*)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(adjustsdk[-_/ ]*)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[vV]", "", cleaned)

    # 下划线转点
    cleaned = cleaned.replace("_", ".")

    # 去掉 build metadata
    cleaned = cleaned.split("+")[0]

    # 先尝试从字符串中提取第一段版本号
    # 支持 1、1.2、1.2.3、1.2.3-beta.1
    match = re.search(r"\d+(?:\.\d+)*(?:[-.]?(?:alpha|beta|rc|preview)\.?\d*)?", cleaned, re.IGNORECASE)
    if match:
        cleaned = match.group(0)

    # packaging 支持 1.0rc1，但不太喜欢 1.0-rc.1，这里做一点转换
    cleaned = cleaned.replace("-rc.", "rc")
    cleaned = cleaned.replace("-rc", "rc")
    cleaned = cleaned.replace("-beta.", "b")
    cleaned = cleaned.replace("-beta", "b")
    cleaned = cleaned.replace("-alpha.", "a")
    cleaned = cleaned.replace("-alpha", "a")

    cleaned = cleaned.strip(".")

    if not cleaned:
        print(f"⚠️ 无法从版本号中提取有效数字: {original}")
        return original

    return cleaned


def run_git_command(args, check: bool = True) -> subprocess.CompletedProcess:
    print(f"🔧 执行 git 命令: {' '.join(args)}")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
    )


def fetch_remote_versions() -> Dict[str, Any]:
    """
    从远程 origin/main 读取 versions.json，避免本地 checkout 落后。
    如果失败，则退回本地文件。
    """
    try:
        run_git_command(["git", "fetch", "origin", DEFAULT_BRANCH], check=True)

        result = run_git_command(
            ["git", "show", f"origin/{DEFAULT_BRANCH}:{VERSIONS_FILE}"],
            check=True,
        )

        if not result.stdout.strip():
            print("⚠️ 远程 versions.json 为空，使用空字典")
            return {}

        return json.loads(result.stdout)

    except subprocess.CalledProcessError as e:
        print("⚠️ 无法读取远程 versions.json，将尝试读取本地文件")
        print(f"stdout: {e.stdout}")
        print(f"stderr: {e.stderr}")
        return read_versions()

    except json.JSONDecodeError as e:
        print(f"⚠️ 远程 versions.json JSON 格式错误: {e}，将尝试读取本地文件")
        return read_versions()


def read_versions() -> Dict[str, Any]:
    if not os.path.exists(VERSIONS_FILE):
        print(f"⚠️ 本地 {VERSIONS_FILE} 不存在，使用空字典")
        return {}

    try:
        with open(VERSIONS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            print(f"⚠️ 本地 {VERSIONS_FILE} 为空，使用空字典")
            return {}

        return json.loads(content)

    except json.JSONDecodeError as e:
        print(f"❌ 本地 {VERSIONS_FILE} JSON 格式错误: {e}")
        sys.exit(1)


def write_versions(versions: Dict[str, Any]) -> None:
    with open(VERSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(versions, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"✅ 已更新 {VERSIONS_FILE}")


def fetch_latest_release(repo: str) -> Tuple[Optional[str], Optional[str]]:
    """
    获取 GitHub 仓库最新正式 release。
    返回: tag_name, html_url
    """
    api_url = f"https://api.github.com/repos/{repo}/releases"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sdk-release-checker",
    }

    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    print(f"📡 请求 API: {api_url}")

    response = requests.get(api_url, headers=headers, timeout=20)

    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    print(f"GitHub API status: {response.status_code}, rate remaining: {remaining}, reset: {reset}")

    if response.status_code == 403 and remaining == "0":
        print("❌ GitHub API 速率限制已用尽")
        print(response.text)
        sys.exit(1)

    if response.status_code == 401:
        print("❌ GitHub Token 无效或已过期")
        print(response.text)
        sys.exit(1)

    if response.status_code != 200:
        print(f"❌ 请求失败，状态码: {response.status_code}")
        print(f"错误信息: {response.text}")
        sys.exit(1)

    releases = response.json()

    if not isinstance(releases, list):
        print(f"❌ API 返回非列表数据: {json.dumps(releases, indent=2, ensure_ascii=False)}")
        sys.exit(1)

    print(f"📋 获取到 {len(releases)} 个发布版本")

    if not releases:
        print("⚠️ 该仓库没有 release")
        return None, None

    non_prereleases = [
        r for r in releases
        if not r.get("prerelease", False) and not r.get("draft", False)
    ]

    print(f"📋 找到 {len(non_prereleases)} 个正式发布版本")

    if not non_prereleases:
        print("⚠️ 没有正式发布版本")
        return None, None

    latest_release = sorted(
        non_prereleases,
        key=lambda x: x.get("published_at") or "",
        reverse=True,
    )[0]

    latest_version = latest_release.get("tag_name")
    release_url = latest_release.get("html_url")

    if not latest_version or not release_url:
        print("❌ 发布数据缺少 tag_name 或 html_url")
        print(json.dumps(latest_release, indent=2, ensure_ascii=False))
        sys.exit(1)

    print(f"📦 最新正式版本: {latest_version}")
    print(f"🔗 Release URL: {release_url}")

    return latest_version, release_url


def is_newer_version(latest_version: str, saved_version: str) -> bool:
    norm_latest = normalize_version(latest_version)
    norm_saved = normalize_version(saved_version)

    print(f"原始保存版本: {saved_version}")
    print(f"原始最新版本: {latest_version}")
    print(f"规范化保存版本: {norm_saved}")
    print(f"规范化最新版本: {norm_latest}")

    try:
        current_ver = parse_version(norm_saved)
        latest_ver = parse_version(norm_latest)
    except InvalidVersion as e:
        print(f"⚠️ 标准版本号比较失败: {e}")
        print("⚠️ 将退回到字符串比较，只要 tag 不一致就认为有更新")
        return latest_version != saved_version

    print(f"🔍 版本比较: {latest_ver} > {current_ver} = {latest_ver > current_ver}")

    return latest_ver > current_ver


def main() -> None:
    set_default_env()

    repo = os.getenv("REPO")
    if not repo:
        print("❌ 环境变量 REPO 未设置")
        sys.exit(2)

    print(f"🚀 开始检查仓库: {repo}")

    latest_version, release_url = fetch_latest_release(repo)

    if not latest_version or not release_url:
        print(f"⚠️ {repo} 没有可用正式 release，跳过")
        sys.exit(0)

    remote_versions = fetch_remote_versions()
    local_versions = read_versions()

    # 远程作为基础，本地覆盖，避免 checkout 已经有修改时丢失
    versions = remote_versions.copy()
    versions.update(local_versions)

    repo_key = repo.replace("/", "_")
    saved_version = versions.get(repo_key)

    if not saved_version:
        print(f"📌 初次运行，仅记录最新版本，不发送更新通知: {latest_version}")
        versions[repo_key] = latest_version
        write_versions(versions)

        set_github_env("VERSION_UPDATED", "false")
        set_github_env("SDK", repo)
        set_github_env("NEW_VERSION", latest_version)
        set_github_env("RELEASE_URL", release_url)
        sys.exit(0)

    if is_newer_version(latest_version, saved_version):
        print(f"🎉 发现新版本: {repo} {saved_version} -> {latest_version}")

        versions[repo_key] = latest_version
        write_versions(versions)

        set_github_env("VERSION_UPDATED", "true")
        set_github_env("SDK", repo)
        set_github_env("NEW_VERSION", latest_version)
        set_github_env("RELEASE_URL", release_url)

        sys.exit(0)

    print(f"✅ 当前已是最新版本: {repo} {saved_version}")
    set_github_env("VERSION_UPDATED", "false")
    set_github_env("SDK", repo)
    set_github_env("NEW_VERSION", latest_version)
    set_github_env("RELEASE_URL", release_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 发生未捕获错误: {e}")
        traceback.print_exc()
        sys.exit(1)
