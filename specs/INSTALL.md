# 安装 Autoloop

需要 Node.js 24、DeepSeek Harness 0.2.2-rc.3、DS Bot 0.3.8、Python 3.11+、Git 和已登录的 Codex CLI。适用于用户自己的 DSH profile 和产品仓库。

## 使用发行包

从插件 GitHub Release 下载 `.tgz` 和同名的 `.sha256` 文件。在下载目录核验，随后传入发行包的绝对路径。

```sh
shasum -a 256 -c ./*.tgz.sha256
dsh plugin --profile web add "$PWD"/dsbot-autoloop-plugin-*.tgz
```

默认使用 `python3`。如果配置了其他解释器，插件使用所选解释器。没有兼容的 Pydantic 时，首次启动通过 Python 包索引在插件数据目录创建独立虚拟环境。依赖已安装时可离线启动。

在 Chief 的项目设置中注册自己的私有 GitHub App，按 GitHub 页面提示安装并选择产品仓库。macOS 使用钥匙串存放私钥。Linux 需要可用的 Secret Service 会话和 `secret-tool`。为每个产品选择或创建一个私有仓库，再绑定 PM 和使用 Bot。GitHub 的账户权限由安装者本人确认。

## 从源码生成发行包

在插件目录安装开发依赖，然后运行以下命令。输出目录默认为 `dist`，也可以传入其他目录。脚本用发行包的文件清单建立临时目录，在该目录写入新客户端，最后生成 npm 兼容归档和 SHA-256 文件。

```sh
npm install
node scripts/package.mjs /absolute/path/to/output
```
