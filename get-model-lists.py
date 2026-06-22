from huggingface_hub import HfApi
import json
import os
import requests
import re
from urllib.parse import urljoin, urlparse

# --- 配置部分 ---
HF_TOKEN = os.getenv("HF_TOKEN")
CNB_COOKIE = ""
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# repos 支持三种来源的混合输入：
#
# 1. Hugging Face:
#    - 短格式: "FireRedTeam/FireRed-Image-Edit-1.1-ComfyUI"
#    - 完整链接: "https://huggingface.co/FireRedTeam/FireRed-Image-Edit-1.1-ComfyUI/tree/main"
#
# 2. CNB (仅完整链接):
#    - "https://cnb.cool/SKDZSS90/CNB-Qwen-Image/-/tree/main"
#
# 3. ModelScope / 魔搭社区:
#    - 短格式: "Comfy-Org/SCAIL-2"
#    - 完整链接: "https://www.modelscope.cn/models/Comfy-Org/SCAIL-2/files"
#
repos = [
    "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/tree/main",
]

# 魔搭社区默认分支
MODELSCOPE_DEFAULT_REVISION = "master"

# --- 功能函数 ---

def get_save_path(filename, ref_path=""):
    """
    根据路径优先原则决定保存位置。
    ref_path: 文件的 URL 或相对路径 (例如 .../split_files/text_encoders/...)
    """
    filename = os.path.basename(filename)
    ref_path = ref_path.lower()
    filename_lower = filename.lower()

    # --- 优先级 1：根据目录结构 ---

    # 1.1 Text Encoders
    if "text_encoder" in ref_path:
        return f"models/text_encoders/{filename}"

    # 1.2 VAE
    if "vae" in ref_path:
        return f"models/vae/{filename}"

    # 1.3 LoRA
    if "lora" in ref_path:
        return f"models/loras/{filename}"

    # 1.4 Diffusion Models / UNET
    if "diffusion_models" in ref_path:
        return f"models/diffusion_models/{filename}"
    if "unet" in ref_path:
        return f"models/unet/{filename}"

    # --- 优先级 2：文件后缀与特征 (Fallback) ---

    # 2.1 GGUF (通常是 UNET)
    if filename.endswith(".gguf"):
        return f"models/unet/{filename}"

    # 2.2 VAE 文件特征
    if "vae" in filename_lower and (filename.endswith(".pt") or filename.endswith(".safetensors")):
        return f"models/vae/{filename}"

    # 2.3 Text Encoder 文件特征 (常见编码器名字)
    if any(k in filename_lower for k in ["t5", "clip", "bert", "ul2", "qwen"]):
        # 特例防御：如果 Qwen 文件名带 image，通常是 DiT 主模型
        if "qwen" in filename_lower and "image" in filename_lower:
            return f"models/diffusion_models/{filename}"
        return f"models/text_encoders/{filename}"

    # 2.4 LoRA 文件特征
    if "lora" in filename_lower:
        return f"models/loras/{filename}"

    # 2.5 默认归类
    return f"models/diffusion_models/{filename}"


# ============================================================
#  Hugging Face
# ============================================================

def parse_hf_url(repo_input):
    """
    解析 Hugging Face 链接，提取 repo_id 和 revision。
    支持:
      - 短格式: "FireRedTeam/FireRed-Image-Edit-1.1-ComfyUI"
      - 完整链接: "https://huggingface.co/FireRedTeam/FireRed-Image-Edit-1.1-ComfyUI/tree/main"
    返回 (repo_id, revision)
    """
    revision = "main"

    if repo_input.startswith("http"):
        parsed = urlparse(repo_input)
        path_parts = parsed.path.strip("/").split("/")
        # URL 格式: /{namespace}/{model_name}/tree/{revision} 或 /{namespace}/{model_name}
        if len(path_parts) >= 2:
            repo_id = f"{path_parts[0]}/{path_parts[1]}"
            # 检查是否有 /tree/{revision} 或 /blob/{revision}
            if len(path_parts) >= 4 and path_parts[2] in ("tree", "blob"):
                revision = path_parts[3]
        else:
            raise ValueError(f"无法解析 Hugging Face 链接: {repo_input}")
    else:
        repo_id = repo_input.strip("/")

    return repo_id, revision


def process_hf_repo(api, repo_input, result_list):
    """处理 Hugging Face 仓库"""
    try:
        repo_id, revision = parse_hf_url(repo_input)
    except ValueError as e:
        print(f"  [ERROR] {e}")
        return

    print(f"正在处理 Hugging Face 仓库: {repo_id} (分支: {revision}) ...")
    try:
        repo_info = api.repo_info(repo_id=repo_id, repo_type="model", files_metadata=True)
    except Exception as e:
        print(f"  [ERROR] 无法获取 HF 仓库信息: {e}")
        return

    for file_info in repo_info.siblings:
        if file_info.rfilename.endswith((".safetensors", ".gguf")):
            rfilename = file_info.rfilename
            url = f"https://huggingface.co/{repo_id}/resolve/{revision}/{rfilename}"
            filename_only = os.path.basename(rfilename)

            sha256 = file_info.lfs.get("sha256", "N/A") if file_info.lfs else "N/A"
            size_bytes = file_info.lfs.get("size", 0) if file_info.lfs else 0
            size_gb = round(size_bytes / (1024 ** 3), 2)

            save_path = get_save_path(filename_only, ref_path=rfilename)

            print(f"  -> 找到文件: {filename_only} | 路径: {save_path} | 大小: {size_gb}GB")

            result_list.append({
                "source": "huggingface",
                "repo": repo_id,
                "filename": filename_only,
                "url": url,
                "path": save_path,
                "sha256": sha256,
                "checksum_type": "sha256",
                "size": f"{size_gb}GB"
            })


# ============================================================
#  CNB
# ============================================================

def get_cnb_headers():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://cnb.cool/"
    }
    if CNB_COOKIE:
        headers["Cookie"] = CNB_COOKIE
    return headers


def process_cnb_url(url, result_list):
    print(f"正在处理 CNB 链接: {url} ...")
    headers = get_cnb_headers()

    parsed = urlparse(url)
    repo_base_path = "/".join(parsed.path.split("/")[:4])

    visited_urls = set()
    processed_files = set()

    try:
        if "/blob/" in url and url.endswith((".gguf", ".safetensors")):
            print("  检测到单个文件链接，直接解析...")
            parse_cnb_file_page(url, headers, result_list)
        else:
            print("  检测到目录或主页，开始递归扫描...")
            parse_cnb_repo_recursive(url, headers, result_list, visited_urls, processed_files, repo_base_path)
    except Exception as e:
        print(f"  [ERROR] 处理 CNB 出错: {e}")


def parse_cnb_file_page(file_page_url, headers, result_list):
    """解析单个 CNB 文件详情页"""
    try:
        response = requests.get(file_page_url, headers=headers, timeout=10)
    except Exception:
        print(f"  [ERROR] 请求超时: {file_page_url}")
        return

    if response.status_code != 200:
        return

    html = response.text
    filename = os.path.basename(urlparse(file_page_url).path)

    sha_match = re.search(r'SHA256\s*[:：]?\s*([a-fA-F0-9]{64})', html)
    sha256 = sha_match.group(1) if sha_match else "N/A"

    size_str = "N/A"
    size_match_a = re.search(r'(?:文件大小|Size)\s*[:：]?\s*(?:<[^>]+>|\s|&nbsp;)*([\d\.]+\s*[KMGT]?i?B)', html, re.IGNORECASE)
    if size_match_a:
        size_str = size_match_a.group(1)
    else:
        size_match_b = re.search(r'(\d+(?:\.\d+)?\s*(?:GiB|MiB))', html)
        if size_match_b:
            size_str = size_match_b.group(1)

    if sha256 != "N/A":
        if "/-/blob/" in file_page_url:
            repo_base = file_page_url.split("/-/blob/")[0]
        elif "/blob/" in file_page_url:
            repo_base = file_page_url.split("/blob/")[0].rstrip("/")
        else:
            repo_base = os.path.dirname(file_page_url)
        download_url = f"{repo_base}/-/lfs/{sha256}?name={filename}"
    else:
        print(f"  [WARN] 未找到 SHA256，使用备用 raw 链接: {filename}")
        download_url = file_page_url.replace("/blob/", "/raw/").replace("/-/raw/", "/raw/")

    save_path = get_save_path(filename, ref_path=file_page_url)

    print(f"  -> 找到文件: {filename} | 路径: {save_path}")

    result_list.append({
        "source": "cnb.cool",
        "repo": "cnb_recursive",
        "filename": filename,
        "url": download_url,
        "path": save_path,
        "sha256": sha256,
        "checksum_type": "sha256" if sha256 != "N/A" else "N/A",
        "size": size_str
    })


def parse_cnb_repo_recursive(current_url, headers, result_list, visited_urls, processed_files, repo_base_path):
    """递归解析仓库目录"""
    clean_current_url = current_url.split('?')[0]

    if clean_current_url in visited_urls:
        return
    visited_urls.add(clean_current_url)

    try:
        response = requests.get(current_url, headers=headers, timeout=10)
    except Exception:
        return

    if response.status_code != 200:
        return

    html_content = response.text

    all_links = re.findall(r'href="([^"]+)"', html_content)
    unique_links = set()
    for link in all_links:
        full_url = urljoin(current_url, link)
        unique_links.add(full_url)

    for full_url in unique_links:
        if repo_base_path not in full_url:
            continue

        if full_url.endswith((".gguf", ".safetensors")) and "/blob/" in full_url:
            clean_file_url = full_url.split('?')[0]
            if clean_file_url not in processed_files:
                processed_files.add(clean_file_url)
                parse_cnb_file_page(full_url, headers, result_list)

        elif "/tree/" in full_url:
            clean_dir_url = full_url.split('?')[0]
            if clean_dir_url != clean_current_url and clean_dir_url not in visited_urls:
                parse_cnb_repo_recursive(full_url, headers, result_list, visited_urls, processed_files, repo_base_path)


# ============================================================
#  ModelScope / 魔搭社区
# ============================================================

def parse_modelscope_url(repo_input):
    """
    解析魔搭社区链接，提取 namespace、model_name 和 revision。
    支持:
      - 短格式: "Comfy-Org/SCAIL-2"
      - 完整链接: "https://www.modelscope.cn/models/Comfy-Org/SCAIL-2/files"
    返回 (namespace, model_name, revision)
    """
    revision = MODELSCOPE_DEFAULT_REVISION

    if repo_input.startswith("http"):
        parsed = urlparse(repo_input)
        path_parts = parsed.path.strip("/").split("/")
        # URL 格式: /models/{namespace}/{model_name}/files 或 /models/{namespace}/{model_name}
        if len(path_parts) >= 2 and path_parts[0] == "models":
            namespace = path_parts[1]
            model_name = path_parts[2] if len(path_parts) >= 3 else ""
        else:
            raise ValueError(f"无法解析魔搭社区链接: {repo_input}")
    else:
        parts = repo_input.strip("/").split("/")
        if len(parts) >= 2:
            namespace = parts[0]
            model_name = parts[1]
        else:
            raise ValueError(f"无法解析短格式 repo: {repo_input}，应为 namespace/model_name")

    return namespace, model_name, revision


def fetch_modelscope_files(namespace, model_name, revision):
    """调用魔搭社区 API 获取仓库文件列表（递归），返回 blob 文件列表。"""
    api_url = f"https://www.modelscope.cn/api/v1/models/{namespace}/{model_name}/repo/files"
    params = {
        "Revision": revision,
        "Recursive": "true"
    }

    print(f"  正在请求 API: {api_url}?Revision={revision}&Recursive=true")
    try:
        resp = requests.get(api_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [ERROR] API 请求失败: {e}")
        return []

    if not data.get("Success") and data.get("Code") != 200:
        print(f"  [ERROR] API 返回错误: {data.get('Message', 'Unknown error')}")
        return []

    files = data.get("Data", {}).get("Files", [])
    blob_files = [f for f in files if f.get("Type") == "blob"]
    print(f"  获取到 {len(blob_files)} 个文件（共 {len(files)} 个条目）")
    return blob_files


def build_modelscope_download_url(namespace, model_name, revision, file_path):
    """构造魔搭社区文件下载链接。"""
    return f"https://www.modelscope.cn/models/{namespace}/{model_name}/resolve/{revision}/{file_path}"


def process_modelscope_repo(repo_input, result_list):
    """处理魔搭社区仓库"""
    print(f"正在处理魔搭社区仓库: {repo_input} ...")

    try:
        namespace, model_name, revision = parse_modelscope_url(repo_input)
    except ValueError as e:
        print(f"  [ERROR] {e}")
        return

    print(f"  -> 命名空间: {namespace}, 模型: {model_name}, 分支: {revision}")

    files = fetch_modelscope_files(namespace, model_name, revision)
    if not files:
        print(f"  [WARN] 未获取到文件列表")
        return

    for file_info in files:
        file_path = file_info.get("Path", "")
        filename = os.path.basename(file_path)

        if not filename.endswith((".safetensors", ".gguf")):
            continue

        sha256 = file_info.get("Sha256", "N/A")
        size_bytes = file_info.get("Size", 0)
        size_gb = round(size_bytes / (1024 ** 3), 2) if size_bytes else 0

        download_url = build_modelscope_download_url(namespace, model_name, revision, file_path)
        save_path = get_save_path(filename, ref_path=file_path)

        print(f"  -> 找到文件: {filename} | 路径: {save_path} | 大小: {size_gb}GB")

        result_list.append({
            "source": "modelscope",
            "repo": f"{namespace}/{model_name}",
            "filename": filename,
            "url": download_url,
            "path": save_path,
            "sha256": sha256,
            "checksum_type": "sha256" if sha256 != "N/A" else "N/A",
            "size": f"{size_gb}GB"
        })


# ============================================================
#  主程序
# ============================================================

def classify_repo(repo):
    """
    根据 repo 字符串判断来源类型。
    返回: ("hf" | "cnb" | "modelscope", repo_input)
    """
    if repo.startswith("http"):
        if "cnb.cool" in repo:
            return ("cnb", repo)
        elif "modelscope.cn" in repo:
            return ("modelscope", repo)
        elif "huggingface.co" in repo:
            return ("hf", repo)
        else:
            return ("unknown", repo)
    else:
        # 短格式：当作 Hugging Face repo_id 处理（向后兼容）
        return ("hf", repo)


def search_repos(repos, hf_token=None):
    """
    搜索指定的 repos 列表，返回模型列表。
    repos: 链接字符串列表
    hf_token: Hugging Face token（可选）
    返回: list[dict]
    """
    api = HfApi(token=hf_token or HF_TOKEN)
    result = []

    for repo in repos:
        source_type, repo_input = classify_repo(repo)

        if source_type == "hf":
            process_hf_repo(api, repo_input, result)
        elif source_type == "cnb":
            process_cnb_url(repo_input, result)
        elif source_type == "modelscope":
            process_modelscope_repo(repo_input, result)
        else:
            print(f"跳过不支持的链接: {repo}")

    # 按文件名排序
    result.sort(key=lambda x: x["filename"])
    return result


if __name__ == "__main__":
    result = search_repos(repos)

    output_file = "file-list.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    print(f"\n[OK] 处理完成！JSON 文件已保存为 {output_file}")
    print(f"共找到 {len(result)} 个文件（已按文件名排序）。")
