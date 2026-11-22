#!/usr/bin/env python3
"""构造示例/测试用的旧地名名录备份 ZIP。

用法： python3 -I tests/make_sample.py <输出目录>
生成的包：
  backup_good.zip        完整链：全量 v1 + 增量 v1→v2 + 增量 v2→v3
  backup_missing.zip     清单引用了 inc_v2_v3.json 但包内缺失（缺包演示）
  backup_badops.zip      增量删除不存在的编号（非法增删改演示）
  backup_multichain.zip  同一版有两个增量，先错后对（试其他链演示）
  backup_fullv2.zip      全量 v2 + 增量 v2→v3（从中间全量表起步演示）
  backup_brokenv4.zip    清单登记 v3→v4 增量但包内缺失（断链演示）
"""
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 数据
V1_ROWS = [["P0001", "张家村", "张家乡"],
           ["P0002", "李家屯", "李家乡"],
           ["P0010", "王家洼", "王家乡"]]

INC12 = {"add": [["P0011", "赵家堡", "赵家乡"]],
         "modify": [["P0002", "李家屯", "新李家乡"]],
         "delete": ["P0010"]}

INC23 = {"add": [["P0012", "孙家沟", "孙家乡"]],
         "modify": [["P0001", "张家庄", "张家乡"]],
         "delete": []}

# 操作合法但结果与清单摘要不符的“坏”增量（v1→v2）
INC12_WRONG = {"add": [], "modify": [["P0002", "李家屯", "错误乡"]], "delete": []}
# 操作非法的增量：删除没有原记录的编号
INC12_ILLEGAL = {"add": [], "modify": [], "delete": ["P9999"]}
# v3→v4 增量（用于“缺包”演示：清单登记了但包内没有文件）
INC34 = {"add": [["P0013", "周家台", "周家乡"]], "modify": [], "delete": []}


def apply(rows, inc):
    state = {r[0]: list(r) for r in rows}
    for r in inc.get("add", []):
        state[r[0]] = list(r)
    for r in inc.get("modify", []):
        state[r[0]] = list(r)
    for pid in inc.get("delete", []):
        state.pop(pid, None)
    return [state[k] for k in sorted(state)]


V2_ROWS = apply(V1_ROWS, INC12)
V3_ROWS = apply(V2_ROWS, INC23)
V4_ROWS = apply(V3_ROWS, INC34)


def canonical(rows):
    ordered = sorted(([str(r[0]), str(r[1]), str(r[2])] for r in rows),
                     key=lambda r: r[0])
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def digest(rows):
    return hashlib.sha256(canonical(rows).encode("utf-8")).hexdigest()


D1, D2, D3 = digest(V1_ROWS), digest(V2_ROWS), digest(V3_ROWS)
D4 = digest(V4_ROWS)


# ---------------------------------------------------------------- 打包
def _manifest(versions, fulls, incs):
    return json.dumps(
        {"versions": [{"version": v, "sha256": s} for v, s in versions],
         "full_files": [{"version": v, "file": f} for v, f in fulls],
         "increments": [{"from": a, "to": b, "file": f} for a, b, f in incs]},
        ensure_ascii=False, indent=1)


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _rows_json(rows):
    return json.dumps(rows, ensure_ascii=False, indent=1)


def good_zip():
    return _zip({
        "manifest.json": _manifest(
            [("v1", D1), ("v2", D2), ("v3", D3)],
            [("v1", "full_v1.json")],
            [("v1", "v2", "inc_v1_v2.json"), ("v2", "v3", "inc_v2_v3.json")]),
        "full_v1.json": _rows_json(V1_ROWS),
        "inc_v1_v2.json": json.dumps(INC12, ensure_ascii=False),
        "inc_v2_v3.json": json.dumps(INC23, ensure_ascii=False),
    })


def missing_zip():
    """清单引用 inc_v2_v3.json，但包内没有这个文件。"""
    return _zip({
        "manifest.json": _manifest(
            [("v1", D1), ("v2", D2), ("v3", D3)],
            [("v1", "full_v1.json")],
            [("v1", "v2", "inc_v1_v2.json"), ("v2", "v3", "inc_v2_v3.json")]),
        "full_v1.json": _rows_json(V1_ROWS),
        "inc_v1_v2.json": json.dumps(INC12, ensure_ascii=False),
    })


def badops_zip():
    return _zip({
        "manifest.json": _manifest(
            [("v1", D1), ("v2", D2)],
            [("v1", "full_v1.json")],
            [("v1", "v2", "inc_v1_v2.json")]),
        "full_v1.json": _rows_json(V1_ROWS),
        "inc_v1_v2.json": json.dumps(INC12_ILLEGAL, ensure_ascii=False),
    })


def multichain_zip():
    """v1→v2 有两个增量：先生效的结果摘要不符，后一个才对。"""
    return _zip({
        "manifest.json": _manifest(
            [("v1", D1), ("v2", D2), ("v3", D3)],
            [("v1", "full_v1.json")],
            [("v1", "v2", "inc_v1_v2_bad.json"),
             ("v1", "v2", "inc_v1_v2.json"),
             ("v2", "v3", "inc_v2_v3.json")]),
        "full_v1.json": _rows_json(V1_ROWS),
        "inc_v1_v2_bad.json": json.dumps(INC12_WRONG, ensure_ascii=False),
        "inc_v1_v2.json": json.dumps(INC12, ensure_ascii=False),
        "inc_v2_v3.json": json.dumps(INC23, ensure_ascii=False),
    })


def fullv2_zip():
    """从 v2 全量表起步 + v2→v3 增量。"""
    return _zip({
        "manifest.json": _manifest(
            [("v2", D2), ("v3", D3)],
            [("v2", "full_v2.json")],
            [("v2", "v3", "inc_v2_v3.json")]),
        "full_v2.json": _rows_json(V2_ROWS),
        "inc_v2_v3.json": json.dumps(INC23, ensure_ascii=False),
    })


def brokenv4_zip():
    """清单登记了 v3→v4 增量，但包内没有该文件（缺包演示）。"""
    return _zip({
        "manifest.json": _manifest(
            [("v3", D3), ("v4", D4)],
            [],
            [("v3", "v4", "inc_v3_v4.json")]),
    })


# ------------------------------------------------- 同版两份名录、摘要不同
# B 清单里的 v2：与标准 V2_ROWS 内容不同（少 P0011），摘要自然不同
V2B_ROWS = [r for r in V2_ROWS if r[0] != "P0011"]
D2B = digest(V2B_ROWS)
# B 的 v2→v3 增量：只作用在 B 的 v2 上，还原结果与标准 v3 完全一致（同摘要 D3）
INC23_FROM_B = {
    "add": [["P0011", "赵家堡", "赵家乡"],
            ["P0012", "孙家沟", "孙家乡"]],
    "modify": [["P0001", "张家庄", "张家乡"]],
    "delete": [],
}
# A 的 v2→v3 更名包：操作非法（删除不存在的编号），A 的链在 v3 断掉
INC23_ILLEGAL = {"add": [], "modify": [], "delete": ["P9999"]}


def lineage_a_zip():
    """A 备份：全量 v1/v2 + 合法 v1→v2，但 v2→v3 更名包非法。"""
    return _zip({
        "manifest.json": _manifest(
            [("v1", D1), ("v2", D2), ("v3", D3)],
            [("v1", "full_v1.json"), ("v2", "full_v2.json")],
            [("v1", "v2", "inc_v1_v2.json"),
             ("v2", "v3", "inc_v2_v3.json")]),
        "full_v1.json": _rows_json(V1_ROWS),
        "full_v2.json": _rows_json(V2_ROWS),
        "inc_v1_v2.json": json.dumps(INC12, ensure_ascii=False),
        "inc_v2_v3.json": json.dumps(INC23_ILLEGAL, ensure_ascii=False),
    })


def lineage_b_zip():
    """B 备份：另一份 v2 名录（摘要 D2B）+ 能还原到目标 v3 的增量。"""
    assert digest(apply(V2B_ROWS, INC23_FROM_B)) == D3
    return _zip({
        "manifest.json": _manifest(
            [("v2", D2B), ("v3", D3)],
            [("v2", "full_v2.json")],
            [("v2", "v3", "inc_v2_v3.json")]),
        "full_v2.json": _rows_json(V2B_ROWS),
        "inc_v2_v3.json": json.dumps(INC23_FROM_B, ensure_ascii=False),
    })


SAMPLES = {
    "backup_good.zip": good_zip,
    "backup_missing.zip": missing_zip,
    "backup_badops.zip": badops_zip,
    "backup_multichain.zip": multichain_zip,
    "backup_fullv2.zip": fullv2_zip,
    "backup_brokenv4.zip": brokenv4_zip,
}


def main(argv):
    out = Path(argv[1] if len(argv) > 1 else "tests/out")
    out.mkdir(parents=True, exist_ok=True)
    for name, builder in SAMPLES.items():
        path = out / name
        path.write_bytes(builder())
        print(f"已生成 {path} ({path.stat().st_size} 字节)")
    print(f"\n版本摘要：\n  v1 {D1}\n  v2 {D2}\n  v3 {D3}")


if __name__ == "__main__":
    main(sys.argv)
