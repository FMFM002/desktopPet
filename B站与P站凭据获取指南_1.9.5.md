# B站与P站（Pixiv）凭据获取指南

> **适用版本**：铃兰桌宠 1.9.4
> **面向读者**：不需要懂编程的使用者
> **本文只解决一件事**：把 B站 和 Pixiv 两套凭据**填到正确的位置**。
> 所有键名、路径都以源码为准，文末列出依据的行号。

---

## 0. 先看这张速查表（最重要的一节）

| 平台 | 你需要准备的东西 | **填在哪里**（准确位置） |
|---|---|---|
| **B站** | 一段 Cookie 文本，至少包含 `SESSDATA`、`bili_jct`、`DedeUserID` | **不是 `.env`，也不是 `config.json`**。是一个**文件**，放在项目里的 `data/cookies/` 目录下 |
| **Pixiv** | 一个 `refresh_token`（刷新令牌）字符串 | **`.env` 文件**里加一行：`PIXIV_REFRESH_TOKEN=你的令牌` |

★ 这两个平台**存放位置完全不一样**，是本文最容易搞错的地方：

- B站 → **文件**（目录 `data/cookies/`）
- Pixiv → **`.env` 里的一行**（键名 `PIXIV_REFRESH_TOKEN`）

程序里**没有任何** B站 的 `.env` 键或 `config.json` 字段（已全文搜索确认）。
所以 B站 的 Cookie 如果填进 `.env`，**程序读不到，等于没填**。

---

## 第一部分：B站

### 1.1 B站需要哪些 Cookie 字段

| 字段名 | 必需？ | 它是干什么的 | 缺了会怎样 |
|---|---|---|---|
| `SESSDATA` | **必需** | 你的"已登录"身份凭证 | 程序只在**低画质**下工作，高清（1080P 及以上）拿不到 |
| `bili_jct` | **必需** | 写操作令牌（点赞、投币等） | 部分需要登录态的操作会失败 |
| `DedeUserID` | **必需** | 你的 UID | 同上，登录态不完整 |
| `buvid3` | 建议 | 设备标识 | 一般不影响，但带上更稳 |

★ **只有 `SESSDATA` 是程序会主动检查的**（源码第 158、171–173 行）。
但为了拿到完整的登录态，**三个都请一起填**。

### 1.2 怎么在浏览器里拿到它们

**前置条件：先在浏览器里登录 B站**（没登录就根本没有 `SESSDATA` 这一项）。

1. 用浏览器打开并登录 `https://www.bilibili.com`。
2. 按 **F12** 打开开发者工具。
   *（也可以右键页面 → "检查"）*
3. 切到 **Application** 面板。
   - 中文界面里可能叫 **"应用"**；Firefox 里叫 **"存储" / Storage**。
   - **不同浏览器版本的叫法略有不同**，以"能找到一个列出网站存储内容的侧栏"为准。
4. 在左侧列表里找到 **Cookies** → 点开 → 选 **`https://www.bilibili.com`**。
5. 右侧会出现一张表格，每一行是一个 Cookie，两列最关键：**Name**（名字）和 **Value**（值）。
6. 找到 `SESSDATA` 那一行，**双击 Value 列**，全选 → 复制。
   ⚠️ 值可能很长，复制时**不要漏掉结尾**，也不要多复制空格或换行。
7. 同样复制 `bili_jct` 和 `DedeUserID` 的值。
   建议顺手把 `buvid3` 也复制上。

> **提示**：`SESSDATA` 的值里常含 `%2C`、`%2F` 这样的字符（是 `,` `/` 的网址编码），
> **保持原样粘贴，不要自己改**。

### 1.3 填到哪里（★ 跟随源码，不是猜的）

**目标目录**：项目根目录下的 `data/cookies/`
（例如 `PTRTS1.9.4\data\cookies\`）

程序**只认两种文件**，二选一即可：

#### 方式 A（推荐）：放一个 `.json` 文件

在 `data/cookies/` 里新建一个文本文件，命名为：

```
www.bilibili.com_cookies.json
```

用记事本打开，粘贴下面这段（把每一处的"在这里粘…"换成你复制的值）：

```json
{
  "url": "https://www.bilibili.com",
  "cookies": [
    { "name": "SESSDATA",   "value": "在这里粘 SESSDATA 的值" },
    { "name": "bili_jct",   "value": "在这里粘 bili_jct 的值" },
    { "name": "DedeUserID", "value": "在这里粘 DedeUserID 的值" },
    { "name": "buvid3",     "value": "在这里粘 buvid3 的值" }
  ]
}
```

**保存时务必选"UTF-8"编码**（记事本右下角可以改）——存成别的编码会读不出来。

关于文件名的规则（源码第 148–153 行）：

- 文件名里**必须含有 `bilibili`**（大小写无所谓），或者文件名**正好是** `exported-cookies.json`；
- 以 `www.bilibili.com` **开头**的文件会被**优先**使用。

所以推荐直接用上面那个名字。如果你想用浏览器插件（例如 EditThisCookie）
导出的 `.json`，直接放进这个目录也行 —— 只要名字里带 `bilibili`。
插件导出的文件里会带 `expirationDate` 字段，程序会**自动跳过已过期的条目**，
这是正常行为，不用手动清理。

#### 方式 B（备选）：放 `cookies.txt`（Netscape 格式）

只有当**没有任何可用的 `.json` 文件**时，程序才会回头读它（源码第 164 行）。
文件路径必须是：

```
data/cookies/cookies.txt
```

格式要求很严：**一行一个 Cookie，每行必须有 7 列，用「Tab 制表符」分隔**，
并且第 6 列是名字、第 7 列是值（源码第 121–125 行）：

```
# Netscape HTTP Cookie File
.bilibili.com	TRUE	/	FALSE	1900000000	SESSDATA	在这里粘 SESSDATA 的值
.bilibili.com	TRUE	/	FALSE	1900000000	bili_jct	在这里粘 bili_jct 的值
.bilibili.com	TRUE	/	FALSE	1900000000	DedeUserID	在这里粘 DedeUserID 的值
```

（上面各行之间的空白处，实际上必须是**一个 Tab 字符**，不是若干空格。
用记事本手打 Tab 容易变成空格，所以**优先用方式 A**。）

> ⚠️ `data/cookies/` 这个目录**程序不会自动创建**。如果它不存在，
> 请自己在 `data` 文件夹里新建一个名为 `cookies` 的文件夹。

### 1.4 怎么验证 B站 填对了

#### 第 1 步：离线自检（不联网，只看程序能不能读到）

在**项目根目录**打开终端（Windows：在项目文件夹的地址栏里输入 `cmd` 然后回车），
粘贴这一整行并回车：

```
python -c "import sys; sys.path.insert(0,'.'); from tools.bilibili_tool import _get_cookie_string as g; cs=g(); names={p.split('=',1)[0] for p in cs.split('; ') if p}; print('已载入 Cookie 条数:', len(names)); [print(('  [OK]   ' if k in names else '  [缺失] ')+k) for k in ['SESSDATA','bili_jct','DedeUserID']]"
```

**填对时应该看到**（条数 ≥ 3，三项全 `[OK]`）：

```
已载入 Cookie 条数: 4
  [OK]   SESSDATA
  [OK]   bili_jct
  [OK]   DedeUserID
```

**没填 / 填错位置时**：

```
已载入 Cookie 条数: 0
  [缺失] SESSDATA
  [缺失] bili_jct
  [缺失] DedeUserID
```

★ **最重要的一步：填完 Cookie 后一定要把桌宠完全重启。**
Cookie 只在程序启动时读一次并缓存（源码第 110–112 行），
**不重启的话新 Cookie 不会生效**，你会以为是自己填错了。

#### 第 2 步：真实下载验证（联网）

重启桌宠后，在聊天窗口里给它一个 B站 视频链接，让它下载。
若成功，日志里会先出现这样一行（源码第 159 行）：

```
从 JSON 文件加载 Cookie: www.bilibili.com_cookies.json (4 条，含 SESSDATA)
```

关键是括号里的 **"含 SESSDATA"** 这四个字。

如果日志里出现的是（源码第 173 行）：

```
Cookie 中未找到 SESSDATA，B站可能只能获取低画质
```

说明 `SESSDATA` 没读到 —— 回到 1.3 检查文件和文件名。

---

## 第二部分：Pixiv（P站）

### 2.1 Pixiv 需要什么

**只需要一个东西**：`refresh_token`（刷新令牌）。

它是一个**很长的字符串**（常见是 40 多个字符），**不是你的 Pixiv 账号密码**。

程序里对应的键名（准确写法，源码第 113 行）：

```
PIXIV_REFRESH_TOKEN
```

### 2.2 为什么不能直接填账号密码

参考项目（`pixivpy`）的说明文档开头就写明了：由于 Pixiv 官方的限制，
**用账号密码登录的方式已经失效**，必须改用 `refresh_token`
（见参考项目 `README.md` 第 3–11 行）。

所以只有一条路：**走一次 OAuth 授权流程，换回一个 `refresh_token`**。

### 2.3 怎么拿到 refresh_token（OAuth PKCE 流程）

这个流程的原理是：

1. 你的电脑生成一个**随机的"验证串"**（`code_verifier`），并算出它的哈希（`code_challenge`）。
2. 你用浏览器打开 Pixiv 的登录地址，把 `code_challenge` 交给 Pixiv。
3. 你在浏览器里**正常登录** Pixiv（密码在这一步输，**只输给 Pixiv 官方网页**）。
4. Pixiv 把你重定向到一个带 `code=` 的地址，你把 `code` 复制出来。
5. 用 `code` + 原来的 `code_verifier` 去换令牌。因为只有你的电脑知道
   `code_verifier`，所以别人偷到 `code` 也没用 —— 这就是 PKCE 的作用。
6. 换回来的东西里包含 `refresh_token`，**这才是要填进 `.env` 的那串**。

#### 做法一：用现成的小工具（对非技术用户最省事）

参考项目 `README.md`（第 8–11 行）自己推荐了两个工具，它们实现的就是上面这套 PKCE 流程：

- **gppt（get-pixivpy-token）** — <https://github.com/eggplants/get-pixivpy-token>
  README 里标注为 "Easy to use 👍 base on selenium"，会自动开浏览器让你登录。
- **ZipFile 的 Pixiv OAuth Flow 说明** — <https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362>
  这是手工版流程的原始出处。

用 gppt 的话，大致是：

```
pip install gppt
gppt --help
```

然后**按它仓库 README 的说明操作**（不同版本的命令名可能不一样，
以 `gppt --help` 打印出来的为准）。最后它会输出一串 `refresh_token`。

#### 做法二：手工跑一遍（不想装额外工具时）

把下面这段存成一个文件，比如 `tools/_get_pixiv_token.py`，
**用完就可以删掉**（它只是帮你走一次流程，不会进程序逻辑）：

```python
# -*- coding: utf-8 -*-
"""一次性脚本：走 Pixiv 的 OAuth PKCE 流程，换出 refresh_token。用完可删。"""
import base64, hashlib, os, urllib.parse, requests

CLIENT_ID     = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"

# ① 生成 PKCE 的两个串
verifier  = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

login_url = ("https://app-api.pixiv.net/web/v1/login"
             f"?code_challenge={challenge}"
             "&code_challenge_method=S256&client=pixiv-android")

print("① 在浏览器里打开下面这个地址，并正常登录 Pixiv：\n")
print(login_url + "\n")
print("② 登录后浏览器会跳到一个打不开的页面 —— 这是正常的。")
print("   把地址栏里 code= 后面那一串复制出来（到 & 为止）。\n")

code = urllib.parse.unquote(input("③ 把 code 粘到这里后回车：").strip())

# ④ 用 code + verifier 换令牌
r = requests.post(
    "https://oauth.secure.pixiv.net/auth/token",
    data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "include_client_id": True,
    },
    headers={
        "User-Agent": "PixivAndroidApp/5.14.0 (Android 4.4; Mobile; en)",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    timeout=30,
)

if r.status_code != 200:
    print(f"\n✗ 失败：HTTP {r.status_code}\n{r.text[:400]}")
    print("\n常见原因：code 复制错了、code 已经过期（几分钟内有效，且只能用一次）、"
          "或者没挂代理连不上 Pixiv。")
else:
    print("\n✓ 成功！你的 refresh_token 是：\n")
    print(r.json()["response"]["refresh_token"])
```

运行方式（项目根目录终端）：

```
python tools/_get_pixiv_token.py
```

**注意事项**：

- `code` 的**有效期只有几分钟**，而且**只能用一次**。复制要快，失败就重新跑一遍脚本。
- 国内网络需要**先挂代理**，否则第 4 步连不上 `oauth.secure.pixiv.net`
  （参考项目源码第 140 行就是这个域名）。
  挂好代理后可以直接跑；如果脚本报连不上，在脚本里给 `requests.post(...)` 加一个
  `proxies={"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}`
  （端口换成你自己代理客户端的）。
- **Pixiv 的登录页面偶尔会变**（有时会要求人机验证）。如果做法二卡住，
  请改用做法一的 gppt 工具。

### 2.4 填到哪里

打开**项目根目录**的 `.env` 文件（如果没有，就把 `.env.example` 复制一份改名成 `.env`），
找到 `# ---- Pixiv ----` 那一段，把令牌填在等号后面：

```ini
# ---- Pixiv ----
PIXIV_REFRESH_TOKEN=把刚才拿到的刷新令牌粘在这里
```

★ 三个容易犯的错：

1. **等号两边不要加空格**，值也**不要加引号**。
2. **不要换行**，令牌必须和 `PIXIV_REFRESH_TOKEN=` 在同一行。
3. 这一行**要放在 `.env` 里**（程序读过这个文件，源码第 49 行），
   不要放进 `config.json`。

程序**首次运行**且没有 `.env` 时，会自动从 `.env.preset` 复制一份出来，
所以 `.env` 通常已经存在；`.env.example` 第 46 行的位置就是这一项。

### 2.5 国内网络：还要顺手配一个代理

Pixiv 的接口在国内直连基本连不上，`.env` 里再加一行（写在 `# ---- Pixiv ----` 附近）：

```ini
PRTS_PROXY=http://127.0.0.1:7890
```

- 端口换成你代理客户端实际监听的端口（Clash 常见 `7890` / `7897`，v2rayN 常见 `10808`）。
- 程序会**优先读 `PRTS_PROXY`**；如果没设，会兼容读旧名字 `PIXIV_PROXY`
  （源码第 116–132 行；`.env.example` 第 42、47–48 行）。
- **另一条路**（不想开代理时）：先 `pip install requests-toolbelt`，
  再在 `.env` 里加一行 `PIXIV_BYPASS=1` 走 SNI 绕过
  （源码第 167、196–199 行）。缺 `requests-toolbelt` 时这条会直接报错并告诉你装它。

### 2.6 ★ 关于令牌"会变"这件事

Pixiv 会**轮换刷新令牌**：每次用旧令牌换新令牌，旧的立刻作废。

本程序**已经自动处理**了这件事 —— 它会把新令牌**写回 `.env`**
（源码第 236–255 行）。所以：

- 你可能会发现 `.env` 里 `PIXIV_REFRESH_TOKEN=` 后面的值和当初填的**不一样了**
  —— **这是正常的，不要改回去**。
- 千万不要把同一个 `refresh_token` 同时填给两个程序（比如桌宠和别的 P 站工具）。
  两边会互相顶掉，表现为"昨天还能用，今天就登录失败"。
- 如果确实失效了（报错信息里会提示"令牌可能过期/失效"），
  按 2.3 节重新走一次流程换新的即可。

### 2.7 怎么验证 Pixiv 填对了

#### 第 1 步：离线确认 `.env` 被读到了（不联网）

项目根目录终端里跑：

```
python -c "import os; from dotenv import load_dotenv; load_dotenv('.env'); t=(os.getenv('PIXIV_REFRESH_TOKEN') or '').strip(); print('PIXIV_REFRESH_TOKEN:', ('已配置（%d 字符）'%len(t)) if t else '★ 还没配置')"
```

看到 `已配置（43 字符）` 之类（数字因令牌而异，关键是**不是"还没配置"**）就说明位置填对了。

#### 第 2 步：真实调用一次（联网）

项目里**自带**了一个手动排查入口（源码第 540–552 行），直接跑：

```
python tools/pixiv_tool.py search keyword=风景 limit=3
```

也可以换个动作试：

```
python tools/pixiv_tool.py ranking mode=day limit=3
python tools/pixiv_tool.py info illust_id=59580629
```

**成功时**会打印一段 JSON，里面有 `"success": true` 和一个 `items` 列表
（每条形如 `1. [作品ID] 标题 —— 画师 (N页, ♥收藏数)`）。

**失败时**分两种，看错误信息就能定位：

| 打印出来的内容 | 意思 | 怎么办 |
|---|---|---|
| 一整段"还没配置 Pixiv 刷新令牌…请在项目根目录 .env 里加一行：`PIXIV_REFRESH_TOKEN=…`" | `.env` 里没读到令牌 | 回到 2.4 检查键名和位置 |
| `Pixiv 登录失败: …` 并且带"令牌可能过期/失效" | 令牌不对或已作废 | 按 2.3 重新换一个 |
| `search 失败（网络）: …` 并提示检查 `PIXIV_PROXY` | 网络/代理问题 | 回到 2.5 配代理 |

#### 第 3 步：在桌宠里验证

重启桌宠后，在聊天窗口让它找一张 P 站插画。
下载成功的图会**自动出现在聊天窗口里**（源码第 473–490 行），
文件落在 `data/downloads/pixiv/<作品ID>_<标题>/`。

---

## 第三部分：常见问题

**Q：B站的 Cookie 我填进 `.env` 了，为什么没用？**
A：因为程序**根本不读** `.env` 里的 B站 Cookie。B站凭据**只能**放在
`data/cookies/` 目录的文件里（见 1.3）。

**Q：填完 B站 Cookie，马上测试还是低画质。**
A：Cookie 只在程序启动时读一次（源码第 110–112 行）。**必须完全重启桌宠**。

**Q：`data/cookies` 这个文件夹不存在。**
A：程序不会自动建它，请自己在 `data` 下新建一个叫 `cookies` 的文件夹。

**Q：Pixiv 昨天还能用，今天报登录失败。**
A：可能是令牌被轮换后又被别处用旧值覆盖了（见 2.6）。按 2.3 重新换一个。

**Q：这些凭据安全吗？会不会被上传？**
A：B站 Cookie 文件放在本地 `data/cookies/`，Pixiv 令牌放在本地 `.env`，
`.env` 已被 `.gitignore` 排除、不随包发布。
但请注意：`SESSDATA` 和 `refresh_token` **等同于你的登录状态**，
不要截图给别人、不要发到群里、不要放进共享网盘。

**Q：我想撤销授权怎么办？**
A：B站 → 在浏览器里"退出登录"会让 `SESSDATA` 失效；删掉 `data/cookies/` 里的文件即可。
Pixiv → 在 Pixiv 账号设置里解除对第三方应用的授权；再把 `.env` 里
`PIXIV_REFRESH_TOKEN=` 的值清空。

---

## 附录：本文依据的源码位置

以下每一条都是本文内容的出处。**改代码后请同步更新本文。**

### B站 —— `tools/bilibili_tool.py`

| 行号 | 内容 | 本文对应处 |
|---|---|---|
| 61 | `COOKIES_FILE = PROJECT_ROOT / "data" / "cookies" / "cookies.txt"` | 1.3 方式 B 的准确路径 |
| 108–112 | `_get_cookie_string()` 开头；`_cookie_str` 缓存 | 1.4「必须重启」、第三部分 Q2 |
| 114–126 | `_parse_netscape()`：按 Tab 分行，**第 6 列=名字、第 7 列=值** | 1.3 方式 B 的 7 列要求 |
| 128–142 | `_parse_json()`：支持 `{"cookies":[…]}` 与裸数组两种写法，字段为 `name` / `value` / `expirationDate` | 1.3 方式 A 的 JSON 结构 |
| 136–139 | 带 `expirationDate` 且已过期的条目**被跳过** | 1.3「自动跳过已过期条目」 |
| 147 | `_cookie_dir = PROJECT_ROOT / "data" / "cookies"` | 1.3 目标目录 |
| 148–153 | 只挑文件名含 `bilibili` 或名为 `exported-cookies.json` 的 JSON；`www.bilibili.com` 开头者优先 | 1.3 文件名规则 |
| 152 | 注释明确指出 `www.bilibili.com` 开头的文件可能来自 EditThisCookie 导出（含 SESSDATA） | 1.3「插件导出」 |
| 158、171–173 | **唯一被检查的字段是 `SESSDATA`**；缺失时打警告"B站可能只能获取低画质" | 1.1、1.4 |
| 164–166 | 只有**没有**可用 JSON 时才回退读 `cookies.txt` | 1.3 方式 B 的优先级 |
| 362–364 | PlayURL 请求带上 Cookie | 1.1 `SESSDATA` 决定画质 |
| 467–469 | 下载请求也带 Cookie（防盗链） | 同上 |
| 669–670 | yt-dlp 回退方案只使用 `cookies.txt` 这个文件 | 1.3（`.json` 不参与 yt-dlp 回退） |

> ★ 全项目搜索 `cookie` / `refresh_token` 后确认：**不存在**任何 B站 的 `.env` 键，
> **也不存在** `config.json` 的 Cookie 字段 —— 所以 1.3 的结论"B站靠文件"是唯一的正确填法。

### Pixiv —— `tools/pixiv_tool.py`

| 行号 | 内容 | 本文对应处 |
|---|---|---|
| 12–14 | 文件开头的提示：在项目根目录 `.env` 里加 `PIXIV_REFRESH_TOKEN=你的刷新令牌` | 2.1、2.4 |
| 21–23 | `.env` 里可加 `PIXIV_PROXY` / `PIXIV_BYPASS=1` | 2.5 |
| 44、49 | `from dotenv import load_dotenv`；`load_dotenv(PROJECT_ROOT / ".env")` | 2.4「程序会读 `.env`」 |
| 113 | `os.getenv("PIXIV_REFRESH_TOKEN")` —— **准确键名** | 2.1、2.4 |
| 116–132 | `_proxy()`：经 `src/core/net_proxy` 读，兼容 `PIXIV_PROXY` | 2.5 |
| 167 | `PIXIV_BYPASS` 的判定（空 / `0` / `false` 视为关闭） | 2.5 |
| 170–177 | 未配置令牌时的可操作提示文本（`_NO_TOKEN_HINT`） | 2.7 失败对照表 |
| 196–199 | `PIXIV_BYPASS=1` 需要 `requests-toolbelt`，缺失时明确报错 | 2.5 |
| 224 | `api.auth(refresh_token=token)` —— 令牌就是在这一步被使用 | 2.2 |
| 229–230 | 认证失败时的提示："令牌可能过期/失效；刷新令牌是一次性的" | 2.6、2.7 |
| 236–255 | `_save_rotated_token()`：新令牌**自动写回 `.env`**（247–251 行是替换逻辑） | 2.6「值变了是正常的」 |
| 473–490 | 下载完成的图推送到聊天窗口 | 2.7 第 3 步 |
| 540–552 | `if __name__ == "__main__"` 手动排查入口与用法打印 | 2.7 第 2 步的命令 |

### `src/core/net_proxy.py`（代理键名）

| 行号 | 内容 | 本文对应处 |
|---|---|---|
| 71–72 | `_PROXY_KEYS = ("PRTS_PROXY", "PIXIV_PROXY")` —— `PRTS_PROXY` 优先 | 2.5 |
| 137 | 两者都空时的提示文本 | 2.5 |

### `.env.example`（配置项在模板里的位置）

| 行号 | 内容 | 本文对应处 |
|---|---|---|
| 42 | `PRTS_PROXY=` | 2.5 |
| 44–46 | `# ---- Pixiv ----` / `# refresh token 会过期…` / `PIXIV_REFRESH_TOKEN=` | 2.4 |
| 47–48 | `# 兼容旧名：没设 PRTS_PROXY 时用它` / `PIXIV_PROXY=` | 2.5 |

### `tools/make_preset_env.py`（出厂预设不含哪些）

| 行号 | 内容 | 本文对应处 |
|---|---|---|
| 18–23 | "**不预设**"清单里明确包含 `PIXIV_REFRESH_TOKEN`（会过期）与 `PIXIV_PROXY`（机器相关） | 2.4、2.5 |
| 96–97 | 生成器输出的注释行，同上 | 同上 |

### 参考项目（只读，未修改）

路径：`素材-不可改-可复制提取\pixivpy-master\pixivpy-master\`

| 文件:行 | 内容 | 本文对应处 |
|---|---|---|
| `README.md` 3–11 | "password login no longer exist"，请改用 `api.auth(refresh_token=…)`；并给出获取 `refresh_token` 的三个链接（ZipFile gist / gppt / Selenium gist） | 2.2、2.3 |
| `pixivpy3/api.py` 18–20 | `client_id` / `client_secret` / `hash_secret` 三个常量的值 | 2.3 手工脚本里的常量 |
| `pixivpy3/api.py` 118–161 | `auth()` 的实现；152–158 行是 `grant_type` 的两种分支（`password` / `refresh_token`） | 2.2 为什么只能填 refresh_token |
| `pixivpy3/api.py` 139–145 | 认证域名 `https://oauth.secure.pixiv.net` + 路径 `/auth/token` | 2.3 脚本里的 POST 地址 |
| `pixivpy3/api.py` 184–185 | 响应里取 `access_token` / `refresh_token` | 2.3 第 6 步 |
| `demo.py` 12–14 | "get your refresh_token, and replace `_REFRESH_TOKEN`"，并链接 issue #158 的说明 | 2.2 |
| `demo.py` 326–340 | `AppPixivAPI(**_REQUESTS_KWARGS)` → `aapi.auth(refresh_token=_REFRESH_TOKEN)` 的标准用法 | 2.2 |

> ⚠️ **重要事实**：参考项目里 **pixivpy3 本身没有实现 PKCE 流程**。
> 全文搜索 `code_challenge` / `code_verifier` 在两个 `pixivpy3` 目录里都没有命中，
> `api.py` 只保留了 `password` 与 `refresh_token` 两种 `grant_type`。
> PKCE 是**在 pixivpy 之外**通过独立工具完成的
> （参考项目 README 第 8–11 行给出的 gppt / ZipFile gist 才是那套流程的载体）。
> 本文 2.3 的 URL 与参数即来自这套社区通行流程；Pixiv 官方随时可能调整，
> **如果手工流程走不通，请以 gppt 工具为准。**
