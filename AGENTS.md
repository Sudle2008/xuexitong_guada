# AGENTS.md

> 本文件为 AI 代理（agent）提供在本仓库内工作的指引，遵循 AGENTS.md 约定。
> 使用者**不阅读任何文档**：需要用户提供的信息，由你按 §2 的模板**主动提问**，用户只需回答；其余一切由你完成。
> 分工：引擎 `scripts/required_walker.py` 负责机械流程（播放视频、完成文档点、取题、勾选填写、提交）；需要判断的工作（答案、范围、异常处置）由你推理或转问用户。

## Project overview

- 仓库内容：`README.md`（机制说明）、`AGENTS.md`（本文件）、脱敏主引擎 `scripts/required_walker.py`。
- 目标：替用户按顺序完成一门超星/学习通课程中**用户确认的小节清单**——真实播放视频 → 完成文档任务点 → 测验出题后由你作答 → 提交 → 解锁下一节，直到全部完成或无法继续。
- 引擎**不会产生答案**；不含任何账号/课程编号/域名/路径，全部配置来自 `--workdir` 指向的工作目录（不入库）。
- 全程无头运行，不弹任何窗口。

## Setup

```powershell
python -m pip install playwright requests fonttools beautifulsoup4
python scripts\required_walker.py --help    # 自检：能打印参数说明即可
```

- 需要本机 Chrome，默认 `C:\Program Files\Google\Chrome\Application\chrome.exe`；装在别处用环境变量 `XXW_CHROME` 指向 chrome.exe。
- 缺什么装什么；下载慢可用镜像源。

## Asking the user（向用户提问）

**提问规则**：

- 一次只问一两件，问完一件做一件；不要把全部要求一次甩给用户。
- 用大白话 + 具体点击路径；用户答非所问就换个说法再问一次。
- **不要**让用户去读 README 或任何文档。
- 用户总共只需提供三样信息：Cookie、学习页网址、范围与节奏；章节树你能自己抓（见 Generating configuration），抓不到才追问。

### 1. Cookie（最先问）

提问模板（原话照用）：

> 请先在电脑 Chrome 里登录学习通，然后照这几步把登录凭据给我：
> ① 按 F12 → ② 切到「网络 / Network」标签 → ③ 刷新页面 → ④ 点列表里第一个请求 → ⑤ 在「标头 / Headers」里找到「Cookie:」这一行 → ⑥ 把冒号后面的整段值全选复制 → ⑦ 打开记事本粘贴，保存为 `D:\xxw\cookies.txt`（保存时编码选 UTF-8）→ ⑧ 把这个文件路径告诉我。

- 首选**让用户自己存成文件**（Cookie 很长，经对话转写容易截断）；备选：直接粘贴到对话，你校验后写盘。
- 校验：内容必须含 `UID` 或 `_uid`；缺了多半复制不完整，请用户重取。
- 保密：Cookie 是实时登录凭证。只写进本机工作目录；不展示原文、不外传、不提交仓库、不贴日志。收到后只回「已收到」。

### 2. 学习页网址

提问模板：

> 请打开要学的课程，进入能看到章节目录（章节列表）的学习页面，把浏览器地址栏的完整网址复制发给我。

收到后从 URL 解析（这些就是写配置的来源）：

| URL 中的内容 | 用途 |
| --- | --- |
| 主机名（如 `mooc1.example.edu.cn`） | `domain` |
| `courseid=` | `courseId` |
| `clazzid=` | `clazzId` |
| `cpi=`（没有就填 0） | `cpi` |
| 网址主机去掉最左一段 | `cookie_domains` 之一，如 `mooc1.example.edu.cn` → `.example.edu.cn` |

### 3. 范围与节奏（最后问）

提问模板：

> 我从章节树解析到 N 个小节（首节「1.1.1 xxx」、末节「x.x.x xxx」）。按顺序全部做完吗？有没有要跳过的节？
> 可以夜间连续跑吗？最长跑到什么时候 / 停在哪一节就停？

- 小节清单里**包含页面上的全部分支**（有的课程会分成多个部分，不同课程的分法不一样），做哪些由用户在这里确认；确认后你把不做的小节从清单里删掉再交给引擎。
- 和用户定好**明确停止条件**（全部完成 / 停在某节 / 时长上限），之后连续运行、少打扰，只在出题和卡住时联系用户。
- 防风控常识（转告用户）：单日 ≤8 小时、深夜勿高频、期间别在另一设备同时学同一门课。

## Generating configuration（生成配置）

工作目录选**无空格、无中文**的新目录，如 `D:\xxw\<课程名>`。三件套：

### cookies.txt

把提问 1 拿到的内容原样写入 `<WORKDIR>\cookies.txt`（UTF-8）。

### course_config.json

```json
{
  "courseId": 0,
  "clazzId": 0,
  "cpi": 0,
  "domain": "mooc1.example.edu.cn",
  "cookie_domains": [".chaoxing.com", ".example.edu.cn"]
}
```

- `courseId` / `clazzId` / `domain` **必填**，缺任一引擎启动即退出。
- 平台就是 chaoxing.com 域时，`cookie_domains` 只留 `[".chaoxing.com"]`。

### required_leaves.json（小节清单）

格式：按**执行顺序**排列的叶子小节数组（四级子分节如 1.3.2.1 也各占一项，漏了会卡在解锁）：

```json
[ { "id": "1207345001", "name": "1.1.1 示例小节" },
  { "id": "1207345002", "name": "1.1.2 示例小节" } ]
```

**第一步**：用提问 2 的网址 + Cookie 无头抓取章节树。存成 `.temp\dump_chapters.py` 跑一次：

```python
# 用法: python dump_chapters.py <学习页URL> <cookies.txt> <输出.html>
import re, sys, os
from pathlib import Path
from playwright.sync_api import sync_playwright

url, cpath, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
d = {}
for part in re.split(r'[;\n]', cpath.read_text(encoding='utf-8')):
    p = part.strip()
    if p and '=' in p:
        n, _, v = p.partition('='); d[n.strip()] = v.strip()
host = re.sub(r'^https?://([^/]+).*', r'\1', url)
root = ('.' + '.'.join(host.split('.')[-3:])) if host.endswith('.cn') \
       else ('.' + '.'.join(host.split('.')[-2:]))
chrome = os.environ.get('XXW_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=chrome, args=['--no-first-run'])
    ctx = browser.new_context()
    ctx.add_cookies([{'name': n, 'value': v, 'domain': dm, 'path': '/'}
                     for n, v in d.items() for dm in {'.chaoxing.com', root}])
    page = ctx.new_page()
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(6000)          # 等章节 iframe 加载
    html = None
    for f in page.frames:                # 顶层或任一 iframe 里找章节树
        try:
            if 'chapter_item' in f.content():
                html = f.content(); break
        except Exception:
            pass
    browser.close()
if not html:
    sys.exit('未找到章节树(Cookie 失效或 URL 不对)')
out.write_text(html, encoding='utf-8')
print('OK %d bytes -> %s' % (len(html), out))
```

**第二步**：解析成清单。存成 `.temp\parse_leaves.py` 跑（已实测：真实章节树上解析无遗漏、顺序与页面一致）：

```python
# 用法: python parse_leaves.py <章节树.html> <输出required_leaves.json>
import re, sys, json
from pathlib import Path

html = Path(sys.argv[1]).read_text(encoding='utf-8')
# id 与 class 之间可能跨行换行(四级节点)，属性间用 [^>]* 兼容
items = list(re.finditer(r'<div\s+class="chapter_item"[^>]*?id="cur(\d+)"[^>]*>', html))
nodes = []
for k, im in enumerate(items):
    # 名称取「本节点起点→下一节点起点」之间的片段：深缩进节点名称离得很远，固定小窗口会漏
    seg_end = items[k+1].start() if k+1 < len(items) else im.start() + 3000
    seg = html[im.end():seg_end]
    nm = re.search(r'catalog_sbar">\s*([^<]*)</span>\s*([^<]*)</a>', seg)
    num = nm.group(1).strip() if nm else ''
    rest = nm.group(2).strip() if nm else ''
    name = (num + ' ' + rest).strip()
    if not name:
        ta = re.search(r'title="([^"]*)"', im.group(0))
        name = ta.group(1).strip() if ta else ''
    nodes.append((im.start(), im.group(1), name))
pos_map = {c: p for p, c, _ in nodes}
# 叶子判定：名称带两级以上编号前缀(如 1.1.1)；章/节这类无编号前缀的中间节点自动排除
leaves = [{'id': c, 'name': n} for p, c, n in nodes if re.match(r'^\d+\.\d+\.\d+', n)]
leaves.sort(key=lambda x: pos_map[x['id']])
Path(sys.argv[2]).write_text(json.dumps(leaves, ensure_ascii=False, indent=2), encoding='utf-8')
print('叶子 %d 个: %s ... %s' % (len(leaves),
      leaves[0]['name'] if leaves else '-', leaves[-1]['name'] if leaves else '-'))
```

注意事项：

- 抓不到章节树（`未找到章节树`）时才追问用户：
  > 请在那个章节页面：右键 →「查看网页源代码」，把内容全选复制发我（太长就存成 .txt/.html 文件发我）。
- 清单会包含**页面上所有**带编号前缀的叶子小节（各课程的分支划分不一样，脚本不预设）；做哪些由用户在提问 3 确认，确认后把不做的从 JSON 里删掉（保持顺序）再写盘。
- 叶子判定按名称的编号前缀（如 1.1.1、1.3.2.1）；个别课程命名不带这种前缀时，把解析出的节点清单给用户确认后再定。
- 抓取这一步同时验证了 Cookie 有效性：能解析出章节树 = Cookie 可用。
- 解析结果连同数量、首末节名一起向用户确认后再进入下一步。

## Running（启动）

**先试跑一节**（验证全链路，新课程直接这样跑）：

```powershell
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
python scripts\required_walker.py --workdir "D:\xxw\<课程名>" --max-leaves 1
```

- 坑：`--max-leaves` 按清单**序号**计数，已完成的节也占号。续跑想只试下一节，要填「doneLeaves 数 + 1」。
- 试跑通过后正式启动（**长驻后台**，用你运行环境提供的后台机制启动，别前台阻塞自己）：

```powershell
python scripts\required_walker.py --workdir "D:\xxw\<课程名>" --rate 2 --quiz-wait-min 90
```

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--workdir` | 工作目录（三件套所在）；也可用环境变量 `XXW_WORKDIR` | 必填 |
| `--rate` | 视频播放倍速 | 2 |
| `--quiz-wait-min` | 每道测验等你答案的最长分钟数，超时自动停止 | 45（建议改成 90） |
| `--max-leaves` | 处理到清单第几节为止 | 0＝全部 |
| `--no-recheck-prev` | 未解锁时不回看上一节核对，直接停止 | 关 |

## Monitoring loop（运行循环）

每 1～2 分钟做一轮：

```powershell
Get-Content "<WORKDIR>\required_watch.log" -Tail 20 -Encoding UTF8
Test-Path "<WORKDIR>\pending_quiz.json"    # 出现 = 有测验在等你答案
```

### 出题作答流程

1. **读题**：读 `<WORKDIR>\pending_quiz.json`。`stem`/`text` 出现乱码 = 平台字体加密。
2. **解码**（字体加密时）：
   - 读 `<WORKDIR>\font_grid_pending.png`：每格上方红色数字是序号、下方是真实字形。有视觉能力就自己认，提示词模板：
     > 这是字符对照表网格（8 列）。每个格子：上方红色数字是序号，下方大字是字形。请按序号依次输出每格的字，格式「序号:字」，每行一个。
   - 再从 `font_grid_pending.html` 用正则 `<div class="idx">(\d+)</div><div class="ch">([^<]+)</div>` 提取「序号→密文字符」。
   - 两者合成「密文→明文」映射，解码题干与选项。**形近字必须放大二次确认**，认错一个字整题答错。无视觉能力时把图发给用户认字。
   - 有把握的字可写入 `<WORKDIR>\font_map.json`（键=字符码点十进制）：`{"21700": "要"}`，下次同类题自动还原。
   - 可以通过视觉OCR工具识别题目并作答
3. **定答案**：由你推理（常识/安全教育类直接推）；专业题拿不准就**转问用户**，把解码后的题目原文转述给用户。平台只判对错、不泄露正确答案。
4. **写答案**：写 `<WORKDIR>\quiz_answers.json`（必须 UTF-8、合法 JSON；写错会被当作「还没写」一直等到超时）：

| 题型 | answer 写法 | 示例 |
| --- | --- | --- |
| 单选 / 判断 | 选项字母；判断题 对＝`A`、错＝`B` | `["A"]` |
| 多选 | `opt:<序号>`，从 0 起 | `["opt:0", "opt:2"]` |
| 填空 | `txt:<文本>` | `["txt:根部"]` |
| 兜底 | 选项文本片段（≥4 字，**必须只命中一个选项**，否则别用） | `["国家安全"]` |

   键是题目里的 `qid`（同一题所有选项共享一个 qid）：

   ```json
   { "123456001": ["A"], "123456002": ["opt:0", "opt:2"], "123456003": ["txt:根部"] }
   ```

5. 引擎每 5 秒查一次该文件，自动勾选、填写、提交；无需重启。日志出现 `✔ 测验卡N 已提交成功` 即成功，随后引擎自动删除 `pending_quiz.json` 与 `quiz_answers.json`。
6. **提交成功 ≠ 及格**：及格线很高（常见 5 题错 1 题就不过），以「下一节能否解锁」为准。

### 日志关键词 → 动作表

| 日志 | 含义 | 你的动作 |
| --- | --- | --- |
| `⛔ ... 未解锁` | 上一节任务点未完成，或平台落库延迟 | 先等 2～3 分钟重启一次（引擎自身也会重载上一节核对重试一轮）；仍锁 → 查上一节视频是否真播完、清单是否漏了四级子分节 |
| `⏱ 等待答案超时` | 你没在时限内写答案 | qid 未变的话把答案文件放回重启即可自动续交；qid 变了按出题作答流程重来 |
| `⛔ 测验提交失败` | 服务端拒收（字段/网络） | 引擎已自停并清理答案文件；直接重启重做该节（重新出题后按新 qid 作答） |
| `✘ 测验卡N: work iframe ... 未加载` | 页面加载失败 | 重启重做该节 |
| `✔ 视频卡N ... 播放完成` / `✔ 测验卡N 已提交成功` | 正常推进 | 无 |
| `? 视频卡N 解析不出视频附件` | 按 1 个播放处理 | 观察；若该节一直不解锁再排查 |

### 未及格重做流程

1. 日志「提交响应」里 `stuStatus`：4＝通过、5＝未达线。
2. 未达线：从 `<WORKDIR>\required_state.json` 的 `doneLeaves` 数组里删掉该节 id → 修正对应题的答案 → 重启引擎（会自动重做该节；**重做后 qid 会变**，必须按新 `pending_quiz.json` 重新写答案）。
3. 用户要求人工跳过某节：把 id 加进 `doneLeaves` 可以跳，但平台侧任务点未完成、下一节多半仍锁——如实告知后果，经用户确认再动。

### 向用户汇报

- 节奏：每完成 5～10 节、或出阻塞、或需要用户作答/确认时简报；其余时间安静跑。
- 模板：`已完成 X/N 节（当前到「x.x.x xxx」）；视频 M 个、测验提交 K 次；异常：……；下一步：……`
- 全部完成：`清单 N/N 完成；视频共 M、测验提交共 K；进度文件在 <WORKDIR>\required_state.json`。

## Security rules（红线）

- Cookie、题目、答案、课程编号都属个人信息/课程数据：**只留本机工作目录**，零外传、零入库、不上传网盘；对外输出（汇报、日志摘录）不带 Cookie 原文。
- 不修改引擎代码。发现 bug 报告用户；要改须用户确认，改完先做语法校验与试跑，经确认再正式运行。
- 不同时开多个挂机端跑同一账号；不与用户手机端同时学同一门课。
- 中断安全：随时可停（Ctrl+C / 结束进程），进度在 `required_state.json`，重启同命令自动续跑。
- 完成判定：`doneLeaves` 数 = 清单数（引擎结束时会打印完成与统计行）。

## Quick answers（用户常见问题速答）

- 「会弹窗吗？」→ 不会，全无头后台。
- 「答案哪来的？」→ 你（AI）推理或转问用户；平台只判对错，没有题库。
- 「中断了咋办？」→ 进度已存盘，重启命令自动接着跑。
- 「能快点吗？」→ 视频可调 `--rate`（2 已是稳妥上限）；进度必须真实播放，纯接口上报会被平台校验拦下。
- 「为什么卡在某节不动？」→ 上一节没过及格线或没全播完；按日志动作表 / 未及格重做流程处理。

> AI生成
