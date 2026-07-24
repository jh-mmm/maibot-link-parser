# MaiBot 多平台链接解析器 (maibot-link-parser)

一个 MaiBot 插件。在聊天里发一个知乎、微博、YouTube 或 Twitter(X) 的链接，它会自动读取链接里的内容，把标题、作者、正文摘要、图片、视频和互动数据（点赞、评论等）整理成一条回复发出来，并跳过麦麦原本要做的回答。

## 功能说明

- **多平台支持**：知乎、微博、YouTube、Twitter(X)。
- **内容提取**：标题、作者、正文摘要、点赞 / 评论等互动数据。
- **媒体发送**：自动下载并发送链接中的图片和视频。
- **合并转发**：知乎结果和多图结果默认打包成“合并转发”发送，避免逐条刷屏（可在配置中关闭，失败时自动回退为逐条发送）。
- **流程控制**：解析成功后拦截原消息的后续处理，避免机器人对同一条消息再答一遍。

## 安装

需要先装好并跑通过 MaiBot。下面假设 MaiBot 已经能正常运行。

### 1. 把插件放进 MaiBot 的插件目录

1. 找到 MaiBot 安装目录下的 `plugins` 文件夹。
2. 把本插件整个文件夹（里面有 `plugin.py`、`config.toml`、`requirements.txt` 等的那个）复制或移动到 `plugins` 文件夹下。
3. 最终路径大致是：`<MaiBot目录>/plugins/maibot-link-parser/`。

### 2. 打开命令行，进入插件目录

- **Windows**：打开插件文件夹，点击地址栏，输入 `cmd` 后回车，会弹出一个黑色命令行窗口，并且已经在这个文件夹里。
- 也可以用 PowerShell、Windows Terminal、或 VS Code 的终端。

### 3. 安装依赖

在命令行里运行：

```bash
pip install -r requirements.txt
```

等待安装完成。这一步会装上 `aiohttp`、`beautifulsoup4`、`curl_cffi`、`yt-dlp` 等插件运行必需的库。看到类似 `Successfully installed ...` 就说明装好了。

> 如果提示 `pip` 不是命令，说明 Python 没装好或没加入环境变量，需要先解决 Python 环境。

### 4. 重启 MaiBot

重启 MaiBot，让它加载这个插件。首次启动会在插件目录生成 `config.toml` 配置文件。

## 配置项

用记事本（或任意文本编辑器，如 VS Code）打开插件目录下的 `config.toml`。各部分含义如下：

```toml
[plugin]
name = "maibot-link-parser"     # 插件名，不要改
config_version = "1.2.0"        # 配置版本，不要改
version = "1.2.0"               # 插件版本，不要改
enabled = true                  # 是否启用插件。true 启用，false 停用

[general]
timeout = 15                  # 请求超时时间（秒）。网络慢可以调大
max_content_length = 500      # 文本摘要最大字数
max_video_size_mb = 50        # 允许下载的视频最大体积（MB）
max_video_duration = 300      # 允许下载的视频最大时长（秒）

[platforms]
zhihu = true                  # 启用知乎解析
weibo = true                  # 启用微博解析
youtube = false               # 启用 YouTube 解析（默认关，需要时改 true）
twitter = false               # 启用 Twitter 解析（默认关，需要时改 true）

[youtube]
youtube_api_key = ""          # YouTube Data API v3 密钥（可选）
cookies = ""                  # YouTube 登录 Cookies，下载受限视频时用（可选）

[zhihu]
# 知乎常要求登录。遇到 403 或风控时，把登录后的 Cookie 填到这里
cookies = ""
proxy = ""                     # HTTP/HTTPS 代理（如 http://127.0.0.1:7890），留空不使用

[twitter]
twitter_api_key = ""          # Twitter API Key（可选）
twitter_api_base_url = ""     # Twitter 自定义 API 地址，用于反代（可选）

[onebot]
host = "127.0.0.1"            # OneBot（NapCat/go-cqhttp 等）的 HTTP 地址。同台机器就填 127.0.0.1
port = 3000                   # OneBot 的 HTTP 端口，按你的 OneBot 配置填
token = ""                    # OneBot 的 access_token，没开就留空
bot_uin = ""                  # 机器人 QQ 号，合并转发节点会用，可不填
merge_send = true             # 知乎与多图结果是否用合并转发。true 开启，失败自动回退逐条发送
```

**几个常用设置的修改建议：**

- **只用知乎和微博**：保持 `[platforms]` 里 `zhihu = true`、`weibo = true`，其余不动即可。
- **视频发不出来**：把 `max_video_size_mb` 和 `max_video_duration` 调大，比如改成 `100` 和 `600`。
- **OneBot 地址端口**：必须和你实际运行的 OneBot（NapCat / go-cqhttp）的 HTTP 配置一致，否则消息发不出去。

## API 与 Cookie 怎么获取

下面分别说明这三个平台的凭据（API 密钥 / Cookie）怎么填。其中 **YouTube 和 Twitter(X) 默认留空也能用**——会自动走免费的公共接口；填 API 是为了拿到更完整的数据，或在公共接口不稳定时换成自己的源。**知乎则不同**：它没有 API，靠抓页面解析，对未登录请求限制很严，需要登录 Cookie 才能正常解析。

### YouTube

`[youtube]` 里有两项：

- **`youtube_api_key`**：填的是 **Google 的 YouTube Data API v3 密钥**（不是某个视频或账号的 key）。
  - **填了有什么用**：能拿到视频的播放量、点赞数、评论数、时长等互动数据。<del>但等视频发出来了，你不也就知道时长了吗？</del>
  - **不填会怎样**：自动回退到 `noembed.com` 这个免费公共接口，只能拿到标题、作者和封面图，**没有播放量、点赞、时长**这些数据。
  - **怎么申请**：
    1. 打开 [Google Cloud Console](https://console.cloud.google.com/)，登录 Google 账号。
    2. 新建一个项目（或用已有的）。
    3. 在「API 和服务 → 库」里搜索并启用 **YouTube Data API v3**。
    4. 进入「API 和服务 → 凭据」，点「创建凭据 → API 密钥」，把生成的密钥复制出来。
    5. 粘贴到 `youtube_api_key = "..."` 里。
  - 这个 API 有免费额度（每天一定的调用次数），个人使用足够。

- **`cookies`**：YouTube 登录后的 Cookies，**只在用 yt-dlp 下载受限视频时才需要**，普通视频留空即可。获取方式和下面知乎的 Cookie 类似：浏览器登录 YouTube → `F12` 打开开发者工具 → 在请求头里找到 `Cookie` 这一行 → 整行复制填进来。

### Twitter(X)

`[twitter]` 里有两项。**注意：这里填的不是 Twitter/X 官方 API**（官方 API 收费且返回格式不兼容），而是 [fxtwitter](https://github.com/FixTweet/FxTwitter) 这个开源项目的兼容接口。

- **两个都留空（默认）**：直接用 fxtwitter 的公共接口 `api.fxtwitter.com`，免费、开箱即用，能拿到正文、作者、图片、视频、点赞 / 转发 / 评论数。绝大多数人这样就够了。
- **`twitter_api_base_url` + `twitter_api_key`**：当你**自建或反代**了一个 fxtwitter 实例时才填。
  - **为什么填**：公共 fxtwitter 偶尔会被限流，自建一个更稳定，也能在前面加一层鉴权。
  - **`twitter_api_base_url`**：填你自己的 fxtwitter 实例根地址，比如 `https://fxtwitter.example.com`。插件会自动拼成 `https://fxtwitter.example.com/{用户名}/status/{推文ID}` 去请求。
  - **`twitter_api_key`**：填你给这个反代 / 实例设置的 Bearer Token（鉴权用）。插件请求时会带上 `Authorization: Bearer 你的key`。
  - **两项必须同时填**，只填其中一个不会生效，会退回用公共接口。
  - **格式要求**：你的实例返回的 JSON 必须和 fxtwitter 一致（含 `tweet` 字段，里面有 `author`、`text`、`media`、`likes` 等）。

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

不需要输入任何命令。在对话里发一条包含所支持平台链接的消息，插件会自动识别并解析。

例如直接发：

```
https://www.zhihu.com/question/xxxxxx
```

插件就会回复解析出的标题、作者、摘要、图片等。

## 权限与能力说明

插件通过配置的 OneBot HTTP 接口（如 NapCat、go-cqhttp）直接发送结果，`_manifest.json` 中声明的能力是：

- `send.text`：发送文本摘要（OneBot `text` 消息段）。
- `send.image`：发送图片（OneBot `image` 消息段）。

视频通过 OneBot 原生 `video` 消息段发送，多图和知乎结果通过合并转发接口发送，不依赖 SDK 的 `send.custom`。

插件需要联网访问各平台的接口或网页。

## 故障排查

### 知乎链接没反应，或后台提示 403 / 风控

知乎对未登录请求限制较严，需要填入登录 Cookie 才能正常解析。获取步骤见上文「[知乎的 Cookie 怎么填](#知乎)」一节，填完重启 MaiBot。

### 视频发不出来或看不到

可能原因和处理：

- 视频太大或太长：调大 `max_video_size_mb` 和 `max_video_duration`。
- 网络问题：检查网络是否能正常访问对应平台。
- 权限问题：受限视频可能需要填对应的 Cookies（见 YouTube 一项）。

### YouTube / Twitter 解析没生效

依次检查（各项具体填什么、怎么申请，见上文「[YouTube 与 Twitter(X) 和知乎的 API 与 Cookie 怎么填](#youtube-与-twitterx-和知乎的-api-与-cookie-怎么填)」一节）：

1. `config.toml` 的 `[platforms]` 里，对应平台是否设成了 `true`。
2. YouTube 受限视频：在 `[youtube]` 填入登录 Cookies。
3. Twitter：两个都留空时默认走公共 fxtwitter 接口；若公共接口被限流，再配置自建的 `twitter_api_base_url` 和 `twitter_api_key`。

改完配置后记得重启 MaiBot 让配置生效。

### 遇到了其他问题？

联系我的邮箱 `yuan00712@outlook.com`，或者在 GitHub 上提交 issues，希望能帮到您。

顺颂时祺。


## 致谢与许可

- 知乎页面解析改编自 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)，采用了相近的浏览器指纹和 `js-initialData` 解析流程。
- 本插件采用 **MIT** 许可证发布，详见 `LICENSE` 文件。
