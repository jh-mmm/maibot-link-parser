# MaiBot 多平台链接解析器 (maibot-link-parser)

一个 MaiBot 插件。在聊天里发一个知乎、微博、YouTube、Twitter(X) 或 Pixiv 的链接（或 PID），它会自动读取链接里的内容，把标题、作者、正文摘要、图片/画集、动图(GIF)、视频和互动数据（点赞、收藏、评论等）整理成一条回复发出来，并跳过麦麦原本要做的回答。

## 功能说明

- **多平台支持**：知乎、微博、YouTube、Twitter(X)、Pixiv。
- **Pixiv 深度解析**：支持插画、多图画集、漫画（含系列进度信息）、动图（Ugoira 自动合成为动态 GIF 在 QQ 中播放）、小说（含字数与正文摘录），支持链接触发与纯文本 PID 指令触发。
- **R18 / NSFW 安全控制**：支持 `blur`（Pillow 高斯模糊封面打码）、`ignore`（拦截并提示）、`send`（正常发送）。
- **网络与反代兼容**：支持 HTTP/HTTPS 代理与图片反向代理域名（如 `i.pixiv.re`），内置防盗链 Header。
- **内容提取**：标题、作者、正文摘要、点赞 / 收藏 / 评论等互动数据。
- **媒体发送**：自动下载并发送链接中的图片、动图和视频。
- **合并转发**：知乎结果和多图结果默认打包成“合并转发”发送，避免逐条刷屏（可在配置中关闭，失败时自动回退为逐条发送）。
- **访问控制**：支持按群号、用户 QQ 号双维度设置黑白名单，各平台（知乎/微博/YouTube/Twitter/Pixiv）均可独立配置触发范围。
- **流程控制**：解析成功后拦截原消息的后续处理，避免机器人对同一条消息再答一遍。


## 安装

需要先装好并跑通 MaiBot。下面假设 MaiBot 已经能正常运行。

### 1. 安装插件

本插件已经上传到 MaiBot 插件仓库，可以在插件市场中直接找到并下载安装。如果不想在插件市场中安装，也可以使用 Git 命令手动安装：

#### 方式一：插件市场安装（推荐）

在 MaiBot 管理面板的插件市场中搜索 **多平台链接解析器**（或 `maibot-link-parser`），点击一键安装即可。

#### 方式二：通过 Git 命令安装

1. **克隆仓库**：进入 MaiBot 的插件目录（`plugins` 文件夹），在命令行中执行：
   ```bash
   git clone https://github.com/jh-mmm/maibot-link-parser.git
   ```

2. **进入插件目录**：
   - **Windows**：打开 `plugins/maibot-link-parser` 文件夹，点击地址栏输入 `cmd` 后回车，即可在弹出的命令行窗口中直接定位到该目录。
   - 也可以使用 PowerShell、Windows Terminal 或 VS Code 终端进入该文件夹。

3. **安装依赖**：在插件目录的命令行里运行：
   ```bash
   pip install -r requirements.txt
   ```
   等待安装完成。这一步会安装 `aiohttp`、`beautifulsoup4`、`curl_cffi`、`Pillow`、`yt-dlp` 等插件运行必需的库。看到类似 `Successfully installed ...` 即说明安装成功。
   > 如果提示 `pip` 不是内部或外部命令，说明 Python 未正确配置环境变量，需要先配置好 Python 环境。

### 2. 重启 MaiBot

重启 MaiBot，让它加载这个插件。首次启动会在插件目录生成 `config.toml` 配置文件（也可在 WebUI 中直接进行可视化配置）。

## 配置项

本插件支持两种配置修改方式：
1. **MaiBot WebUI 可视化设置**：在 MaiBot 管理面板的插件设置页面中，所有配置项均已支持完整的中文标题、下拉选择菜单与悬浮说明。
2. **编辑 `config.toml` 配置文件**：用记事本或 VS Code 打开插件目录下的 `config.toml`，每个配置项上方均内嵌了详细的中文注释、取值范围与获取教程。

配置文件完整参考如下：

```toml
# ==============================================================================
#                      MaiBot 多平台链接解析器配置文件
# ==============================================================================

[plugin]
name = "maibot-link-parser"     # 插件唯一标识名（请勿修改）
config_version = "1.4.4"        # 配置文件版本（请勿修改）
version = "1.4.4"               # 插件版本（请勿修改）
enabled = true                  # 插件总开关：true=开启解析 | false=完全停用插件

[general]
timeout = 15                  # 全局网络请求超时时间（秒）。网络较慢或有波动时建议调大（如 20 或 30）
max_content_length = 500      # 正文摘要最大字符数（超过此长度会自动在句末断句截断并显示省略号）
max_video_size_mb = 50        # 允许下载并发送的最大视频体积（MB，超过限制则不发送视频文件）
max_video_duration = 300      # 允许下载并发送的最大视频时长（秒，300 即 5 分钟，超过限制则跳过）
show_status_hint = true        # 是否在识别到链接时发送提示消息（例如：🔗 识别到知乎链接，开始解析...；失败时发送失败原因）

[platforms]
zhihu = true                  # 是否启用【知乎】链接解析
weibo = true                  # 是否启用【微博】链接解析
youtube = false               # 是否启用【YouTube】视频解析（国外网站，默认关闭，需要时改 true）
twitter = false               # 是否启用【Twitter/X】推文解析（国外网站，默认关闭，需要时改 true）
pixiv = true                  # 是否启用【Pixiv】插画/漫画/动图/小说解析

[zhihu]
# 知乎登录 Cookies（遇到反爬挑战或 403 拦截时填写）：
cookies = ""
# HTTP/HTTPS 代理地址（例如 "http://127.0.0.1:7890"，留空则直接连接）：
proxy = ""
# 独立访问控制
group_mode = "off"            # off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析
group_whitelist = []
group_blacklist = []
user_mode = "off"             # off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析
user_whitelist = []
user_blacklist = []

[weibo]
# 独立访问控制
group_mode = "off"            # off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析
group_whitelist = []
group_blacklist = []
user_mode = "off"             # off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析
user_whitelist = []
user_blacklist = []

[youtube]
youtube_api_key = ""          # YouTube Data API v3 密钥（用于获取播放量/点赞数等数据，可选）
cookies = ""                  # YouTube 登录 Cookies（用于 yt-dlp 下载受限视频，可选）
proxy = ""                    # HTTP/HTTPS 代理地址（国内服务器推荐配置，如 "http://127.0.0.1:7890"）
# 独立访问控制
group_mode = "off"            # off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析
group_whitelist = []
group_blacklist = []
user_mode = "off"             # off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析
user_whitelist = []
user_blacklist = []

[twitter]
twitter_api_key = ""          # Twitter API Key（使用自建 fxtwitter 实例时可选）
twitter_api_base_url = ""     # Twitter 自定义 API 根地址（使用自建 fxtwitter 实例时可选）
proxy = ""                    # HTTP/HTTPS 代理地址（国内服务器推荐配置，如 "http://127.0.0.1:7890"）
# 独立访问控制
group_mode = "off"            # off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析
group_whitelist = []
group_blacklist = []
user_mode = "off"             # off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析
user_whitelist = []
user_blacklist = []

[pixiv]
cookies = ""                  # Pixiv 登录 Cookies（用于访问 R18 或登录限定内容，包含 PHPSESSID 等，可选）
proxy = ""                    # HTTP/HTTPS 代理地址（国内服务器推荐配置，如 "http://127.0.0.1:7890"）
img_proxy = ""                # 图片反向代理域名（如 "i.pixiv.re"，免代理直连下载图片，留空直连原图站）
nsfw = "blur"                 # R18 内容策略：blur=高斯模糊打码封面 | ignore=忽略并拦截 | send=正常发送
image_quality = "regular"     # 图片清晰度：regular=标准清晰度大图 | original=原图
max_manga_pages = 20          # 漫画最大下载解析页数（超过该限制仅发送封面并提示，0 为不限制）
# 独立访问控制
group_mode = "off"            # off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析
group_whitelist = []
group_blacklist = []
user_mode = "off"             # off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析
user_whitelist = []
user_blacklist = []

[onebot]
host = "127.0.0.1"            # OneBot（NapCat/go-cqhttp 等）HTTP 地址
port = 3000                   # OneBot HTTP 端口
token = ""                    # OneBot access_token，未开启 Token 保持为空
bot_uin = ""                  # 机器人 QQ 号（用于合并转发展示，可选）
merge_send = true             # 知乎与多图画集是否使用合并转发发送（失败自动降级逐条发送）
```

### 访问控制说明（分平台独立黑白名单）

在 `[zhihu]`、`[weibo]`、`[youtube]`、`[twitter]`、`[pixiv]` 各平台配置段中，均可**单独设置**该平台的访问控制策略（按 **群号** 和 **用户 QQ 号** 两个维度限制哪些消息会触发该平台的解析）。

**规则说明：**
- **各平台独立生效**：知乎、微博、YouTube、Twitter(X)、Pixiv 互不影响。例如可以让知乎在所有群开放，而 Pixiv 仅在指定群解析。
- **两个维度是「与」关系**：对于特定平台，群维度和用户维度都通过，才会触发该平台的解析。
- **同一维度同一时刻只有一份名单生效**：由该维度的 `mode` 决定；`mode = "off"` 时两份名单都被忽略。
- **whitelist 模式下名单为空 = 全部拒绝**：想放行所有人请把 `mode` 设回 `"off"`，不要留空白名单。
- **私聊不受群名单影响**：私聊消息没有群号，群维度自动跳过；用户名单在群聊和私聊中都生效。
- **被名单拦截的消息正常由麦麦回复**：插件只是不解析链接，不会静音该消息，对正常对话完全透明。
- 名单中的号码建议写成字符串（如 `["123456789"]`），写成数字也能兼容。

**配置示例**（知乎仅在特定群开放、微博屏蔽某位刷屏用户、Pixiv 不作限制）：
```toml
[zhihu]
group_mode = "whitelist"
group_whitelist = ["123456789", "987654321"]

[weibo]
user_mode = "blacklist"
user_blacklist = ["10001"]

[pixiv]
group_mode = "off"
user_mode = "off"
```

**几个常用设置的修改建议：**

- **只用知乎和微博**：保持 `[platforms]` 里 `zhihu = true`、`weibo = true`，其余设为 `false` 即可。
- **Pixiv 在国内无法直接访问**：在 `[pixiv]` 配置 `proxy = "http://127.0.0.1:7890"` 或填写图片反代域名 `img_proxy = "i.pixiv.re"`。
- **视频发不出来**：把 `max_video_size_mb` 和 `max_video_duration` 调大，比如改成 `100` 和 `600`。
- **OneBot 地址端口**：必须和你实际运行的 OneBot（NapCat / go-cqhttp）的 HTTP 配置一致，否则消息发不出去。

## API 与 Cookie 怎么获取

下面分别说明各平台的凭据（API 密钥 / Cookie / 代理）怎么填。

### Pixiv

`[pixiv]` 配置说明：
- **`proxy`**：国内服务器或本地运行常需配置代理（如 `http://127.0.0.1:7890`）以稳定连接 Pixiv API 与图片服务器。
- **`img_proxy`**：可选反向代理域名（如 `i.pixiv.re`），免代理直连图片加速。
- **`cookies`**：登录后的 Cookie（用于访问仅登录可见或受限作品）。**支持直接填写整行请求头 Cookie，或仅填 `PHPSESSID=...`（甚至仅填 session token 值）**，插件均会自动兼容解析。获取方式：在浏览器登录 Pixiv → 按 `F12` 打开开发者工具 → 在 Network 中查看任意请求的 `Cookie` Header 复制即可。
- **`nsfw`**：R18 处理策略：
  - `blur`（默认）：自动使用 Pillow 对封面图做高斯模糊打码，保护群聊安全。
  - `ignore`：遇到 R18 作品时直接拦截并回复友好提示。
  - `send`：不作打码，直接发送。

### YouTube

`[youtube]` 里有两项：

- **`youtube_api_key`**：填的是 **Google 的 YouTube Data API v3 密钥**（不是某个视频或账号的 key）。
  - **填了有什么用**：能拿到视频的播放量、点赞数、评论数、时长等互动数据。
  - **不填会怎样**：自动回退到 `noembed.com` 这个免费公共接口，只能拿到标题、作者和封面图，**没有播放量、点赞、时长**这些数据。
  - **怎么申请**：
    1. 打开 [Google Cloud Console](https://console.cloud.google.com/)，登录 Google 账号。
    2. 新建一个项目（或用已有的）。
    3. 在「API 和服务 → 库」里搜索并启用 **YouTube Data API v3**。
    4. 进入「API 和服务 → 凭据」，点「创建凭据 → API 密钥」，把生成的密钥复制出来。
    5. 粘贴到 `youtube_api_key = "..."` 里。
  - 这个 API 有免费额度（每天一定的调用次数），个人使用足够。

- **`cookies`**：YouTube 登录后的 Cookies，**只在用 yt-dlp 下载受限视频时才需要**，普通视频留空即可。

### Twitter(X)

`[twitter]` 里有两项。**注意：这里填的不是 Twitter/X 官方 API**，而是 [fxtwitter](https://github.com/FixTweet/FxTwitter) 这个开源项目的兼容接口。

- **两个都留空（默认）**：直接用 fxtwitter 的公共接口 `api.fxtwitter.com`，免费、开箱即用，能拿到正文、作者、图片、视频、点赞 / 转发 / 评论数。绝大多数人这样就够了。
- **`twitter_api_base_url` + `twitter_api_key`**：当你**自建或反代**了一个 fxtwitter 实例时才填。

### 知乎

`[zhihu]` Cookie 如何获取：

- **`cookies`**：知乎的登录 Cookie。知乎对未登录请求限制很严，遇到 403、风控或解析不出内容时就需要填。
  - **怎么获取**：
    1. 在浏览器里登录知乎。
    2. 打开浏览器开发者工具（一般按 `F12`），切到「网络 / Network」标签。
    3. 刷新一个知乎页面，在请求列表里点任意一个请求，找到请求头里的 `Cookie` 这一行，整行复制它的值。
    4. 粘贴到 `config.toml` 的 `[zhihu]` → `cookies = "..."` 里。
  - 留空也能跑，但可能因为风控拿不到内容；Cookie 失效后需要重新获取。
- **`proxy`**：可选的 HTTP/HTTPS 代理地址（如 `http://127.0.0.1:7890`）。服务器 IP 被知乎风控、或需要走代理访问时填写，留空则直连。

改完 `config.toml` 记得重启 MaiBot 让配置生效。

## 怎么用

不需要输入任何命令。在对话里发一条包含所支持平台链接的消息（或 PID 指令），插件会自动识别并解析。

例如直接发：

```text
https://www.zhihu.com/question/xxxxxx
https://www.pixiv.net/artworks/12345678
pid 12345678
```

插件就会回复解析出的标题、作者、摘要、图片/动图等。

## 权限与能力说明

插件通过配置的 OneBot HTTP 接口（如 NapCat、go-cqhttp）直接发送结果，`_manifest.json` 中声明的能力是：

- `send.text`：发送文本摘要（OneBot `text` 消息段）。
- `send.image`：发送图片（OneBot `image` 消息段）。

视频通过 OneBot 原生 `video` 消息段发送，动图自动合成 GIF 发送，多图和知乎结果通过合并转发接口发送，不依赖 SDK 的 `send.custom`。

插件需要联网访问各平台的接口或网页。

## 故障排查

### 知乎链接没反应，或后台提示 403 / 风控

知乎对未登录请求限制较严，需要填入登录 Cookie 才能正常解析。获取步骤见上文「[知乎](#知乎)」一节，填完重启 MaiBot。

### Pixiv 解析失败或图片下载失败

- 国内服务器请确保在 `[pixiv]` 中配置了有效的 `proxy`（如 `http://127.0.0.1:7890`）或配置了图片反代 `img_proxy = "i.pixiv.re"`。
- 若作品为 R18 限制且仅登录用户可见，可在 `cookies` 填入登录 Cookie。

### 视频发不出来或看不到

可能原因和处理：

- 视频太大或太长：调大 `max_video_size_mb` 和 `max_video_duration`。
- 网络问题：检查网络是否能正常访问对应平台。
- 权限问题：受限视频可能需要填对应的 Cookies（见 YouTube 一项）。

### YouTube / Twitter 解析没生效

依次检查：

1. `config.toml` 的 `[platforms]` 里，对应平台是否设成了 `true`。
2. YouTube 受限视频：在 `[youtube]` 填入登录 Cookies。
3. Twitter：两个都留空时默认走公共 fxtwitter 接口；若公共接口被限流，再配置自建的 `twitter_api_base_url` 和 `twitter_api_key`。

改完配置后记得重启 MaiBot 让配置生效。

### 遇到了其他问题？

联系我的邮箱 `yuan00712@outlook.com`，或者在 GitHub 上提交 issues，希望能帮到您。

顺颂时祺。


## 致谢与许可

- 知乎与 Pixiv 页面解析改编自 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)。
- 访问控制机制（群/用户黑白名单）参考自 [XinxInxiN0/bilibili_video_sender_plugin](https://github.com/XinxInxiN0/bilibili_video_sender_plugin)。
- 本插件采用 **MIT** 许可证发布，详见 `LICENSE` 文件。

