# MaiBot 多平台链接解析器 (maibot-link-parser)

一个 MaiBot 插件。在聊天里发一个知乎、微博、YouTube 或 Twitter(X) 的链接，它会自动读取链接里的内容，把标题、作者、正文摘要、图片、视频和互动数据（点赞、评论等）整理成一条回复发出来，并跳过机器人原本要做的回答。

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
config_version = "1.1.0"        # 配置版本，不要改
version = "1.1.0"               # 插件版本，不要改
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

## 怎么用

不需要输入任何命令。在对话里发一条包含所支持平台链接的消息，插件会自动识别并解析。

例如直接发：

```
https://www.zhihu.com/question/xxxxxx
```

机器人就会回复解析出的标题、作者、摘要、图片等。

## 权限与能力说明

插件通过配置的 OneBot HTTP 接口（如 NapCat、go-cqhttp）直接发送结果，`_manifest.json` 中声明的能力是：

- `send.text`：发送文本摘要（OneBot `text` 消息段）。
- `send.image`：发送图片（OneBot `image` 消息段）。

视频通过 OneBot 原生 `video` 消息段发送，多图和知乎结果通过合并转发接口发送，不依赖 SDK 的 `send.custom`。

插件需要联网访问各平台的接口或网页。

## 故障排查

### 知乎链接没反应，或后台提示 403 / 风控

知乎对未登录请求限制较严。需要填入你的知乎 Cookie：

1. 在浏览器登录知乎。
2. 打开浏览器开发者工具（一般按 `F12`），切到“网络 / Network”标签。
3. 刷新一个知乎页面，在请求列表里点任意一个请求，找到请求头里的 `Cookie` 这一行，整行复制它的值。
4. 粘贴到 `config.toml` 的 `[zhihu]` → `cookies = "..."` 里。
5. 重启 MaiBot。

### 视频发不出来或看不到

可能原因和处理：

- 视频太大或太长：调大 `max_video_size_mb` 和 `max_video_duration`。
- 网络问题：检查网络是否能正常访问对应平台。
- 权限问题：受限视频可能需要填对应的 Cookies（见 YouTube 一项）。

### YouTube / Twitter 解析没生效

依次检查：

1. `config.toml` 的 `[platforms]` 里，对应平台是否设成了 `true`。
2. YouTube 受限视频：在 `[youtube]` 填入登录 Cookies。
3. Twitter：可能需要配置 `twitter_api_key` 或反代地址 `twitter_api_base_url`。

改完配置后记得重启 MaiBot 让配置生效。

## 致谢与许可

- 知乎页面解析改编自 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)，采用了相近的浏览器指纹和 `js-initialData` 解析流程。
- 本插件采用 **MIT** 许可证发布，详见 `LICENSE` 文件。
