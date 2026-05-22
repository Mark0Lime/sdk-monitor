import requests
import os
import sys
import traceback
import json
from packaging.version import parse as parse_version
import subprocess

def normalize_version(version):
    # Remove leading 'v' or 'V'
    if version.startswith(('v', 'V')):
        version = version[1:]
    # Remove trailing '.0'
    while version.endswith('.0'):
        version = version[:-2]
    # Strip pre-release or build metadata (e.g., -beta, +build)
    version = version.split('-')[0].split('+')[0]
    # Remove prefixes like 'AdjustSDK', 'release', 'sdk'
    for prefix in ['AdjustSDK', 'release', 'Release', 'sdk', 'SDK']:
        if version.startswith(prefix):
            version = version[len(prefix):]
    # Replace underscores or other separators with dots
    version = version.replace('_', '.')
    # Ensure version is numeric with dots, trim invalid characters
    parts = [p for p in version.split('.') if p.isdigit()]
    version = '.'.join(parts) if parts else version
    # Fallback if version is still invalid
    if not all(c.isdigit() or c == '.' for c in version):
        print(f"⚠️ 版本号 {version} 仍无效，尝试提取数字")
        version = ''.join(c for c in version if c.isdigit() or c == '.').strip('.')
    return version

def fetch_remote_versions():
    try:
        subprocess.run(["git", "fetch", "origin", "main"], check=True)
        result = subprocess.run(
            ["git", "show", "origin/main:versions.json"],
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError:
        print("⚠️ Could not fetch remote versions.json, assuming empty file")
        return {}
    except json.JSONDecodeError:
        print("⚠️ Remote versions.json is invalid, assuming empty file")
        return {}

def read_versions():
    version_file = "versions.json"
    if os.path.exists(version_file):
        with open(version_file, "r") as f:
            return json.load(f)
    return {}

def write_versions(versions):
    with open("versions.json", "w") as f:
        json.dump(versions, f, indent=2)
    print("✅ 已更新 versions.json 文件")

def main():
    try:
        REPO = os.getenv("REPO")
        if not REPO:
            print("❌ 环境变量 REPO 未设置")
            sys.exit(2)

        API_URL = f"https://api.github.com/repos/{REPO}/releases"
        headers = {
            "Accept": "application/vnd.github.v3+json"
        }
        github_token = os.getenv("GITHUB_TOKEN")
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        print(f"📡 请求 API: {API_URL}")
        response = requests.get(API_URL, headers=headers, timeout=10)
        if response.status_code == 403 and 'X-RateLimit-Remaining' in response.headers:
            if int(response.headers['X-RateLimit-Remaining']) == 0:
                print(f"❌ GitHub API 速率限制已用尽，剩余: {response.headers['X-RateLimit-Remaining']}")
                sys.exit(1)
        if response.status_code != 200:
            print(f"❌ 请求失败，状态码: {response.status_code}")
            print(f"错误信息: {response.text}")
            sys.exit(1)

        releases = response.json()
        print(f"📋 获取到 {len(releases)} 个发布版本")
        if not isinstance(releases, list):
            print(f"❌ API 返回非列表数据: {releases}")
            sys.exit(1)
        if not releases:
            print("❌ 该仓库没有发布版本")
            sys.exit(0)

        non_prereleases = [r for r in releases if not r.get('prerelease', False)]
        print(f"📋 找到 {len(non_prereleases)} 个非预发布版本")
        if not non_prereleases:
            print("无正式发布版本")
            sys.exit(0)
        try:
            latest_release = sorted(non_prereleases, key=lambda x: x['published_at'], reverse=True)[0]
            print(f"📄 最新发布数据: {json.dumps(latest_release, indent=2)}")
            latest_version = latest_release['tag_name']
            release_url = latest_release['html_url']
        except (KeyError, TypeError) as e:
            print(f"❌ 发布数据格式错误: {e}")
            sys.exit(1)

        remote_versions = fetch_remote_versions()
        local_versions = read_versions()
        versions = remote_versions.copy()
        versions.update(local_versions)

        repo_key = REPO.replace("/", "_")
        saved_version = versions.get(repo_key)

        if not saved_version:
            print(f"📌 初次运行，记录最新版本: {latest_version}")
            versions[repo_key] = latest_version
            try:
                with open(os.environ['GITHUB_ENV'], 'a') as env_file:
                    env_file.write(f"NEW_VERSION={latest_version}\n")
                    env_file.write(f"RELEASE_URL={release_url}\n")
                    env_file.write(f"SDK={REPO}\n")
                    env_file.write("VERSION_UPDATED=true\n")
                write_versions(versions)
            except Exception as e:
                print(f"❌ 无法设置环境变量: {e}")
                sys.exit(1)
            sys.exit(0)

        print(f"原始保存版本: {saved_version}")
        print(f"原始最新版本: {latest_version}")
        norm_saved = normalize_version(saved_version)
        norm_latest = normalize_version(latest_version)
        print(f"规范化保存版本: {norm_saved}")
        print(f"规范化最新版本: {norm_latest}")
        try:
            current_ver = parse_version(norm_saved)
            latest_ver = parse_version(norm_latest)
        except packaging.version.InvalidVersion as e:
            print(f"❌ 无效版本号: saved_version={saved_version}, latest_version={latest_version}, 错误: {e}")
            sys.exit(1)

        print(f"🔍 版本比较: {latest_ver} > {current_ver} = {latest_ver > current_ver}")

        if latest_ver > current_ver:
            print(f"🎉 发现新版本: {latest_version}")
            versions[repo_key] = latest_version
            try:
                with open(os.environ['GITHUB_ENV'], 'a') as env_file:
                    env_file.write(f"NEW_VERSION={latest_version}\n")
                    env_file.write(f"RELEASE_URL={release_url}\n")
                    env_file.write(f"SDK={REPO}\n")
                    env_file.write("VERSION_UPDATED=true\n")
                write_versions(versions)
            except Exception as e:
                print(f"❌ 无法设置环境变量: {e}")
                sys.exit(1)
        else:
            print(f"✅ 当前已是最新版本: {latest_version}")

    except Exception as e:
        print(f"❌ 发生错误: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
