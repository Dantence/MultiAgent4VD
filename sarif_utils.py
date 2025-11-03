# sarif_utils.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Dict, Any, List, Iterable, Optional, Tuple
import json
import os
from urllib.parse import urlparse, unquote

def load_sarif(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def iter_results(sarif: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    runs = sarif.get("runs", []) or []
    for run in runs:
        results = run.get("results", []) or []
        for r in results:
            yield r

def _file_from_file_uri(uri: str) -> Optional[str]:
    # 兼容 file:///D:/... 与 Unix 风格
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path or "")
        if os.name == "nt":
            # Windows: 可能以 /D:/ 开头
            if path.startswith("/") and len(path) > 3 and path[2] == ":":
                path = path[1:]
        return path
    except Exception:
        return None

def _resolve_with_base(rel_uri: str, uri_base_id: Optional[str], cfg: Dict[str, Any]) -> str:
    """
    用 uriBaseId_map 或 project_root 将相对路径落地到绝对路径。
    """
    app = cfg.get("app", {})
    base_map = app.get("uriBaseId_map", {}) or {}
    project_root = app.get("project_root", "") or ""

    base_dir = project_root
    if uri_base_id and uri_base_id in base_map:
        base_dir = base_map[uri_base_id]

    # 兼容 posix 风格分隔符
    rel_path = rel_uri.replace("\\", "/")
    return os.path.normpath(os.path.join(base_dir, rel_path))

def resolve_artifact_location(artifact: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    """
    支持三种情况：
      1) file:/// 绝对路径
      2) 带 uriBaseId 的相对路径（如 %SRCROOT%）
      3) 单纯相对路径（拼接 project_root）
    """
    uri = artifact.get("uri")
    if not uri:
        return None

    # 1) 绝对 file:///
    if uri.startswith("file://"):
        if cfg.get("app", {}).get("prefer_absolute_uri", True):
            p = _file_from_file_uri(uri)
            if p and os.path.isfile(p):
                return p
        # 若 prefer_absolute_uri=false，继续尝试下面逻辑

    # 2) uriBaseId 相对
    base_id = artifact.get("uriBaseId")
    abs_path = _resolve_with_base(uri, base_id, cfg)
    if os.path.isfile(abs_path):
        return abs_path

    # 3) project_root + 相对
    app = cfg.get("app", {})
    project_root = app.get("project_root", "") or ""
    fallback = os.path.normpath(os.path.join(project_root, uri))
    if os.path.isfile(fallback):
        return fallback

    # 4) 最后尝试把 file:/// 解析但忽略存在性
    if uri.startswith("file://"):
        p = _file_from_file_uri(uri)
        return p

    return abs_path  # 返回推测路径，可能不存在，调用方自行判断

def extract_result_files(res: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """
    遍历 locations[].physicalLocation.artifactLocation
    """
    locs = res.get("locations", []) or []
    paths: List[str] = []
    for loc in locs:
        phy = (loc or {}).get("physicalLocation", {})
        art = phy.get("artifactLocation", {})
        if not art:
            continue
        p = resolve_artifact_location(art, cfg)
        if p:
            paths.append(p)
    # 去重，保持顺序
    seen = set(); dedup = []
    for p in paths:
        if p not in seen:
            seen.add(p); dedup.append(p)
    return dedup

def safe_read_text(path: str, encoding: str="utf-8", max_chars: int=120000) -> str:
    try:
        with open(path, "r", encoding=encoding, errors="ignore") as f:
            s = f.read()
        if len(s) > max_chars:
            s = s[:max_chars]
        return s
    except Exception:
        return ""
