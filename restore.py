"""旧地名名录备份的链式恢复核心逻辑（纯标准库，便于独立测试）。

名录行 = [地名编号, 名称, 行政村]。
每一步（全量起步或增量接力）结束后，把名录行按编号文本升序排成
紧凑的 UTF-8 JSON 数组（保留非 ASCII 字符），再核对清单中的 SHA-256。

恢复策略：
  * 每条链只认同一份清单登记的摘要：整表/增量按清单版号接力，
    不会把甲清单的还原结果拿去对乙清单的摘要；
  * 从某个全量表起步；每个增量的起始版必须与上一步还原版相同；
  * 一份中间名录走不通（缺包、非法增删改、摘要不符）时，继续尝试
    同版其他能从整表或增量还原、且通过本清单校验的名录；
  * 同版同摘要的中间名录在尝试多个后续增量时复用已还原结果
    （verified 以 (版号, 摘要) 为键）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

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
    label: str                       # 人类可读来源，如 “#3 backup.zip :: full_v1.json”
    present: bool = True             # False 表示清单引用了但包内缺失
    loader: Optional[Callable[[], str]] = None
    # 所属清单的 版号 -> 摘要集合 映射；链上每一步只与本清单的摘要核对。
    # None 表示未提供清单归属（旧用法），退回全局摘要集合。
    digest_scope: Optional[Dict[str, Set[str]]] = None


@dataclass
class Increment:
    from_version: str
    to_version: str
    label: str
    present: bool = True
    loader: Optional[Callable[[], str]] = None
    digest_scope: Optional[Dict[str, Set[str]]] = None


# 名录摘要 -> 还原结果 的缓存条目
VerifiedEntry = Tuple[Dict[str, Row], List[Step]]


class Restorer:
    """在一次恢复会话内找链、校验、复用同版同摘要的中间名录。"""

    def __init__(self, digests: Dict[str, set], fulls: List[FullFile],
                 increments: List[Increment]):
        self.digests = digests          # 版本 -> 全局清单摘要集合（兜底用）
        self.fulls = fulls
        self.increments = increments
        # 已校验中间版：键 (版号, 摘要)，值 (名录状态, 恢复步骤)
        self.verified: Dict[Tuple[str, str], VerifiedEntry] = {}
        self.reasons: List[str] = []

    # -- 对外入口 -------------------------------------------------
    def restore(self, target: str) -> Tuple[Dict[str, Row], List[Step]]:
        target = str(target)
        result = self._reach(target, None, [])
        if result is None:
            if not self.reasons:
                self.reasons.append(
                    f"版本 {target} 既没有全量文件，也没有指向它的增量包，无法成链")
            raise RestoreFailure(self.reasons)
        return result

    # -- 内部 -----------------------------------------------------
    def _expected_digests(self, version: str,
                          scope: Optional[Dict[str, Set[str]]]) -> Optional[Set[str]]:
        """该步核对所用的摘要集合：优先用所属清单，否则退回全局集合。"""
        if scope is not None:
            return scope.get(version)
        return self.digests.get(version)

    def _check(self, version: str, state: Dict[str, Row], steps: List[Step],
               scope: Optional[Dict[str, Set[str]]]):
        """核对某一步还原结果的摘要；通过则按 (版号, 摘要) 缓存。"""
        expected = self._expected_digests(version, scope)
        if not expected:
            self.reasons.append(f"清单中没有版本 {version} 的名录摘要，无法核对")
            return None
        digest = digest_rows(list(state.values()))
        if digest in expected:
            self.verified[(version, digest)] = (state, steps)
            return self.verified[(version, digest)]
        exp = "、".join(sorted(e[:16] + "…" for e in expected))
        self.reasons.append(
            f"版本 {version} 摘要不符：清单登记 {exp}，实得 {digest[:16]}…")
        return None

    def _cached(self, version: str,
                scope: Optional[Dict[str, Set[str]]]) -> Optional[VerifiedEntry]:
        """复用同版同摘要、已通过校验的中间名录（跨同摘要清单也安全）。"""
        expected = self._expected_digests(version, scope)
        if not expected:
            return None
        for digest in expected:
            hit = self.verified.get((version, digest))
            if hit is not None:
                return hit
        return None

    def _reach(self, version: str, scope: Optional[Dict[str, Set[str]]],
               stack: List[str]):
        """还原 version。scope 为所属清单的摘要映射；同清单的文件才能接力。

        同版同摘要且已校验的中间名录直接复用；同版不同摘要（不同清单的
        两份名录）分别还原，互不顶替。
        """
        if version in stack:
            self.reasons.append(
                "版本链出现循环：" + " → ".join(stack + [version]))
            return None
        cached = self._cached(version, scope)
        if cached is not None:
            return cached
        stack = stack + [version]

        same_scope = lambda s: scope is None or s is None or s is scope
        has_candidate = False

        # 1) 全量表直接起步：只取按清单版号对得上、且与当前链同清单的整表
        for full in self.fulls:
            if full.version != version or not same_scope(full.digest_scope):
                continue
            has_candidate = True
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
                              [("full", full.label, version)],
                              full.digest_scope)
            if got:
                return got

        # 2) 增量接力：起始版须与上一步还原版相同，且增量与当前链同清单
        incoming = [i for i in self.increments
                    if i.to_version == version and same_scope(i.digest_scope)]
        for inc in incoming:
            has_candidate = True
            # 缺包先记下原因，再看这条链的其余环节能否走通
            if not inc.present or inc.loader is None:
                self.reasons.append(
                    f"缺少增量文件 {inc.label}（{inc.from_version}→{inc.to_version}）")
                continue
            base = self._reach(inc.from_version, inc.digest_scope, stack)
            if base is None:
                continue  # 原因已在递归中记录，试其他链 / 其他清单
            base_state, base_steps = base
            try:
                ops = parse_increment(inc.loader(), inc.label)
                state = apply_ops(base_state, ops, inc.label)
            except (ValueError, IllegalOp) as exc:
                self.reasons.append(str(exc))
                continue
            got = self._check(
                version, state,
                base_steps + [("increment", inc.label,
                               f"{inc.from_version}→{inc.to_version}")],
                inc.digest_scope)
            if got:
                return got

        if not has_candidate:
            self.reasons.append(
                f"版本 {version} 没有全量文件，也没有指向它的同清单增量包，无法成链")
        return None
