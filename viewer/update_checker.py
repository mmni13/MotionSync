import requests

GITHUB_API = "https://api.github.com/repos/mmni13/MotionSync/releases/latest"


def get_latest_version():
    try:
        response = requests.get(GITHUB_API, timeout=5)
        response.raise_for_status()

        data = response.json()

        return {
            "version": data["tag_name"].replace("v", ""),
            "url": data["html_url"]
        }

    except Exception:
        return None