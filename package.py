"""备份 ZIP 与清单（manifest.json）的解析。

ZIP 约定结构（清单可位于包内任意目录，文件引用相对清单所在目录）：

    manifest.json          清单
    full_v1.json           全量表： [[编号, 名称, 行政村], ...]
    inc_v1_v2.json         增量包： {"add": [...], "modify": [...], "delete": [...]}

清单格式：

    {
      "versions":    [{"version": "v1", "sha256": "..."}, ...],   // 各正式版名录摘要
      "full_files":  [{"version": "v1", "file": "full_v1.json"}], // 全量文件的版号
      "increments":  [{"from": "v1", "to": "v2", "file": "inc_v1_v2.json"}]  // 增量起止版
    }
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import List, Tuple

# 单个引用文件的大小上限，防止解压炸弹
MAX_MEMBER_BYTES = 64 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PackageError(Exception):
    """备份包或清单不合法。"""


@dataclass
class Manifest:
    versions: List[Tuple[str, str]] = field(default_factory=list)   # (版号, sha256)
    fulls: List[Tuple[str, str]] = field(default_factory=list)      # (版号, 包内路径)
    increments: List[Tuple[str, str, str]] = field(default_factory=list)  # (起始版, 目标版, 包内路径)


def find_manifest_name(zf: zipfile.ZipFile) -> str:
    """在 ZIP 内定位清单文件：根目录的 manifest.json 优先。"""
    candidates = [n for n in zf.namelist()
                  if not n.endswith("/")
                  and PurePosixPath(n).name.lower() == "manifest.json"]
    if not candidates:
        raise PackageError("压缩包中未找到清单文件 manifest.json")
    candidates.sort(key=lambda n: (n.count("/"), len(n)))
    return candidates[0]


def _norm_sha(value, where: str) -> str:
    sha = str(value).strip().lower()
    if not _SHA256_RE.match(sha):
        raise PackageError(f"清单中{where}的摘要不是 64 位十六进制 SHA-256：{value!r}")
    return sha


def _entry_file(item, where: str) -> str:
    for key in ("file", "path", "name"):
        if isinstance(item, dict) and item.get(key):
            return str(item[key])
    raise PackageError(f"清单{where}缺少 file 字段：{item!r}")


def parse_manifest(text: str) -> Manifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackageError(f"清单 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise PackageError("清单应是 JSON 对象")

    m = Manifest()

    # 各正式版的名录摘要：接受数组或 {版号: 摘要} 两种写法
    versions = data.get("versions") or data.get("正式版") or []
    if isinstance(versions, dict):
        versions = [{"version": k, **(v if isinstance(v, dict) else {"sha256": v})}
                    for k, v in versions.items()]
    if not isinstance(versions, list):
        raise PackageError("清单 versions 字段应是数组或对象")
    for item in versions:
        if not isinstance(item, dict) or "version" not in item or "sha256" not in item:
            raise PackageError(f"清单 versions 条目应含 version 与 sha256：{item!r}")
        ver = str(item["version"])
        m.versions.append((ver, _norm_sha(item["sha256"], f"版本 {ver} ")))

    # 全量文件的版号
    fulls = data.get("full_files") or data.get("fulls") or []
    if not isinstance(fulls, list):
        raise PackageError("清单 full_files 字段应是数组")
    for item in fulls:
        if not isinstance(item, dict) or "version" not in item:
            raise PackageError(f"清单 full_files 条目应含 version：{item!r}")
        ver = str(item["version"])
        m.fulls.append((ver, _entry_file(item, "full_files")))
        if item.get("sha256"):
            m.versions.append((ver, _norm_sha(item["sha256"], f"版本 {ver} ")))

    # 增量文件的起止版
    incs = data.get("increments") or data.get("deltas") or []
    if not isinstance(incs, list):
        raise PackageError("清单 increments 字段应是数组")
    for item in incs:
        if not isinstance(item, dict):
            raise PackageError(f"清单 increments 条目应是对象：{item!r}")
        frm = item.get("from", item.get("from_version", item.get("start")))
        to = item.get("to", item.get("to_version", item.get("end")))
        if frm is None or to is None:
            raise PackageError(f"清单 increments 条目应含起止版 from/to：{item!r}")
        frm, to = str(frm), str(to)
        m.increments.append((frm, to, _entry_file(item, "increments")))
        if item.get("sha256"):
            m.versions.append((to, _norm_sha(item["sha256"], f"版本 {to} ")))

    if not m.versions and not m.fulls and not m.increments:
        raise PackageError("清单为空：没有 versions / full_files / increments 条目")
    return m


def safe_member_path(name: str) -> str:
    """规范化包内路径，拒绝绝对路径与 .. 穿越。"""
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or str(p) in ("", "."):
        raise PackageError(f"包内路径非法：{name!r}")
    return str(p)


def read_member(zf: zipfile.ZipFile, arcname: str) -> bytes:
    """读取包内文件（带大小上限）。不存在时抛 KeyError。"""
    info = zf.getinfo(arcname)
    if info.file_size > MAX_MEMBER_BYTES:
        raise PackageError(f"包内文件 {arcname} 超过大小上限")
    return zf.read(arcname)
