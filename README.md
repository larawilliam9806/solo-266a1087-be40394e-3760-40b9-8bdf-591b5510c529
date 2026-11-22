# 旧地名名录 · 补档页

用 Flask 做的名录补档页：上传备份 ZIP（整表 / 逐版更正包 + 清单），在网页上选目标正式版，
从备份链恢复名录。当前名录存 SQLite，可按地名编号查询；每次恢复（无论成败）都留痕。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据（SQLite 库 + 已上传的备份文件）持久化在命名卷 `gazetteer-data` 里。

### 本地开发

```bash
pip install -r requirements.txt
python app.py            # 或 flask --app app run --debug
```

### 生成示例备份包

```bash
python3 tests/make_sample.py sample_data
```

生成 6 个演示包：完整链、缺文件包、非法增量包、多链包、从中间全量起步的包、断链包。

### 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 备份 ZIP 格式

包内需有清单 `manifest.json`（可在子目录，文件引用相对清单所在目录）：

```json
{
  "versions": [
    {"version": "v1", "sha256": "364a3af4…"},   // 各正式版的名录摘要
    {"version": "v2", "sha256": "8f9c67c1…"}
  ],
  "full_files": [
    {"version": "v1", "file": "full_v1.json"}     // 全量文件的版号
  ],
  "increments": [
    {"from": "v1", "to": "v2", "file": "inc_v1_v2.json"}   // 增量文件的起止版
  ]
}
```

**全量文件**：名录行的 JSON 数组，每行依次是地名编号、名称、行政村：

```json
[["P0001", "张家村", "张家乡"], ["P0002", "李家屯", "李家乡"]]
```

**增量文件**：按编号增删改。新增必须用新号；改、删必须有原记录。

```json
{
  "add":    [["P0011", "赵家堡", "赵家乡"]],
  "modify": [["P0002", "李家屯", "新李家乡"]],
  "delete": ["P0010"]
}
```

也接受操作列表形式 `[{"op": "add", "row": [...]}, {"op": "delete", "id": "P0010"}]`。

## 名录摘要算法（清单中 SHA-256 的计算口径）

每一步还原后，把名录行：

1. 按**地名编号文本升序**排列（`"P10" < "P2"`）；
2. 排成**紧凑 JSON 数组**（`separators=(",", ":")`，无多余空白）；
3. **保留非 ASCII 字符**（`ensure_ascii=False`），按 **UTF-8** 编码；
4. 取 **SHA-256** 十六进制小写，与清单核对。

Python 一句话：

```python
hashlib.sha256(json.dumps(sorted(rows, key=lambda r: r[0]),
               ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
```

## 恢复规则

- 从某个**全量表**起步；每个增量的**起始版须与上一步还原版相同**，逐步接力到目标版；
- 每步结束都按上面的口径核对清单中的 SHA-256；
- 遇到**缺包、非法增删改、摘要不符**，自动改试其他链（可跨多个已上传的备份包）；
- 同一次恢复中，**已校验的中间版直接复用**，不重复加载、不重复核对；
- 恢复**成功**：同一事务内更新名录（按编号可查）和本次使用的备份文件记录；
- **找不到可用链**：页面说明具体原因（缺哪个包、哪条操作非法、哪个版摘要不符），
  原名录不动，历史恢复记录照常可查。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | `dev-secret-key` | Flask 会话密钥，生产环境务必修改 |
| `GAZETTEER_DATABASE` | `<数据目录>/app.db` | SQLite 路径 |
| `GAZETTEER_UPLOAD_DIR` | `<数据目录>/uploads` | 备份文件存放目录 |

## 目录结构

```
app.py            Flask 补档页（上传、恢复、查询）
restore.py        链式恢复核心：摘要核对、增删改、找链、中间版复用
package.py        备份 ZIP 与清单解析（路径消毒、大小上限）
db.py             SQLite 模式与连接
templates/        页面模板
tests/            样例包生成器 + 单元/端到端测试
Dockerfile / docker-compose.yml
```
