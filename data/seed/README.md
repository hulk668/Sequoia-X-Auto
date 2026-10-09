# 数据库种子 | DB Seed

`sequoia_v2.db.gz` —— 用于给 GitHub Actions 引导初始数据库的压缩快照。

## 为什么需要它

`data/sequoia_v2.db` 有 100MB+，超过 GitHub 单文件 100MB 限制，且被 `.gitignore` 排除，
所以数据库本身不入库。Actions 上的数据靠 `actions/cache` 滚动缓存持久化，
但**缓存是空的、只能由运行环境自己产生**，冷启动时没有任何数据。

工作流已移除自动回填（baostock 回填接口数据仍有问题），因此改用这个种子文件做冷启动：
缓存未命中时自动 `gunzip` 出 `data/sequoia_v2.db`，之后正常运行增量更新并写回缓存。

## 当前种子内容

| 项 | 值 |
|---|---|
| 覆盖股票 | **1332 只**（全市场约 5200 只） |
| 日期范围 | 2024-01-02 ~ 2026-10-08 |
| 总行数 | 870,164 |
| 解压后大小 | 106.3 MB |
| 压缩后大小 | 35.3 MB（gzip -9，压缩率 3.0x） |

> ⚠️ 只覆盖约 1/4 的市场，策略每天只会扫描这 1332 只。
> 想覆盖全市场，需要先在本机把历史数据补齐，再重新生成种子。

## 如何重新生成种子

在本机数据库补齐后，重新压一份覆盖上去：

```bash
python - <<'PY'
import sqlite3, os, gzip, shutil

db = "data/sequoia_v2.db"
# 1) 复制一份再 VACUUM，避免动到正在使用的库
tmp = "data/seed/.vacuum.db"
shutil.copyfile(db, tmp)
sqlite3.connect(tmp).execute("VACUUM")

# 2) gzip -9 压缩
with open(tmp, "rb") as fi, gzip.open("data/seed/sequoia_v2.db.gz", "wb", compresslevel=9) as fo:
    shutil.copyfileobj(fi, fo, length=1024 * 1024)

os.remove(tmp)
print("压缩后:", round(os.path.getsize("data/seed/sequoia_v2.db.gz") / 1024 / 1024, 1), "MB")
PY
```

然后提交 `data/seed/sequoia_v2.db.gz` 即可。下一轮 Actions 若缓存已存在，会继续用缓存，
**不会**用新种子覆盖 —— 需要清理缓存（Actions → Caches → 删掉 `sequoia-db-*`）才会重新引导。

## 注意

- 种子是**冷启动兜底**，正常运行路径是缓存，不会每轮读它。
- `actions/cache` 连续 7 天未被访问会被 GitHub 清理，届时会自动回退到种子重新引导。
- 种子日期越旧，冷启动时需要的增量拉取区间越长；建议定期更新。
