# 🚀 SAP BAS Keep Alive（Python 版）

自动保活 SAP Business Application Studio (BAS) Dev Space，防止空间因长时间无操作被停止或删除，并**深度验证 dev space 内的应用是否真正健康运行**。

在 [zaofengyue/sap-bas-keepalive](https://github.com/zaofengyue/sap-bas-keepalive)（JS 版）基础上重写为 Python，并融合实战验证的 **supervisord 终端健康检查**。

---

## 与参考项目的区别

| 能力 | 参考项目（JS） | 本项目（Python） |
| --- | --- | --- |
| 定时登录保活 | ✅ 每小时 | ✅ 每 2 小时（可改 cron） |
| 停止时自动启动 dev space | ✅ | ✅ |
| 进入编辑器停留记录活跃 | ✅ 60 秒 | ✅ 60 秒 |
| **应用健康检查** | ❌ 仅页面状态 | ✅ IDE 终端执行 `supervisorctl status`，逐一断言服务 RUNNING |
| **服务异常自动修复** | ❌ | ✅ `supervisorctl start all` 后复查 |
| 结构化结果 | 日志 | 日志 + **GitHub Job Summary 表格** |
| 失败标红 | 仅脚本退出 | 退出码非 0，Actions 直接显示失败 |

---

## 工作原理

```
GitHub Actions 每 2 小时触发
        ↓
Playwright 无头浏览器登录 BTP（多账号支持）
        ↓
处理隐私声明弹窗 → 检查 dev space 状态
        ↓
  ┌─ STOPPED ──▶ 自动启动 ──▶ 轮询等待 RUNNING
  └─ RUNNING ─────────────────────────┐
                                      ↓
                        点击空间名进入 IDE 编辑器
                                      ↓
              在集成终端执行健康检查命令：
                pgrep supervisord + 标记文件 + supervisorctl status
                                      ↓
              逐一断言服务 RUNNING（异常则自动 start all 修复）
                                      ↓
                        停留 60 秒，记录活跃 ✅
```

> 健康检查原理：dev space 内通过 `.bashrc` 钩子在首次开终端时用 supervisord 拉起全部服务并写入 `/tmp/.sv-bootstrapped` 标记。健康检查读取 supervisord pid、标记文件与各任务状态，即可判断应用真实存活。

---

## 快速开始

### 1. Fork / 推送本仓库（Public 仓库免费额度足够）

### 2. 配置 GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 说明 | 示例 |
| --- | --- | --- |
| `BAS_URL` | BAS 地址（浏览器地址栏域名部分） | `https://xxxxx.us10cf.trial.applicationstudio.cloud.sap` |
| `BTP_USER` | BTP 登录邮箱 | `you@example.com` |
| `BTP_PASSWORD` | BTP 登录密码 | `your-password` |
| `BAS_SPACE_NAME` | Dev Space 名称（选填） | `boss` |

**多账号**：用 `;` 分隔，顺序一一对应：

```
BAS_URL=https://a.us10...;https://b.ap21...
BTP_USER=u1@x.com;u2@x.com
BTP_PASSWORD=p1;p2
BAS_SPACE_NAME=space1;space2
```

### 3. 手动触发测试

**Actions → SAP BAS Keep Alive → Run workflow**

成功标志：Job Summary 出现绿色表格（每个服务 ✅ RUNNING），日志输出 `✅ 应用运行正常`。

---

## 可调参数（workflow env 或环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HEALTHCHECK_ENABLED` | `true` | 是否执行终端健康检查 |
| `HEALTHCHECK_TASKS` | 6 个服务名 | 需断言的服务列表，`;` 分隔，空 = 跳过逐一断言 |
| `SUPERVISOR_CONF` | `~/.config/supervisor/supervisord.conf` | supervisor 配置路径 |
| `AUTO_FIX` | `true` | 服务未运行时自动 `start all` 并复查 |
| `FAIL_ON_UNHEALTHY` | `true` | 健康检查失败时退出码非 0 |
| `STAY_SECONDS` | `60` | 编辑器内停留时长 |
| `BOOTSTRAP_WAIT_SEC` | `40` | 等待终端钩子引导 supervisord 的时长 |
| `START_TIMEOUT_SEC` | `300` | 等待 dev space 变 RUNNING 的超时 |
| `EDITOR_TIMEOUT_SEC` | `180` | 等待编辑器加载的超时 |
| `HEADLESS` | `true` | 本地调试设 `false` 可看浏览器操作 |

---

## 本地运行

```bash
pip install -r requirements.txt
playwright install chromium

export BAS_URL="https://xxxxx.us10cf.trial.applicationstudio.cloud.sap"
export BTP_USER="you@example.com"
export BTP_PASSWORD="your-password"
export BAS_SPACE_NAME="boss"

python scripts/bas_keepalive.py          # 正常运行
HEADLESS=false python scripts/bas_keepalive.py   # 调试模式，可视化观察
```

---

## 各区域 BAS URL 格式

| 区域 | URL 格式 |
| --- | --- |
| 美国东部（us10 / trial） | `https://xxxxx.us10(.cf).applicationstudio.cloud.sap` |
| 日本东京（ap21） | `https://xxxxx.ap21.applicationstudio.cloud.sap` |
| 新加坡（ap10） | `https://xxxxx.ap10.applicationstudio.cloud.sap` |
| 欧洲（eu10） | `https://xxxxx.eu10.applicationstudio.cloud.sap` |
| 韩国（ap12） | `https://xxxxx.ap12.applicationstudio.cloud.sap` |

---

## 文件结构

```
.
├── .github/
│   └── workflows/
│       └── bas-keepalive.yml   # 定时任务（每 2 小时）+ 防停用 commit + 清理旧 runs
├── scripts/
│   └── bas_keepalive.py        # 登录保活 + 健康检查核心脚本
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 注意事项

- 仓库需保持 **Public**（私有仓库 Actions 免费额度每月 2000 分钟，本任务单次约 5-8 分钟，也够用但会消耗额度）
- workflow 自带两项自我保护：每次运行后写 `.last-run` commit（防止 GitHub 因 60 天不活跃禁用定时任务）、每次运行后清理历史 runs（防止 Actions 历史膨胀）
- 若 SAP 修改登录页结构导致选择器失效，可在 `HEADLESS=false` 模式下本地排查，更新 `scripts/bas_keepalive.py` 顶部的选择器常量
- 请勿将凭据写入代码，一律使用 GitHub Secrets

## License

MIT
