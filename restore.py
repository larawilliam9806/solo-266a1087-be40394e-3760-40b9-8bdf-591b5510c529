"""旧地名名录备份的链式恢复核心逻辑（纯标准库，便于独立测试）。

名录行 = [地名编号, 名称, 行政村]。
每一步（全量起步或增量接力）结束后，把名录行按编号文本升序排成
紧凑的 UTF-8 JSON 数组（保留非 ASCII 字符），再核对清单中的 SHA-256。

恢复策略：
  * 从某个全量表起步；
  * 每个增量的起始版必须与上一步还原版相同；
  * 遇到缺包、非法增删改或摘要不符就试其他链；
  * 同一次恢复中，已校验的中间版直接复用（self.verified 缓存）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# 名录行： [编号, 名称, 行政村]
Row = List[str]
# 恢复步骤： (类型 full/increment, 备份文件说明, 版号或起止版)
Step = Tuple[str, str, str]


class RestoreFailure(Exception):
    """找不到可用链。reasons 为人类可读的原因列表（已去重）。"""

    def __init__(self, reasons):
        self.reasons = list(dict.fromkeys(str(r) for r in reasons)) or ["未知原因"]
        super().__init__("；".join(self.reasons))


class IllegalOp(Exception):
    """非法增删改：新增用了旧号，或改/删没有原记录。"""


# ---------------------------------------------------------------- 摘要

def canonical_text(rows) -> str:
    """名录行按编号文本升序排成紧凑 JSON 数组文本（保留非 ASCII 字符）。"""
    ordered = sorted(([str(r[0]), str(r[1]), str(r[2])] for r in rows),
                     key=lambda r: r[0])
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def digest_rows(rows) -> str:
    """紧凑 UTF-8 JSON 数组的 SHA-256（十六进制小写）。"""
    return hashlib.sha256(canonical_text(rows).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 解析

_ID_KEYS = ("id", "code", "编号", "地名编号")
_NAME_KEYS = ("name", "名称")
_VILLAGE_KEYS = ("village", "行政村")


def _pick(obj, keys):
    for k in keys:
        if k in obj:
            return obj[k]
    raise KeyError(keys[0])


def parse_row(obj, where) -> Row:
    """名录行：接受 [编号, 名称, 行政村] 或含 id/name/village 键的对象。"""
    if isinstance(obj, (list, tuple)) and len(obj) == 3:
        return [str(obj[0]), str(obj[1]), str(obj[2])]
    if isinstance(obj, dict):
        try:
            return [str(_pick(obj, _ID_KEYS)),
                    str(_pick(obj, _NAME_KEYS)),
                    str(_pick(obj, _VILLAGE_KEYS))]
        except KeyError:
            pass
    raise ValueError(f"{where}：无法识别的名录行 {obj!r}（应为 [编号, 名称, 行政村]）")


def parse_full(text: str, where: str) -> List[Row]:
    """解析全量表：名录行的 JSON 数组，编号不得重复。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}：JSON 解析失败（{exc}）") from exc
    if not isinstance(data, list):
        raise ValueError(f"{where}：全量文件应是名录行的 JSON 数组")
    rows = [parse_row(o, where) for o in data]
    seen = set()
    for r in rows:
        if r[0] in seen:
            raise ValueError(f"{where}：地名编号 {r[0]} 重复出现")
        seen.add(r[0])
    return rows


def _parse_delete_id(item, where) -> str:
    if isinstance(item, (list, tuple)) and len(item) == 3:
        return str(item[0])
    if isinstance(item, dict):
        try:
            return str(_pick(item, _ID_KEYS))
        except KeyError:
            pass
        raise ValueError(f"{where}：删除操作缺少编号 {item!r}")
    return str(item)


def parse_increment(text: str, where: str) -> List[Tuple[str, object]]:
    """解析增量包，返回操作列表 [(op, payload), ...]。

    支持两种写法：
      1) {"add": [行...], "modify": [行...], "delete": [编号...]}
         （也接受中文键 新增/修改/删除；按 增→改→删 顺序应用）
      2) [{"op": "add", "row": 行}, {"op": "delete", "id": 编号}, ...]
         （按列表顺序应用）
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}：JSON 解析失败（{exc}）") from exc
    ops: List[Tuple[str, object]] = []
    if isinstance(data, dict):
        groups = (("add", "add"), ("modify", "modify"), ("delete", "delete"),
                  ("新增", "add"), ("修改", "modify"), ("删除", "delete"))
        for key, op in groups:
            items = data.get(key)
            if not items:
                continue
            if not isinstance(items, list):
                raise ValueError(f"{where}：{key} 应是数组")
            for item in items:
                if op == "delete":
                    ops.append(("delete", _parse_delete_id(item, where)))
                else:
                    ops.append((op, parse_row(item, where)))
    elif isinstance(data, list):
        aliases = {"add": "add", "new": "add", "insert": "add", "新增": "add",
                   "modify": "modify", "update": "modify", "change": "modify", "修改": "modify",
                   "delete": "delete", "remove": "delete", "del": "delete", "删除": "delete"}
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(f"{where}：增量操作应是对象，得到 {item!r}")
            opname = str(item.get("op") or item.get("type") or "")
            op = aliases.get(opname.lower())
            if op is None:
                raise ValueError(f"{where}：未知操作类型 {opname!r}")
            if op == "delete":
                payload = item.get("id", item.get("row", item.get("编号", item.get("地名编号"))))
                if payload is None:
                    raise ValueError(f"{where}：删除操作缺少编号 {item!r}")
                ops.append(("delete", _parse_delete_id(payload, where)))
            else:
                if "row" not in item:
                    raise ValueError(f"{where}：{opname} 操作缺少 row 字段 {item!r}")
                ops.append((op, parse_row(item["row"], where)))
    else:
        raise ValueError(f"{where}：增量文件应是操作对象或操作数组")
    return ops


def apply_ops(state: Dict[str, Row], ops, where: str) -> Dict[str, Row]:
    """按编号应用增删改；新增须用新号，改删须有原记录。"""
    new_state = dict(state)
    for op, payload in ops:
        if op == "add":
            if payload[0] in new_state:
                raise IllegalOp(f"{where}：新增编号 {payload[0]} 已存在，新增须用新号")
            new_state[payload[0]] = payload
        elif op == "modify":
            if payload[0] not in new_state:
                raise IllegalOp(f"{where}：修改编号 {payload[0]} 没有原记录")
            new_state[payload[0]] = payload
        elif op == "delete":
            if payload not in new_state:
                raise IllegalOp(f"{where}：删除编号 {payload} 没有原记录")
            del new_state[payload]
    return new_state


# ---------------------------------------------------------------- 链式恢复

@dataclass
class FullFile:
    version: str
    label: str                       # 人类可读来源，如 “backup.zip :: full_v1.json”
    present: bool = True             # False 表示清单引用了但包内缺失
    loader: Optional[Callable[[], str]] = None


@dataclass
class Increment:
    from_version: str
    to_version: str
    label: str
    present: bool = True
    loader: Optional[Callable[[], str]] = None


class Restorer:
    """在一次恢复会话内找链、校验、复用中间版。"""

    def __init__(self, digests: Dict[str, set], fulls: List[FullFile],
                 increments: List[Increment]):
        self.digests = digests          # 版本 -> 清单登记的 SHA-256 集合
        self.fulls = fulls
        self.increments = increments
        self.verified: Dict[str, Tuple[Dict[str, Row], List[Step]]] = {}
        self.reasons: List[str] = []

    # -- 对外入口 -------------------------------------------------
    def restore(self, target: str) -> Tuple[Dict[str, Row], List[Step]]:
        target = str(target)
        result = self._reach(target, [])
        if result is None:
            if not self.reasons:
                self.reasons.append(
                    f"版本 {target} 既没有全量文件，也没有指向它的增量包，无法成链")
            raise RestoreFailure(self.reasons)
        return result

    # -- 内部 -----------------------------------------------------
    def _check(self, version: str, state: Dict[str, Row], steps: List[Step]):
        """核对某一步还原结果的摘要；通过则缓存为已校验中间版。"""
        expected = self.digests.get(version)
        if not expected:
            self.reasons.append(f"清单中没有版本 {version} 的名录摘要，无法核对")
            return None
        digest = digest_rows(list(state.values()))
        if digest in expected:
            self.verified[version] = (state, steps)
            return self.verified[version]
        exp = "、".join(sorted(e[:16] + "…" for e in expected))
        self.reasons.append(
            f"版本 {version} 摘要不符：清单登记 {exp}，实得 {digest[:16]}…")
        return None

    def _reach(self, version: str, stack: List[str]):
        # 同次恢复复用已校验的中间版
        if version in self.verified:
            return self.verified[version]
        if version in stack:
            self.reasons.append(
                "版本链出现循环：" + " → ".join(stack + [version]))
            return None
        stack = stack + [version]

        # 1) 全量表直接起步
        for full in self.fulls:
            if full.version != version:
                continue
            if not full.present or full.loader is None:
                self.reasons.append(f"缺少全量文件 {full.label}（版 {version}）")
                continue
            try:
                rows = parse_full(full.loader(), full.label)
            except ValueError as exc:
                self.reasons.append(str(exc))
                continue
            state = {r[0]: r for r in rows}
            got = self._check(version, state,
                              [("full", full.label, version)])
            if got:
                return got

        # 2) 增量接力：起始版须与上一步还原版相同
        incoming = [i for i in self.increments if i.to_version == version]
        for inc in incoming:
            base = self._reach(inc.from_version, stack)
            if base is None:
                continue  # 原因已在递归中记录，试其他链
            base_state, base_steps = base
            if not inc.present or inc.loader is None:
                self.reasons.append(
                    f"缺少增量文件 {inc.label}（{inc.from_version}→{inc.to_version}）")
                continue
            try:
                ops = parse_increment(inc.loader(), inc.label)
                state = apply_ops(base_state, ops, inc.label)
            except (ValueError, IllegalOp) as exc:
                self.reasons.append(str(exc))
                continue
            got = self._check(
                version, state,
                base_steps + [("increment", inc.label,
                               f"{inc.from_version}→{inc.to_version}")])
            if got:
                return got

        if not any(f.version == version for f in self.fulls) and not incoming:
            self.reasons.append(
                f"版本 {version} 没有全量文件，也没有指向它的增量包")
        return None
