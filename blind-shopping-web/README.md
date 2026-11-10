# Blind Shopping Assistant — 拍照识物网页版

把 `最新.ipynb` 里的「摄像头 + 视觉大模型」能力搬到一个网页里，面向盲人和低视力用户：

- **拍一张照片，照片会一直留在页面上，直到你拍下一张才被替换。**
- 照片交给视觉模型分析（货架定位 / 手持商品核对 / 场景描述）。
- 结果用超大字号展示，并用浏览器自带的英文语音朗读。
- **App 里不带任何 API Key**：每个用户在侧边栏填自己的 Key，只存在当前浏览器会话里。

界面语言是英文（与原 notebook 一致），这份说明是中文。

---

## 1. 本地运行

```powershell
cd blind-shopping-web
py -3.14 -m pip install -r requirements.txt
py -3.14 -m streamlit run app.py
```

浏览器打开 <http://localhost:8501>。

也可以双击 `start.ps1`（它会复用已经装好的依赖）。

> 摄像头只在 `https://` 或 `http://localhost` 下可用，所以本地请用 `localhost` 打开，不要用 `127.0.0.1` 之外的局域网 IP（除非配置了 https）。

## 2. 部署到 GitHub + Streamlit Community Cloud

Streamlit 官方托管是免费、带 HTTPS 的，手机直接打开就能调起摄像头，最适合这个项目。

> **一键发布**：装好 [GitHub CLI](https://cli.github.com/) 后，在本文件夹运行
> `powershell -ExecutionPolicy Bypass -File .\publish.ps1`
> 脚本会帮你登录 GitHub、建仓库、推送代码，并打印最后两步（在 share.streamlit.io 点 Deploy）。

手动步骤：
1. **新建 GitHub 仓库**（例如 `blind-shopping-assistant`，Public 即可）。
2. **上传本文件夹**里的这些文件（不要上传 `.deps/`、`.venv/`）：
   ```
   app.py
   requirements.txt
   .streamlit/config.toml
   .gitignore
   README.md
   ```
   用命令行推送：
   ```powershell
   cd blind-shopping-web
   git init
   git add .
   git commit -m "Blind shopping assistant web app"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```
   （或者装好 GitHub CLI 后：`gh auth login`，再 `gh repo create blind-shopping-assistant --public --source . --push`。）
3. 打开 <https://share.streamlit.io>，用 GitHub 账号登录。
4. 点 **Create app** → 选择刚才的仓库和 `main` 分支 → **Main file path** 填 `app.py`。
5. 点 **Deploy**，等 1–3 分钟构建完成，会得到一个 `https://<名字>.streamlit.app` 地址。
6. 手机浏览器打开这个地址，点页面里的 **Take a photo** 即可拍照。

### 关于 API Key（重要）

Streamlit Cloud 的应用默认是公开的，**不要**把 Key 写进代码、或者写进 Streamlit 的 Secrets。正确做法就是现在这样：

- 每个用户在自己的浏览器里打开侧边栏 **API settings**，粘贴自己的 Key；
- Key 只保存在该用户这次会话里，换人、刷新页面就没了；
- 代码和仓库里完全没有 Key，可以放心公开。

## 3. 支持哪些模型服务

侧边栏 **Provider** 有两个选项：

| Provider | 说明 |
| --- | --- |
| `HKBU GenAI (api-key)` | 原 notebook 用的 HKBU 网关，Key 放在 `api-key` 请求头。Base URL 默认 `https://genai.hkbu.edu.hk/api/v0/rest/deployments`，Model 默认 `gpt-4.1`，API version 默认 `2024-12-01-preview`。 |
| `OpenAI-compatible (Bearer)` | 任何 OpenAI 兼容网关（OpenAI、DeepSeek、通义、自建代理等），Key 放在 `Authorization: Bearer`。填好 Base URL（如 `https://api.openai.com/v1`）和模型名即可。 |

**Advanced endpoint settings** 里可以改 Base URL、模型名和 API version。改完点 **Test connection** 验证 Key 和地址是否可用。

## 4. 使用流程

1. 侧边栏填 Key（只需一次）。
2. 选择模式：
   - **Shelf scan**：拍货架，问「目标商品在不在、在哪个位置、怎么伸手去拿」。
   - **Grab check**：拍手里的商品，确认拿的是不是目标商品。
   - **Describe view**：不指定商品，直接描述眼前有什么。
3. 在 **Product name** 里输入商品名（Shelf scan / Grab check 需要）。
4. 点 **Take a photo** 拍照。
   - 照片会留在左边，**直到你拍下一张**。
   - 右边自动出结果；之后改商品名或换模式，会用同一张照片重新分析。
5. 结果会自动朗读；点 **Speak again** 可以重听。

## 5. 常见问题

| 现象 | 处理 |
| --- | --- |
| 摄像头打不开 / 一直黑屏 | 必须用 `https://` 或 `localhost` 访问；浏览器地址栏左侧允许摄像头权限；换 Chrome / Edge / Safari 再试。 |
| `API key was rejected (HTTP 401/403)` | Key 填错，或 Provider 选错（HKBU 用 api-key 头，OpenAI 类用 Bearer）。 |
| `Endpoint not found (HTTP 404)` | Base URL、Model、API version 三者有一个不对。HKBU 的 Base URL 只写到 `/deployments`。 |
| `Rate limit reached (HTTP 429)` | 请求太频繁，等几秒再点 **Analyze again**。 |
| 模型返回格式错误 | 换个模型，或再拍一张（部分小模型不严格输出 JSON）。 |
| 想清空重来 | 侧边栏 **Start a new session**。 |

## 6. 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | 整个 Streamlit 应用：相机、图片压缩、API 调用、结果展示、语音朗读。 |
| `requirements.txt` | Streamlit Cloud / 本地安装依赖。 |
| `.streamlit/config.toml` | 深色高对比主题。 |
| `start.ps1` | 本地一键启动脚本。 |
| `publish.ps1` | 一键建 GitHub 仓库并推送（需 GitHub CLI）。 |

## 7. 隐私说明

- 照片只在当前请求中发送给用户自己配置的模型服务，本应用不落盘、不记录。
- 浏览器语音是本地合成（`speechSynthesis`），不经过第三方音频服务。
- 模型服务商可能会按其自身政策记录请求内容。
